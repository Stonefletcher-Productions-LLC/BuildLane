"""Fletchery's node worker: runs on a test node (SF-NODE-01), takes jobs from the Fletchery coordinator on the PC, runs
them and reports back. Python standard library only; nothing else to install.

The worker trusts its own code and config, not the coordinator: a job can only name one of the RECIPES below, with
parameters checked here, and a commit SHA. The worker fetches that commit read-only with the node's own deploy key and
runs the recipe's repo scripts at below-normal priority. Logs stream back in chunks; a heartbeat goes every 30 s and
carries the answer to "was this job cancelled?".

Config (JSON, e.g. C:\\StoneNode\\fletchery-worker.json):
    {"coordinator": "http://100.83.43.111:8771", "machine": "SF-NODE-01",
     "keyFile": "C:\\\\StoneNode\\\\keys\\\\fletchery_node_key",          # the key Fletchery printed when the node was added
     "labels": ["os:windows", "ram:24", "unreal:5.8.4", "role:tests", "role:cook"],
     "workRoot": "E:\\\\buildlane-node\\\\_work",                          # the checkout is <workRoot>\\fletchery\\LifeShare
     "repoUrl": "git@github.com:Stonefletcher-Productions-LLC/LifeShare.git",
     "gitSshKey": "C:\\\\StoneNode\\\\keys\\\\lifeshare_deploy",           # read-only deploy key
     "engine": "E:\\\\UE_5.8",
     "pauseFile": "E:\\\\GitHubRunner\\\\PAUSED-NODE",                    # while it exists, take no new job
     "recipes": ["node-checks", "nightly-full", "checkout-only"]}         # what this node agrees to run

    python node_worker.py check   --config <file>     sign in to the coordinator once and print what it sees
    python node_worker.py run     --config <file>     the worker loop (what the logon task runs)
    python node_worker.py install --config <file>     a Windows logon task that starts `run` (current user, limited)
    python node_worker.py uninstall
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

VERSION = "1"
TASK_NAME = "Fletchery node worker"
HEARTBEAT_SECONDS = 30
LOG_FLUSH_SECONDS = 5
SHA = re.compile(r"^[0-9a-f]{40}$")
ONLY = re.compile(r"^[A-Za-z0-9_]+(,[A-Za-z0-9_]+)*$")
WINDOWS = os.name == "nt"
BELOW_NORMAL = 0x00004000


# ---- recipes: the only things a job can run ----
def _probe_paths(ws: Path) -> list[str]:
    logs = ws / "Saved" / "Logs"
    out = [str(ws / "Binaries")]
    if logs.is_dir():
        out += [str(p) for p in sorted(logs.glob("*.log")) if "-backup-" not in p.name]
    return out


def _ps_quote(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def _ps(script: str, ws: Path, args: list) -> list[str]:
    return ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(ws / script), *args]


def _stop_processes(ws: Path) -> list[str]:
    # -ProbePath takes an array, which -File can't pass; build the call with every value quoted.
    probes = ",".join(_ps_quote(p) for p in _probe_paths(ws))
    cmd = (f"& {_ps_quote(ws / 'Tools/CI/Stop-JobProcesses.ps1')} -Workspace {_ps_quote(ws)} "
           f"-ProbePath @({probes}) -MaxWaitSeconds 30; exit $LASTEXITCODE")
    return ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", cmd]


def recipe_steps(recipe: str, params: dict, ws: Path, cfg: dict, result_json: Path) -> list[tuple[str, object, bool]]:
    """(name, argv or a callable giving argv, always) for each step, mirroring node-checks.yml / nightly-node.yml.
    `always` steps run even after a failure (clean-up)."""
    engine = cfg.get("engine") or r"E:\UE_5.8"
    if recipe == "checkout-only":
        return [("Show the commit", ["git", "log", "-1", "--format=%H %s"], False)]
    if recipe not in ("node-checks", "nightly-full"):
        raise ValueError(f"unknown recipe {recipe!r}")
    tests = ["-Engine", engine, "-ResultJson", str(result_json)]
    if recipe == "node-checks":
        only = str(params.get("only") or "")
        if only and not ONLY.match(only):
            raise ValueError("bad `only`")
        if only:
            tests += ["-Only", only]
        if params.get("strict_slots") is True:
            tests.append("-StrictSlots")
    return [
        ("Clear stale processes from this checkout", lambda: _stop_processes(ws), False),
        ("Reset generated Content from prior jobs", _ps("Tools/CI/ResetContentWorkspace.ps1", ws, []), False),
        ("Build, BuildContent and tests", _ps("Tools/CI/NodeTests.ps1", ws, tests), False),
        ("Stop what this job left running", lambda: _stop_processes(ws), True),
        ("Reset Content after the run", ["git", "checkout", "--", "Content/Sim"], True),
        ("Reset Content workspace", _ps("Tools/CI/ResetContentWorkspace.ps1", ws, []), True),
    ]


# ---- machine stats ----
def stats(cfg: dict) -> dict:
    out = {"paused": bool(cfg.get("pauseFile")) and Path(cfg["pauseFile"]).exists()}
    try:
        out["disk_free_gb"] = shutil.disk_usage(cfg.get("workRoot") or ".").free / 2**30
    except OSError:
        pass
    total, free = _memory()
    if total:
        out["ram_total_gb"], out["ram_free_gb"] = total / 2**30, free / 2**30
    cpu = _cpu.sample()
    if cpu is not None:
        out["cpu_percent"] = cpu
    return out


def _memory() -> tuple[int, int]:
    if WINDOWS:
        import ctypes

        class MS(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong), ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong), ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong), ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong), ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        m = MS()
        m.dwLength = ctypes.sizeof(MS)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
            return m.ullTotalPhys, m.ullAvailPhys
        return 0, 0
    try:
        info = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines() if ":" in line)
        kb = lambda k: int(info[k].split()[0]) * 1024  # noqa: E731
        return kb("MemTotal"), kb("MemAvailable")
    except (OSError, KeyError, ValueError):
        return 0, 0


class _Cpu:
    def __init__(self):
        self.last = None

    def _times(self):
        if WINDOWS:
            import ctypes
            idle, kernel, user = (ctypes.c_ulonglong() for _ in range(3))
            if ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
                return idle.value, kernel.value + user.value  # kernel time includes idle
            return None
        try:
            parts = [int(x) for x in Path("/proc/stat").read_text().split("\n", 1)[0].split()[1:]]
            return parts[3] + parts[4], sum(parts)
        except (OSError, ValueError, IndexError):
            return None

    def sample(self):
        t = self._times()
        last, self.last = self.last, t
        if not t or not last or t[1] == last[1]:
            return None
        return max(0.0, min(100.0, 100.0 * (1 - (t[0] - last[0]) / (t[1] - last[1]))))


_cpu = _Cpu()


# ---- talking to the coordinator ----
class Coordinator:
    def __init__(self, cfg: dict):
        url = str(cfg["coordinator"]).rstrip("/")
        if not url.startswith("http://") and not url.startswith("https://"):
            raise ValueError("coordinator must be an http(s) URL")
        self.url = url
        self.machine = cfg["machine"]
        self.key = Path(cfg["keyFile"]).read_text(encoding="utf-8").strip()
        # The coordinator is on the private network: never send the key through a proxy.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(self, path: str, body: dict, timeout: float = 30) -> dict:
        req = urllib.request.Request(self.url + path, data=json.dumps(body).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.key,
                                              "X-Fletchery-Machine": self.machine})
        try:
            with self.opener.open(req, timeout=timeout) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read() or b"{}").get("error", "")
            except ValueError:
                detail = ""
            raise RuntimeError(f"{path}: HTTP {e.code} {detail}") from None


# ---- running a job ----
def _git_env(cfg: dict) -> dict:
    env = dict(os.environ)
    key = cfg.get("gitSshKey")
    if key:
        env["GIT_SSH_COMMAND"] = f'ssh -i "{key}" -o IdentitiesOnly=yes -o BatchMode=yes'
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _workspace(cfg: dict) -> Path:
    return Path(cfg["workRoot"]) / "fletchery" / "LifeShare"


class Job:
    def __init__(self, coord: Coordinator, cfg: dict, job: dict, log=print):
        self.coord, self.cfg, self.job, self.log = coord, cfg, job, log
        self.buf: list[str] = []
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.cancelled = False
        self.done = threading.Event()

    def out(self, text: str) -> None:
        with self.lock:
            self.buf.append(text if text.endswith("\n") else text + "\n")

    def flush(self) -> None:
        with self.lock:
            text, self.buf = "".join(self.buf), []
        while text:
            chunk, text = text[:256 * 1024], text[256 * 1024:]
            try:
                self.coord.call("/v1/log", {"job_id": self.job["id"], "text": chunk})
            except Exception as e:  # keep running; the log has a gap
                self.log(f"log upload failed: {e}")

    def _beat(self) -> None:
        last_log = time.monotonic()
        while not self.done.wait(LOG_FLUSH_SECONDS):
            self.flush()
            if time.monotonic() - last_log >= HEARTBEAT_SECONDS - LOG_FLUSH_SECONDS:
                last_log = time.monotonic()
                try:
                    if self.coord.call("/v1/heartbeat", {"job_id": self.job["id"], "stats": stats(self.cfg)}).get("cancel"):
                        self.cancelled = True
                        self.kill()
                except Exception as e:
                    self.log(f"heartbeat failed: {e}")

    def kill(self) -> None:
        p = self.proc
        if p and p.poll() is None:
            if WINDOWS:
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True)
            else:
                p.kill()

    def run_step(self, name: str, argv: list[str], cwd: Path, env: dict | None = None, deadline: float = 0) -> int:
        self.out(f"\n##[step] {name}\n$ {' '.join(argv)}")
        flags = BELOW_NORMAL if WINDOWS else 0
        try:
            self.proc = subprocess.Popen(argv, cwd=str(cwd), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         stdin=subprocess.DEVNULL, creationflags=flags)
        except OSError as e:
            self.out(f"could not start: {e}")
            return 127
        timer = None
        if deadline:
            timer = threading.Timer(max(1.0, deadline - time.monotonic()), self._timeout)
            timer.daemon = True
            timer.start()
        with self.proc.stdout:
            for raw in self.proc.stdout:
                self.out(raw.decode("utf-8", "replace").rstrip("\r\n"))
        code = self.proc.wait()
        if timer:
            timer.cancel()
        self.out(f"##[step] {name}: exit {code}")
        return code

    def _timeout(self) -> None:
        self.out("##[error] the job ran past its time limit; stopping it")
        self.kill()

    def checkout(self, env: dict) -> int:
        ws = _workspace(self.cfg)
        sha = self.job["sha"]
        if not (ws / ".git").exists():
            ws.mkdir(parents=True, exist_ok=True)
            for argv in (["git", "init", "-q"], ["git", "remote", "add", "origin", self.cfg["repoUrl"]],
                         ["git", "config", "core.longpaths", "true"]):
                if self.run_step(" ".join(argv[:2]), argv, ws, env):
                    return 1
        else:
            subprocess.run(["git", "remote", "set-url", "origin", self.cfg["repoUrl"]], cwd=ws, env=env, capture_output=True)
        steps = [("Fetch the commit", ["git", "fetch", "--no-tags", "origin", sha]),
                 ("Check out the commit", ["git", "checkout", "--force", "--detach", sha]),
                 ("Fetch LFS files", ["git", "lfs", "pull"])]
        for name, argv in steps:
            if argv[1] == "lfs" and not shutil.which("git-lfs"):
                self.out("git-lfs is not installed; skipping LFS files")
                continue
            code = self.run_step(name, argv, ws, env)
            if code:
                return code
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ws, capture_output=True, text=True).stdout.strip()
        if head != sha:
            self.out(f"##[error] checked out {head}, expected {sha}")
            return 1
        return 0

    def run(self) -> tuple[int, str, object]:
        job, cfg = self.job, self.cfg
        if job.get("recipe") not in set(cfg.get("recipes") or []):
            return 2, f"This node does not run {job.get('recipe')!r}", None
        if not SHA.match(str(job.get("sha") or "")):
            return 2, "The job has no valid commit SHA", None
        beat = threading.Thread(target=self._beat, daemon=True)
        beat.start()
        tmp = Path(tempfile.mkdtemp(prefix=f"fletchery-job-{job['id']}-"))
        result_json = tmp / "node-result.json"
        deadline = time.monotonic() + float(job.get("timeout_minutes") or 120) * 60
        env = _git_env(cfg)
        code, summary = 0, ""
        try:
            code = self.checkout(env)
            if code:
                summary = "Checkout failed"
            else:
                ws = _workspace(cfg)
                job_env = dict(env)
                job_env["FLETCHERY_JOB"] = str(job["id"])
                for name, step, always in recipe_steps(job["recipe"], job.get("params") or {}, ws, cfg, result_json):
                    if (code or self.cancelled) and not always:
                        continue
                    argv = step() if callable(step) else step
                    rc = self.run_step(name, argv, ws, job_env, deadline if not always else 0)
                    if rc and not code and not always:
                        code, summary = rc, f"{name} failed (exit {rc})"
                if self.cancelled:
                    code, summary = code or 130, "Cancelled from Fletchery"
                elif time.monotonic() > deadline and code:
                    summary = "Timed out"
        except Exception as e:
            code, summary = 1, f"Worker error: {type(e).__name__}: {e}"
            self.out(summary)
        finally:
            self.done.set()
            beat.join(timeout=5)
            self.flush()
        result = None
        try:
            result = json.loads(result_json.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            pass
        shutil.rmtree(tmp, ignore_errors=True)
        if not summary:
            summary = _summary(result) or ("Passed" if code == 0 else f"Failed (exit {code})")
        return code, summary, result


def _summary(result) -> str:
    rows = (result or {}).get("results") if isinstance(result, dict) else None
    if not isinstance(rows, list) or not rows:
        return ""
    failed = [str(r.get("name")) for r in rows if isinstance(r, dict) and not r.get("pass")]
    where = f" on {result.get('machine')}" if result.get("machine") else ""
    if failed:
        return f"{len(failed)} of {len(rows)} failed{where}: {', '.join(failed[:5])}"
    return f"{len(rows)} checks passed{where}"


# ---- the loop ----
def load_config(path: str) -> dict:
    cfg = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    for k in ("coordinator", "machine", "keyFile", "workRoot", "repoUrl"):
        if not cfg.get(k):
            raise SystemExit(f"{path}: `{k}` is missing")
    cfg.setdefault("recipes", ["node-checks", "nightly-full", "checkout-only"])
    return cfg


def _logger(cfg: dict):
    path = Path(cfg.get("logFile") or Path(cfg["workRoot"]) / "fletchery" / "worker.log")
    path.parent.mkdir(parents=True, exist_ok=True)

    def log(msg: str) -> None:
        line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}"
        print(line, flush=True)
        try:
            if path.exists() and path.stat().st_size > 5 * 2**20:
                path.replace(path.with_suffix(".old.log"))
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass
    return log


def hello(coord: Coordinator, cfg: dict) -> dict:
    total, _ = _memory()
    return coord.call("/v1/hello", {"labels": cfg.get("labels") or [], "ram_gb": round(total / 2**30, 1),
                                    "version": VERSION, "stats": stats(cfg)})


def run(cfg: dict, once: bool = False) -> int:
    log = _logger(cfg)
    if WINDOWS:  # children inherit below-normal priority
        import ctypes
        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), BELOW_NORMAL)
    coord = Coordinator(cfg)
    poll, said = 15, ""
    while True:
        try:
            poll = int(hello(coord, cfg).get("poll_seconds") or 15)
            if said:
                log("connected to the coordinator again")
            said = ""
            while True:
                st = stats(cfg)
                # While the pause file exists the node still checks in (it shows as paused) but takes no job.
                job = coord.call("/v1/claim", {"stats": st, "paused": bool(st.get("paused"))}).get("job")
                if job:
                    log(f"job {job['id']}: {job['recipe']} at {job['sha'][:10]}")
                    code, summary, result = Job(coord, cfg, job, log).run()
                    log(f"job {job['id']}: exit {code}: {summary}")
                    for attempt in range(10):
                        try:
                            coord.call("/v1/result", {"job_id": job["id"], "exit_code": code, "summary": summary,
                                                      "result": result, "stats": stats(cfg)})
                            break
                        except Exception as e:
                            log(f"result not sent yet: {e}")
                            time.sleep(min(60, 5 * (attempt + 1)))
                    if once:
                        return code
                    continue
                if once:
                    return 0
                time.sleep(poll)
        except Exception as e:
            msg = f"coordinator unreachable: {e}"
            if msg != said:
                log(msg)
                said = msg
            if once:
                return 1
            time.sleep(30)


def install(config: str) -> int:
    if not WINDOWS:
        raise SystemExit("install is for Windows (a logon task)")
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    runner = pyw if pyw.exists() else exe
    tr = f'"{runner}" "{Path(__file__).resolve()}" run --config "{Path(config).resolve()}"'
    p = subprocess.run(["schtasks", "/Create", "/F", "/TN", TASK_NAME, "/SC", "ONLOGON", "/RL", "LIMITED",
                        "/DELAY", "0000:30", "/TR", tr], capture_output=True, text=True)
    print(p.stdout or p.stderr)
    if p.returncode == 0:
        print(f"Installed. It starts at your next sign-in; start it now with: schtasks /Run /TN \"{TASK_NAME}\"")
    return p.returncode


def uninstall() -> int:
    p = subprocess.run(["schtasks", "/Delete", "/F", "/TN", TASK_NAME], capture_output=True, text=True)
    print(p.stdout or p.stderr)
    return p.returncode


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Fletchery node worker")
    ap.add_argument("command", choices=["check", "run", "once", "install", "uninstall"])
    ap.add_argument("--config", default="")
    a = ap.parse_args(argv)
    if a.command == "uninstall":
        return uninstall()
    if not a.config:
        raise SystemExit("--config is required")
    cfg = load_config(a.config)
    if a.command == "install":
        return install(a.config)
    if a.command == "check":
        print(json.dumps({"hello": hello(Coordinator(cfg), cfg), "stats": stats(cfg)}, indent=2))
        return 0
    return run(cfg, once=a.command == "once")


if __name__ == "__main__":
    raise SystemExit(main())
