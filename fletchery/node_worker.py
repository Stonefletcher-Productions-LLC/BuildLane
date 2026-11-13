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
     "gitPushKey": "C:\\\\StoneNode\\\\keys\\\\lifeshare_push",            # optional write deploy key: lets merge rounds import
                                                                         # content here and push it to fletchery/round-N only
     "engine": "E:\\\\UE_5.8",
     "pauseFile": "E:\\\\GitHubRunner\\\\PAUSED-NODE",                    # while it exists, take no new job
     "buildsDir": "E:\\\\fletchery-builds",
     "heavyLeaseFile": "E:\\\\GitHubRunner\\\\ResourceLeases\\\\heavy.json",  # optional, for a node on a PC that also
                                                                         # runs Unreal itself: hold the PC's heavy lease per job                            # optional: where `package` writes (default <workRoot>\\builds)
     "recipes": ["node-checks", "nightly-full", "checkout-only", "pr-check", "package"],  # what this node agrees to run
     "assetMap": {"E:\\\\lifeshare-scratch\\\\meshy_export": "D:\\\\assets\\\\meshy_export"}}  # optional: mirror a
                                                     # shared PC folder somewhere else (default: the same path here)

Asset folders: the PC shares workers.machines.assetRoots (e.g. E:\\lifeshare-scratch\\meshy_export, which LifeShare's
imports read by absolute path). The worker mirrors them to the same path here before every import job, and also when it
starts and every ASSET_IDLE_SECONDS while idle, so a new node fills itself. Only new or changed files are copied (size +
SHA-256), each one checked before it replaces the old copy; nothing here is ever deleted.

    python node_worker.py check   --config <file>     sign in to the coordinator once and print what it sees
    python node_worker.py run     --config <file>     the worker loop (what the logon task runs)
    python node_worker.py install --config <file>     a Windows logon task that starts `run` (current user, limited)
    python node_worker.py uninstall
    python node_worker.py sync    --config <file>     mirror the PC's shared asset folders now
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

VERSION = "3"   # 2: mirrors the PC's asset folders (label cap:asset-sync); 3: clears stale git locks, backs off on failed checkouts
ASSET_SYNC_LABEL = "cap:asset-sync"
ASSET_IDLE_SECONDS = 15 * 60
ASSET_INDEX = ".fletchery-assets.json"
ASSET_PART = ".fletchery-part"
ASSET_MIN_FREE_GB = 10
TASK_NAME = "Fletchery node worker"
HEARTBEAT_SECONDS = 30
LOG_FLUSH_SECONDS = 5
SHA = re.compile(r"^[0-9a-f]{40}$")
ONLY = re.compile(r"^[A-Za-z0-9_]+(,[A-Za-z0-9_]+)*$")
# Content import steps a merge round names (Tools/<Name>.ps1 [-Map <map>]) and the only branches imports may push to.
IMPORT_LIST = re.compile(r"^[A-Za-z0-9_]+(:[A-Za-z0-9_./-]+)?(,[A-Za-z0-9_]+(:[A-Za-z0-9_./-]+)?)*$")
ROUND_BRANCH = re.compile(r"^fletchery/round-[0-9]{1,6}$")
BUILD_VERSION = re.compile(r"^(0|[1-9]\d{0,5})\.(0|[1-9]\d{0,5})\.(0|[1-9]\d{0,5})$")
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


def recipe_steps(recipe: str, params: dict, ws: Path, cfg: dict, result_json: Path,
                 sha: str = "") -> list[tuple[str, object, bool]]:
    """(name, argv or a callable giving argv, always) for each step, mirroring node-checks.yml / nightly-node.yml.
    `always` steps run even after a failure (clean-up)."""
    engine = cfg.get("engine") or r"E:\UE_5.8"
    if recipe == "checkout-only":
        return [("Show the commit", ["git", "log", "-1", "--format=%H %s"], False)]
    if recipe == "pr-check":
        return pr_check_steps(params, ws, sha)
    if recipe == "package":
        return package_steps(params, ws, engine, result_json)
    if recipe not in ("node-checks", "nightly-full"):
        raise ValueError(f"unknown recipe {recipe!r}")
    tests = ["-Engine", engine, "-ResultJson", str(result_json)]
    imports: list[tuple[str, object, bool]] = []
    if recipe == "node-checks":
        only = str(params.get("only") or "")
        if only and not ONLY.match(only):
            raise ValueError("bad `only`")
        if only:
            tests += ["-Only", only]
        if params.get("strict_slots") is True:
            tests.append("-StrictSlots")
        imports = import_steps(params, ws, engine, cfg)
    return [
        ("Clear stale processes from this checkout", lambda: _stop_processes(ws), False),
        ("Reset generated Content from prior jobs", _ps("Tools/CI/ResetContentWorkspace.ps1", ws, []), False),
        *imports,
        ("Build, BuildContent and tests", _ps("Tools/CI/NodeTests.ps1", ws, tests), False),
        ("Stop what this job left running", lambda: _stop_processes(ws), True),
        ("Reset Content after the run", ["git", "checkout", "--", "Content/Sim"], True),
        ("Reset Content workspace", _ps("Tools/CI/ResetContentWorkspace.ps1", ws, []), True),
    ]


IMPORT_STEP = "Content imports"


def import_steps(params: dict, ws: Path, engine: str, cfg: dict) -> list[tuple[str, object, bool]]:
    """A merge round with Lane-Imports PRs (Alex, 2026-11-12: let the node push imported content): build the editor,
    run the named imports with the project's Tools/CI/RunImports.ps1, which commits the content and pushes it with
    PushWithLfs.ps1 to the round's own throwaway branch. The push uses the node's write key (`gitPushKey`); without
    one the node refuses import jobs. Only fletchery/round-N is accepted, and PushWithLfs itself refuses main."""
    imports = str(params.get("imports") or "")
    if not imports:
        return []
    branch = str(params.get("push_branch") or "")
    if not IMPORT_LIST.match(imports):
        raise ValueError("bad `imports`")
    if not ROUND_BRANCH.match(branch):
        raise ValueError("imports push only to fletchery/round-N")
    if not cfg.get("gitPushKey"):
        raise ValueError("this node has no gitPushKey, so it cannot push imported content")
    return [
        ("Build the editor for the imports", _ps("Tools/Build.ps1", ws, ["-Engine", engine, "-AllowLaneRunning"]), False),
        (IMPORT_STEP, _ps("Tools/CI/RunImports.ps1", ws, ["-Imports", imports, "-Branch", branch]), False),
    ]


MERGE_STEP = "Merge the PR onto the base"
CHECK_STEP = "PR check"


def pr_check_steps(params: dict, ws: Path, base: str) -> list[tuple[str, object, bool]]:
    """The PC check (Fletchery prcheck.py) on a node: the job's commit is the base branch tip, already checked out; merge
    the PR head onto it and run the project's check against that base, like the PC does in its build checkout.
    Tools/CI/PrCheck.ps1 exits 0 pass, 1 fail, 2 no result; Fletchery labels the PR or posts the failure."""
    pr, head = params.get("pr"), str(params.get("head") or "")
    if not isinstance(pr, int) or pr <= 0 or not SHA.match(head) or not SHA.match(base or ""):
        raise ValueError("bad pr-check parameters")
    ident = ["-c", "user.name=Fletchery node", "-c", "user.email=fletchery-node@localhost"]
    return [
        ("Fetch the PR head", ["git", "fetch", "--no-tags", "origin", head], False),
        ("Clear stale processes from this checkout", lambda: _stop_processes(ws), False),
        ("Reset generated Content from prior jobs", _ps("Tools/CI/ResetContentWorkspace.ps1", ws, []), False),
        (MERGE_STEP, ["git", *ident, "merge", "--no-ff", "--no-edit", "-m", f"PR check: merge PR #{pr} ({head[:10]})", head],
         False),
        (CHECK_STEP, _ps("Tools/CI/PrCheck.ps1", ws, ["-BaseRef", base]), False),
        ("Stop what this job left running", lambda: _stop_processes(ws), True),
        ("Abort an unfinished merge", ["git", "merge", "--abort"], True),
        ("Reset Content workspace", _ps("Tools/CI/ResetContentWorkspace.ps1", ws, []), True),
    ]


def package_out(builds_dir, name: str, version: str, sha: str) -> Path:
    """Where a packaged build goes: <builds>\\<Name>-<version>, or with the commit added when that folder is taken (a
    nightly that keeps one version, or the same version built again)."""
    root = Path(builds_dir)
    for folder in (f"{name}-{version}", f"{name}-{version}-{sha[:10]}"):
        if not (root / folder).exists():
            return root / folder
    n = 2
    while (root / f"{name}-{version}-{sha[:10]}-{n}").exists():
        n += 1
    return root / f"{name}-{version}-{sha[:10]}-{n}"


def package_steps(params: dict, ws: Path, engine: str, result_json: Path | None = None) -> list[tuple[str, object, bool]]:
    """A packaged build of whatever is at the commit: editor build and BuildContent (or the Full tests, which do both),
    then Tools/Package.ps1 with the chosen version, then the same clean-up as the test recipes so the next job (or merge
    round, on the PC) starts from a clean checkout. Shared by the node and the PC (builds.py)."""
    version = str(params.get("version") or "")
    if not BUILD_VERSION.match(version):
        raise ValueError("bad `version`")
    out = str(params.get("out_dir") or "")
    if not out:
        raise ValueError("no output folder")
    steps: list[tuple[str, object, bool]] = [
        ("Clear stale processes from this checkout", lambda: _stop_processes(ws), False),
        ("Reset generated Content from prior jobs", _ps("Tools/CI/ResetContentWorkspace.ps1", ws, []), False),
    ]
    if params.get("tests") == "full":
        tests = ["-Engine", engine] + (["-ResultJson", str(result_json)] if result_json else [])
        steps.append(("Build, BuildContent and Full tests", _ps("Tools/CI/NodeTests.ps1", ws, tests), False))
    else:
        steps += [("Build the editor", _ps("Tools/Build.ps1", ws, ["-Engine", engine, "-AllowLaneRunning"]), False),
                  ("BuildContent", _ps("Tools/BuildContent.ps1", ws, ["-Engine", engine, "-AllowLaneRunning"]), False)]
    steps += [
        (f"Package {version}", _ps("Tools/Package.ps1", ws, ["-Engine", engine, "-Version", version, "-Out", out,
                                                            "-AllowLaneRunning"]), False),
        ("Stop what this job left running", lambda: _stop_processes(ws), True),
        ("Reset Content after the run", ["git", "checkout", "--", "Content/Sim"], True),
        ("Reset Content workspace", _ps("Tools/CI/ResetContentWorkspace.ps1", ws, []), True),
    ]
    return steps


# ---- the PC heavy lease (a node that shares a PC with other Unreal work) ----
# Alex, 2026-11-12: the Gaming PC is also a node. Its job scripts run with -AllowLaneRunning, so the worker itself takes
# the PC's heavy lease (LifeShare Tools/CI/HeavyLease.ps1, same file and record shape as a LocalAcquire) for each job,
# and claims nothing while someone else holds it. A PC round or a local editor run then waits for the job, not the other
# way round. processStartUtc is left out on purpose: HeavyLease then counts the hold live while this worker's PID lives.

def lease_holder(cfg: dict) -> str:
    """Who else holds the heavy lease now ("" when it is free or not configured)."""
    path = cfg.get("heavyLeaseFile")
    if not path or not Path(path).exists():
        return ""
    try:
        rec = json.loads(Path(path).read_text(encoding="utf-8-sig") or "{}")
    except (OSError, ValueError):
        return "an unreadable lease"
    if rec.get("token") and rec.get("token") == cfg.get("_lease_token"):
        return ""
    return str(rec.get("purpose") or rec.get("runId") or "another run")[:120]


def take_lease(cfg: dict, job: dict, ws: Path, wait: float = 120, sleep=time.sleep) -> str:
    """Create the lease for this job (atomic, like HeavyLease.ps1's CreateNew); wait briefly if another run took it
    between the claim and now. Returns the token, or "" when the job runs without it."""
    path = cfg.get("heavyLeaseFile")
    if not path:
        return ""
    import uuid
    from datetime import datetime, timezone
    token = uuid.uuid4().hex
    rec = {"schemaVersion": 1, "owner": "local", "runId": "local", "attempt": "0", "token": token, "pid": os.getpid(),
           "acquiredUtc": datetime.now(timezone.utc).isoformat(), "host": os.environ.get("COMPUTERNAME", ""),
           "user": os.environ.get("USERNAME", ""), "workspaces": str(ws),
           "purpose": f"Fletchery node job {job.get('id')}: {job.get('recipe')} at {str(job.get('sha', ''))[:10]}"}
    end = time.monotonic() + wait
    while True:
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            with open(path, "x", encoding="utf-8") as f:
                f.write(json.dumps(rec))
            cfg["_lease_token"] = token
            return token
        except FileExistsError:
            if time.monotonic() > end:
                return ""
            sleep(5)


def drop_lease(cfg: dict, token: str) -> None:
    """Remove the lease only if it is still this job's."""
    path = cfg.get("heavyLeaseFile")
    if not path or not token:
        return
    try:
        if json.loads(Path(path).read_text(encoding="utf-8-sig") or "{}").get("token") == token:
            Path(path).unlink()
    except (OSError, ValueError):
        pass
    cfg.pop("_lease_token", None)


# ---- machine stats ----
def stats(cfg: dict) -> dict:
    out = {"paused": bool(cfg.get("pauseFile")) and Path(cfg["pauseFile"]).exists()}
    if out["paused"]:
        out["pause_file"] = True   # paused by a person: the coordinator never counts this node as free
    holder = lease_holder(cfg)
    if holder:  # the PC is busy with other Unreal work: show as paused and take no job until it is free
        out["paused"], out["lease"] = True, holder
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


# ---- copying a packaged build back to the PC ----
UPLOAD_CHUNK = 4 * 1024 * 1024


def upload_build(coord, job_id: int, folder, log=print, chunk: int = UPLOAD_CHUNK) -> str:
    """Zip a packaged build folder and send it to the coordinator in chunks (Alex, 2026-11-12: builds made on the node
    must land on the PC). Returns where the PC unpacked it, or "" when the copy failed (the build stays on the node)."""
    import base64
    import hashlib
    import zipfile
    folder = Path(folder)
    if not folder.is_dir():
        log(f"upload: {folder} is not a folder")
        return ""
    zpath = folder.parent / (folder.name + ".zip")
    try:
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as z:
            for f in sorted(folder.rglob("*")):
                if f.is_file():
                    z.write(f, f.relative_to(folder).as_posix())
        h = hashlib.sha256()
        with open(zpath, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        total, digest = zpath.stat().st_size, h.hexdigest()
        log(f"upload: {zpath.name} {total / 2**30:.2f} GiB to the PC")
        offset, tries, answer = 0, 0, {}
        with open(zpath, "rb") as f:
            while True:
                f.seek(offset)
                data = f.read(chunk)
                last = offset + len(data) >= total
                try:
                    answer = coord.call("/v1/upload", {"job_id": job_id, "name": zpath.name, "offset": offset,
                                                       "data": base64.b64encode(data).decode("ascii"), "done": last,
                                                       "sha256": digest if last else ""}, timeout=300)
                    tries = 0
                except Exception as e:
                    tries += 1
                    if tries > 5:
                        log(f"upload: gave up: {e}")
                        return ""
                    time.sleep(5 * tries)
                    continue
                if answer.get("ok") is False:
                    offset = int(answer.get("offset") or 0)  # the PC has a different amount: carry on from there
                    continue
                if last:
                    return str(answer.get("path") or "")
                offset += len(data)
    except Exception as e:
        log(f"upload: failed: {type(e).__name__}: {e}")
        return ""
    finally:
        try:
            zpath.unlink()
        except OSError:
            pass


# ---- mirroring the PC's asset folders (Alex, 2026-11-13: a permanent way to share files with every node) ----
class AssetSyncError(RuntimeError):
    pass


def _file_sha(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _safe_rel(rel: str) -> bool:
    parts = Path(rel).parts
    return bool(rel) and not Path(rel).is_absolute() and ".." not in parts and not rel.startswith(("/", "\\")) \
        and ":" not in rel


def sync_assets(coord, cfg: dict, log=print) -> dict:
    """Mirror every folder the PC shares (workers.machines.assetRoots) to the same path on this node (or cfg assetMap).
    Copies only files that are missing or differ, into <name>.fletchery-part, checks the SHA-256, then swaps it in.
    Raises AssetSyncError when the PC is missing a folder or a copy fails, so an import job stops before it imports.
    Returns {"roots": n, "copied": n, "bytes": n}."""
    import base64
    import socket
    try:
        listing = coord.call("/v1/assets/list", {}, timeout=900)
    except RuntimeError as e:
        if "HTTP 404" in str(e):   # an older Fletchery on the PC: it shares nothing yet
            log("assets: the PC's Fletchery does not share asset folders yet; nothing to mirror")
            return {"roots": 0, "copied": 0, "bytes": 0}
        raise AssetSyncError(f"could not list the PC's asset folders: {e}") from None
    if listing.get("missing"):
        raise AssetSyncError("the PC is missing shared asset folder(s): " + ", ".join(map(str, listing["missing"])))
    same_host = str(listing.get("host") or "").lower() == socket.gethostname().lower()
    amap = {str(k).lower(): str(v) for k, v in (cfg.get("assetMap") or {}).items()}
    copied = total = 0
    roots = listing.get("roots") or []
    for entry in roots:
        root = str(entry.get("root") or "")
        dest = Path(amap.get(root.lower()) or root)
        if same_host and str(dest).lower() == root.lower():
            continue   # the worker on the PC itself: the folder is already here
        files = entry.get("files") or []
        need = []
        dest.mkdir(parents=True, exist_ok=True)
        index_path = dest / ASSET_INDEX
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            index = {}
        for rel, size, sha in files:
            rel = str(rel)
            if not _safe_rel(rel):
                raise AssetSyncError(f"refused an unsafe path from the PC: {rel}")
            local = dest / rel
            if local.is_file():
                st = local.stat()
                if st.st_size == size:
                    known = index.get(rel)
                    if known and known[0] == size and known[1] == st.st_mtime_ns and known[2] == sha:
                        continue
                    if _file_sha(local) == sha:
                        index[rel] = [size, st.st_mtime_ns, sha]
                        continue
            need.append((rel, int(size), str(sha)))
        want = sum(n[1] for n in need)
        if need:
            free = shutil.disk_usage(dest).free
            if free - want < ASSET_MIN_FREE_GB * 2**30:
                raise AssetSyncError(f"{dest} needs {want / 2**30:.1f} GB but its drive has {free / 2**30:.1f} GB free")
            log(f"assets: {root}: copying {len(need)} file(s), {want / 2**20:.0f} MB, to {dest}")
        for rel, size, sha in need:
            local = dest / rel
            local.parent.mkdir(parents=True, exist_ok=True)
            part = local.with_name(local.name + ASSET_PART)
            try:
                with open(part, "wb") as f:
                    offset = 0
                    while offset < size:
                        answer = coord.call("/v1/assets/read", {"root": root, "path": rel, "offset": offset}, timeout=120)
                        data = base64.b64decode(answer.get("data") or "")
                        if not data:
                            break
                        f.write(data)
                        offset += len(data)
                if part.stat().st_size != size or _file_sha(part) != sha:
                    raise AssetSyncError(f"{rel}: the copy does not match the PC's file (it changed while copying?)")
                os.replace(part, local)
            except AssetSyncError:
                part.unlink(missing_ok=True)
                raise
            except Exception as e:
                part.unlink(missing_ok=True)
                raise AssetSyncError(f"{rel}: {type(e).__name__}: {e}") from None
            index[rel] = [size, local.stat().st_mtime_ns, sha]
            copied += 1
            total += size
        index_path.write_text(json.dumps(index), encoding="utf-8")
    return {"roots": len(roots), "copied": copied, "bytes": total}


# ---- running a job ----
def _git_env(cfg: dict) -> dict:
    env = dict(os.environ)
    key = cfg.get("gitSshKey")
    if key:
        env["GIT_SSH_COMMAND"] = f'ssh -i "{key}" -o IdentitiesOnly=yes -o BatchMode=yes'
    env["GIT_TERMINAL_PROMPT"] = "0"
    # On the Gaming PC the worker holds the PC's heavy lease for the job. LifeShare's LaneGuard lets a guarded script
    # run under a hold whose token is in LS_LANE_HOLD, so the job's own Build/Test steps don't refuse with "the PC is
    # in use by Fletchery node job N" (exit 2 on every job).
    if cfg.get("_lease_token"):
        env["LS_LANE_HOLD"] = cfg["_lease_token"]
        if cfg.get("heavyLeaseFile"):
            env["LS_HEAVY_LEASE"] = str(cfg["heavyLeaseFile"])
    return env


STALE_LOCK_SECONDS = 120     # a git lock this old, before this worker's own checkout starts, is a leftover
CHECKOUT_BACKOFF = (300, 900, 1800)   # after 2, 3, 4+ failed checkouts in a row: take no job for this long


def clear_stale_git_locks(ws: Path, min_age: float = STALE_LOCK_SECONDS, now: float | None = None) -> list[str]:
    """Remove git lock files a killed job left in this worker's own checkout. The worker runs one job at a time and
    calls this before its first git command, so no git of its own can hold them. On 2026-11-13 a cancelled job left
    .git/index.lock behind and every job on GAMING-PC failed its checkout for 5.5 hours (37 failures, one PR 35 times).
    Locks younger than `min_age` are left alone in case someone else is working in the checkout by hand."""
    git = ws / ".git"
    if not git.is_dir():
        return []
    now = time.time() if now is None else now
    found = [git / n for n in ("index.lock", "HEAD.lock", "shallow.lock", "packed-refs.lock", "config.lock",
                                "FETCH_HEAD.lock", "ORIG_HEAD.lock")]
    refs = git / "refs"
    if refs.is_dir():
        found += list(refs.rglob("*.lock"))
    gone = []
    for path in found:
        try:
            if path.is_file() and now - path.stat().st_mtime >= min_age:
                path.unlink()
                gone.append(str(path.relative_to(ws)))
        except OSError:
            pass
    return gone


def checkout_backoff(failures: int) -> int:
    """Seconds to take no job after `failures` failed checkouts in a row (0 for none or one)."""
    if failures < 2:
        return 0
    return CHECKOUT_BACKOFF[min(failures - 2, len(CHECKOUT_BACKOFF) - 1)]


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
        self.pc_copy = ""

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
            gone = clear_stale_git_locks(ws)
            if gone:
                self.out(f"Removed stale git lock(s) left by an earlier job: {', '.join(gone)}")
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
        code, summary, out_dir = 0, "", ""
        try:
            code = self.checkout(env)
            if code:
                summary = "Checkout failed"
            else:
                ws = _workspace(cfg)
                job_env = dict(env)
                job_env["FLETCHERY_JOB"] = str(job["id"])
                params = dict(job.get("params") or {})
                if params.get("imports") and cfg.get("gitPushKey"):  # only an import job's steps get the write key
                    job_env["GIT_SSH_COMMAND"] = (f'ssh -i "{cfg["gitPushKey"]}" -o IdentitiesOnly=yes -o BatchMode=yes')
                if job["recipe"] == "package":  # the node picks the folder; the coordinator never names a path
                    params["out_dir"] = str(package_out(cfg.get("buildsDir") or Path(cfg["workRoot"]) / "builds",
                                                        "LifeShare", str(params.get("version") or ""), job["sha"]))
                    out_dir = params["out_dir"]
                if params.get("imports"):   # an import reads the PC's asset folders: mirror them first, or stop here
                    self.out("\n##[step] Mirror the PC's asset folders")
                    try:
                        got = sync_assets(self.coord, cfg, self.out)
                        self.out(f"{got['roots']} shared folder(s) up to date; copied {got['copied']} file(s), "
                                 f"{got['bytes'] / 2**20:.0f} MB")
                        self.out("##[step] Mirror the PC's asset folders: exit 0")
                    except AssetSyncError as e:
                        self.out(f"##[error] {e}")
                        self.out("##[step] Mirror the PC's asset folders: exit 1")
                        code, summary = 1, f"Asset folders not mirrored, imports not started: {e}"
                for name, step, always in recipe_steps(job["recipe"], params, ws, cfg, result_json, job["sha"]):
                    if (code or self.cancelled) and not always:
                        continue
                    argv = step() if callable(step) else step
                    rc = self.run_step(name, argv, ws, job_env, deadline if not always else 0)
                    if rc and not code and not always:
                        code, summary = rc, f"{name} failed (exit {rc})"
                if self.cancelled:
                    code, summary = code or 130, "Cancelled from Fletchery"
                elif out_dir and code == 0 and cfg.get("uploadBuilds", True):
                    self.out(f"==> Copy the build to the PC")
                    self.pc_copy = upload_build(self.coord, int(job["id"]), out_dir, self.out)
                    self.out(f"Copied to the PC: {self.pc_copy}" if self.pc_copy else "Not copied; the build stays here")
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
        if not summary and out_dir and code == 0:
            summary = f"Packaged to {out_dir} on {cfg.get('machine', 'the node')}"
            if self.pc_copy:
                summary += f"; copied to {self.pc_copy} on the PC"
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
    cfg.setdefault("recipes", ["node-checks", "nightly-full", "checkout-only", "pr-check"])
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
    labels = list(cfg.get("labels") or [])
    if ASSET_SYNC_LABEL not in labels:
        labels.append(ASSET_SYNC_LABEL)
    return coord.call("/v1/hello", {"labels": labels, "ram_gb": round(total / 2**30, 1),
                                    "version": VERSION, "stats": stats(cfg)})


def run(cfg: dict, once: bool = False) -> int:
    log = _logger(cfg)
    if WINDOWS:  # children inherit below-normal priority
        import ctypes
        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), BELOW_NORMAL)
    coord = Coordinator(cfg)
    poll, said = 15, ""
    bad_checkouts, rest_until = 0, 0.0   # failed checkouts in a row; take no job until rest_until (monotonic)
    next_sync = 0.0   # mirror the asset folders as soon as the worker starts: a new node fills itself
    while True:
        try:
            poll = int(hello(coord, cfg).get("poll_seconds") or 15)
            if said:
                log("connected to the coordinator again")
            said = ""
            while True:
                st = stats(cfg)
                if time.monotonic() < rest_until:  # checkouts keep failing here: check in as paused, take no job
                    st["paused"], st["checkout_failing"] = True, bad_checkouts
                # While the pause file exists the node still checks in (it shows as paused) but takes no job.
                job = coord.call("/v1/claim", {"stats": st, "paused": bool(st.get("paused"))}).get("job")
                if job:
                    log(f"job {job['id']}: {job['recipe']} at {job['sha'][:10]}")
                    token = take_lease(cfg, job, _workspace(cfg))
                    if cfg.get("heavyLeaseFile") and not token:
                        log(f"job {job['id']}: the heavy lease stayed taken ({lease_holder(cfg)}); running without it")
                    try:
                        code, summary, result = Job(coord, cfg, job, log).run()
                    finally:
                        drop_lease(cfg, token)
                    log(f"job {job['id']}: exit {code}: {summary}")
                    bad_checkouts = bad_checkouts + 1 if summary == "Checkout failed" else 0
                    rest = checkout_backoff(bad_checkouts)
                    if rest:
                        rest_until = time.monotonic() + rest
                        log(f"{bad_checkouts} checkouts in a row failed; taking no job for {rest // 60} min")
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
                if not st.get("paused") and time.monotonic() >= next_sync:
                    next_sync = time.monotonic() + ASSET_IDLE_SECONDS
                    try:
                        got = sync_assets(coord, cfg, log)
                        if got["copied"]:
                            log(f"assets: copied {got['copied']} file(s), {got['bytes'] / 2**20:.0f} MB")
                    except Exception as e:   # an import job will try again and fail clearly if it still can't
                        log(f"assets: {e}")
                    continue
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
    ap.add_argument("command", choices=["check", "run", "once", "install", "uninstall", "upload", "sync"])
    ap.add_argument("--config", default="")
    ap.add_argument("--job", type=int, default=0, help="upload: the package job that made the build")
    ap.add_argument("--folder", default="", help="upload: the packaged build folder on this node")
    a = ap.parse_args(argv)
    if a.command == "uninstall":
        return uninstall()
    if not a.config:
        raise SystemExit("--config is required")
    cfg = load_config(a.config)
    if a.command == "install":
        return install(a.config)
    if a.command == "upload":  # send a build packaged earlier (before builds were copied back) to the PC
        where = upload_build(Coordinator(cfg), a.job, a.folder)
        print(f"copied to {where} on the PC" if where else "not copied")
        return 0 if where else 1
    if a.command == "sync":
        try:
            got = sync_assets(Coordinator(cfg), cfg)
        except AssetSyncError as e:
            print(f"not mirrored: {e}")
            return 1
        print(f"{got['roots']} shared folder(s) up to date; copied {got['copied']} file(s), {got['bytes'] / 2**20:.0f} MB")
        return 0
    if a.command == "check":
        print(json.dumps({"hello": hello(Coordinator(cfg), cfg), "stats": stats(cfg)}, indent=2))
        return 0
    return run(cfg, once=a.command == "once")


if __name__ == "__main__":
    raise SystemExit(main())
