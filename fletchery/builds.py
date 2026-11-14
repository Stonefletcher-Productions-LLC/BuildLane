"""Scheduled builds: pick a version and a time, and Fletchery packages whatever is on the branch at that time.

Building -> Builds adds a row: version (0.3.9), branch (the base branch unless you name another), when (now or a time),
once or every day, with or without the Full tests first, and where it runs (automatic, the node, or this PC). Rows live
in state.json (`builds`), so a restart keeps them.

What happens to a row:

    scheduled  waiting for its time. A daily row stays here: at its time it starts a one-off copy and moves on a day.
    waiting    its time came: the branch was read once (`sha` is what gets built, whatever later lands) and the build
               waits for a machine. Automatic sends it to a test node when one that can package (labels os:windows and
               role:cook) is online, otherwise to this PC.
    queued     a node job is queued (machines.py `package` recipe); it starts when the node is free.
    running    a node or this PC is building it.
    done / failed / cancelled

On this PC a build uses the same checkout as merge rounds and PC checks (incremental builds), so it takes the merge slot:
a running round or check finishes first, then the build starts before the next one. It also waits for the PC's heavy
lease, like a round. Afterwards it resets generated Content so the next round starts clean. Packages go to
`workers.builds.outDir` (default <state dir>\\builds), as <Name>-<version>, with the commit added when that folder
already exists. A node writes to its own `buildsDir`.

Profile (all optional), in `workers`:
    "builds": {"outDir": "E:\\\\lifeshare-scratch\\\\LifeShare-builds", "engine": "E:\\\\UE_5.8", "timeoutMinutes": 300,
               "nodeLabels": ["unreal:5.8.3"]}
`nodeLabels` are extra labels a node needs to take a build (for example a matching engine version).
`"publish": {"to": "github", "keep": 3}` also uploads each finished build to a private pre-release on the project
repository and keeps the newest three, so it can be downloaded from any computer (publish.py).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import uuid
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path, PureWindowsPath

VERSION = re.compile(r"^(0|[1-9]\d{0,5})\.(0|[1-9]\d{0,5})\.(0|[1-9]\d{0,5})$")
REF = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
SHA = re.compile(r"^[0-9a-f]{40}$")
WHERE = ("auto", "node", "pc")
TERMINAL = ("done", "failed", "cancelled")
KEEP_FINISHED = 30          # finished rows kept for the list; older ones are dropped
NOT_ON_NODE = "This node does not run"


def utc(value: str = "") -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as e:
        raise ValueError("Choose a valid date and time.") from e
    if t.tzinfo is None:
        raise ValueError("The time must include a time zone.")
    return t.astimezone(timezone.utc)


def iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat(timespec="seconds")


class Builds:
    def __init__(self, engine):
        self.engine = engine
        self.lock = threading.RLock()
        self._publishing: threading.Thread | None = None
        # A PC build that was running when Fletchery stopped did not finish: say so instead of showing it running.
        for row in self.rows:
            if row["state"] == "running" and row.get("route") == "pc":
                row.update(state="failed", finished=iso(utc()),
                           reason="Fletchery restarted while this build ran on this PC. Queue it again.")
            if row.get("publish") in ("zipping", "uploading", "failed"):  # cut off by a restart, or failed: try again
                row["publish"] = ""

    # ---- state ----
    @property
    def rows(self) -> list:
        st = self.engine.state
        if not isinstance(getattr(st, "builds", None), list):
            st.builds = []
        return st.builds

    @property
    def conf(self) -> dict:
        return getattr(self.engine.cfg, "builds", None) or {}

    def save(self) -> None:
        if not self.engine.dry_run:
            self.engine.state.save(self.engine.state_dir / "state.json")

    def _log(self, text: str) -> None:
        try:
            self.engine.ctx.log("builds: " + text)
        except Exception:
            pass

    def update(self, row: dict, **values) -> None:
        row.update(values)
        self.save()

    def view(self) -> dict:
        with self.lock:
            rows = deepcopy(self.rows)
        coord = getattr(self.engine, "machines", None)
        nodes = []
        if coord is not None:
            try:
                nodes = coord.machines.eligible(self.node_labels(), self._node_ram(), "package")
            except Exception:
                nodes = []
        return {"builds": rows, "base": self.engine.cfg.base_branch, "nodes": nodes,
                "machines": coord is not None, "out_dir": str(self.out_dir())}

    def node_labels(self) -> list:
        import machines
        extra = [str(x) for x in (self.conf.get("nodeLabels") or []) if isinstance(x, str)]
        return list(machines.RECIPES["package"]["labels"]) + [x for x in extra if x not in machines.RECIPES["package"]["labels"]]

    def _node_ram(self) -> float:
        import machines
        return float(machines.RECIPES["package"]["min_ram_gb"])

    def out_dir(self) -> Path:
        return Path(self.conf.get("outDir") or Path(self.engine.state_dir) / "builds")

    # ---- what the dashboard does ----
    def add(self, data: dict) -> dict:
        version = str(data.get("version") or "").strip()
        if not VERSION.match(version):
            raise ValueError("Enter a version such as 0.3.9.")
        ref = str(data.get("ref") or "").strip() or self.engine.cfg.base_branch
        if not (SHA.match(ref.lower()) or (REF.match(ref) and ".." not in ref)):
            raise ValueError("Give a branch name or a full commit SHA.")
        at = utc(data.get("at") or "")
        repeat = str(data.get("repeat") or "")
        if repeat not in ("", "daily"):
            raise ValueError("A build runs once or every day.")
        where = str(data.get("where") or "auto")
        if where not in WHERE:
            raise ValueError("Run it automatically, on a node, or on this PC.")
        if where == "node" and getattr(self.engine, "machines", None) is None:
            raise ValueError("No test nodes are set up (workers.machines); choose this PC or automatic.")
        tests = "full" if data.get("tests") in ("full", True) else ""
        row = {"id": uuid.uuid4().hex[:12], "version": version, "ref": ref, "at": iso(at), "repeat": repeat,
               "tests": tests, "where": where, "state": "scheduled", "created": iso(utc()), "sha": "", "route": "",
               "job": 0, "machine": "", "reason": "", "output": "", "started": "", "finished": "", "parent": ""}
        with self.lock:
            if sum(1 for r in self.rows if r["state"] not in TERMINAL) >= 50:
                raise ValueError("50 builds are already scheduled or waiting.")
            self.rows.append(row)
            self.save()
        self._log(f"{version} of {ref} scheduled for {row['at']}{' and every day after' if repeat else ''} "
                  f"({'with Full tests, ' if tests else ''}{where})")
        self.engine.wake()
        return deepcopy(row)

    def cancel(self, build_id: str) -> dict:
        with self.lock:
            row = next((r for r in self.rows if r["id"] == build_id), None)
            if row is None:
                raise ValueError("Build not found.")
            if row["state"] in TERMINAL:
                raise ValueError(f"This build is already {row['state']}.")
            if row["state"] == "running" and row.get("route") == "pc":
                raise ValueError("This build is running on this PC; it finishes.")
            if row.get("job") and row["state"] in ("queued", "running"):
                coord = getattr(self.engine, "machines", None)
                if coord is not None:
                    state = coord.machines.cancel(int(row["job"]))
                    if state == "stopping":
                        self.update(row, reason="Stopping on the node.")
                        return {"message": "The node stops this build at its next heartbeat."}
            self.update(row, state="cancelled", finished=iso(utc()),
                        reason="Repeat stopped." if row.get("repeat") else "Cancelled before it started.")
        self._log(f"{row['version']} cancelled")
        self.engine.wake()
        return {"message": "Repeat stopped." if row.get("repeat") else "Build cancelled."}

    # ---- the scheduler: off the event loop (it may ask GitHub for a branch head) ----
    def tick(self, at: datetime | None = None) -> None:
        at = at or utc()
        with self.lock:
            rows = list(self.rows)
        for row in rows:
            try:
                if row["state"] == "scheduled" and utc(row["at"]) <= at:
                    self._due(row, at)
                elif row["state"] in ("waiting",) and row.get("route") in ("", "node"):
                    self._route(row)
                elif row["state"] in ("queued", "running") and row.get("route") == "node":
                    self._follow_node(row)
            except Exception as e:  # one bad row never stops the others
                self.update(row, reason=f"{type(e).__name__}: {e}"[:300])
        self._trim()
        self._publish_next()

    def _due(self, row: dict, at: datetime) -> None:
        if row.get("repeat"):
            # A daily schedule starts a one-off copy and moves to the next day it has not reached yet.
            run = {**deepcopy(row), "id": uuid.uuid4().hex[:12], "repeat": "", "parent": row["id"], "created": iso(at),
                   "state": "scheduled"}
            nxt = utc(row["at"])
            while nxt <= at:
                nxt += timedelta(days=1)
            with self.lock:
                self.rows.append(run)
                self.update(row, at=iso(nxt), reason=f"Last started {iso(at)[:16].replace('T', ' ')}Z")
            row = run
        sha = self._resolve(row["ref"])
        if not sha:
            raise ValueError(f"GitHub has no commit for {row['ref']!r}")
        self.update(row, sha=sha, state="waiting", started="", reason=f"Building {row['ref']} at {sha[:10]}; waiting for a machine.")
        self._log(f"{row['version']}: {row['ref']} is at {sha[:10]}")
        self._route(row)

    def _resolve(self, ref: str) -> str:
        if SHA.match(ref.lower()):
            return ref.lower()
        coord = getattr(self.engine, "machines", None)
        if coord is not None:
            return coord.resolve(ref)
        data = self.engine.ctx.gh.api(f"repos/{self.engine.cfg.repo}/commits/{ref}", fresh=True)
        return str((data or {}).get("sha") or "")

    def _route(self, row: dict) -> None:
        """Choose the machine once. A node gets a queued job; this PC waits for the merge slot (loop.py starts it)."""
        if row.get("route") == "pc":
            return
        coord = getattr(self.engine, "machines", None)
        where = row.get("where") or "auto"
        nodes = []
        if coord is not None and where != "pc":
            nodes = coord.machines.eligible(self.node_labels(), self._node_ram(), "package")
        if where == "node" or (where == "auto" and nodes):
            if coord is None:
                self.update(row, state="failed", finished=iso(utc()), reason="No test nodes are set up.")
                return
            job = coord.machines.enqueue("package", row["sha"], {"version": row["version"], "tests": row["tests"]},
                                         ref=row["ref"], requested_by=f"build {row['version']}",
                                         labels=self.node_labels())
            self.update(row, route="node", state="queued", job=job,
                        reason=f"Queued for {', '.join(nodes) if nodes else 'a test node (none online yet)'} as job {job}.")
            self._log(f"{row['version']}: job {job} queued for the nodes")
            return
        self.update(row, route="pc", state="waiting", reason="Waiting for this PC: it starts when no merge round or PC check is running.")

    def _follow_node(self, row: dict) -> None:
        coord = getattr(self.engine, "machines", None)
        job = coord.machines.job(int(row["job"])) if coord is not None else None
        if job is None:
            self.update(row, state="failed", finished=iso(utc()), reason="The node job is gone.")
            return
        state = job["state"]
        if state == "running" and row["state"] != "running":
            self.update(row, state="running", machine=job["machine"], started=job["started"] or iso(utc()),
                        reason=f"Building on {job['machine']}.")
        elif state == "passed":
            self.update(row, state="done", machine=job["machine"], finished=job["finished"] or iso(utc()),
                        output=_out_from(job["summary"]) or job["summary"], reason=job["summary"])
            self._log(f"{row['version']}: {job['summary']}")
        elif state in ("failed", "lost", "cancelled"):
            summary = job.get("summary") or state
            if summary.startswith(NOT_ON_NODE) and row.get("where") != "node":
                # The node has not opted in to the `package` recipe: build it here instead.
                self.update(row, route="pc", state="waiting", job=0, machine="",
                            reason=f"{job['machine'] or 'The node'} does not take builds yet; waiting for this PC.")
                return
            self.update(row, state="cancelled" if state == "cancelled" else "failed", machine=job["machine"],
                        finished=job["finished"] or iso(utc()), reason=summary)
            self._log(f"{row['version']}: {summary}")

    def _trim(self) -> None:
        with self.lock:
            done = [r for r in self.rows if r["state"] in TERMINAL]
            if len(done) > KEEP_FINISHED:
                drop = {id(r) for r in sorted(done, key=lambda r: r.get("finished") or r["created"])[:len(done) - KEEP_FINISHED]}
                self.rows[:] = [r for r in self.rows if id(r) not in drop]
                self.save()

    # ---- downloads (publish.py) ----
    def _publish_next(self) -> None:
        """Start publishing the newest finished build that has not been published, one at a time, in its own thread
        (an upload can take an hour; the scheduler keeps ticking)."""
        import publish
        p = publish.conf(self.conf)
        if p is None or self.engine.dry_run or (self._publishing is not None and self._publishing.is_alive()):
            return
        keep = max(1, int(p.get("keep") or 3))
        with self.lock:
            done = sorted((r for r in self.rows if r["state"] == "done"), key=lambda r: r.get("finished") or "", reverse=True)[:keep]
            row = next((r for r in done if not r.get("publish")), None)
        if row is None:
            return
        self.update(row, publish="zipping", publish_reason="")
        self._publishing = threading.Thread(target=self.publish, args=(row, p), name=f"publish-{row['version']}", daemon=True)
        self._publishing.start()

    def publish(self, row: dict, p: dict) -> None:
        import publish
        folder = Path(row.get("output") or "")
        coord = getattr(self.engine, "machines", None)
        if row.get("output") and not folder.is_dir() and coord is not None:
            # A node build whose summary named the node's folder: the copy it sent back has the same name here.
            try:
                folder = Path(coord.machines.upload_dir()) / PureWindowsPath(row["output"]).name
            except Exception:
                pass
        if not row.get("output") or not folder.is_dir():
            self.update(row, publish="skipped", publish_reason=f"The package folder is not on this PC ({row.get('output') or 'none'}).")
            return
        gh = self.engine.ctx.gh
        pub = publish.Publisher(gh, str(p.get("repo") or self.engine.cfg.repo), getattr(gh, "gh", "gh"), self._log)
        try:
            part = int(float(p.get("partGb") or 1.9) * 2**30)
            stem = f"{self.engine.cfg.name or 'Build'}-{row['version']}-{row['sha'][:10]}"
            dest = self.out_dir() / "_downloads"
            zips = publish.make_zips(folder, dest, stem, min(part, 2**31 - 2**20))
            self.update(row, publish="uploading",
                        publish_reason=f"Uploading {len(zips)} zip{'s' if len(zips) > 1 else ''} ({sum(z.stat().st_size for z in zips) / 2**30:.2f} GiB).")
            self._log(f"{row['version']}: uploading {', '.join(z.name for z in zips)}")
            url = pub.release({**row, "name": self.engine.cfg.name}, zips)
            gone = pub.prune(max(1, int(p.get("keep") or 3)))
            drive = ""
            if p.get("driveDir"):
                try:
                    old = publish.to_drive(zips, Path(p["driveDir"]), self.engine.cfg.name or "Build", max(1, int(p.get("keep") or 3)))
                    drive = f" Copied to {p['driveDir']}" + (f" (removed {', '.join(old)})." if old else ".")
                except OSError as e:
                    drive = f" Not copied to {p['driveDir']}: {e}"
            for z in zips:
                z.unlink(missing_ok=True)
            self.update(row, publish="published", download=url,
                        publish_reason=f"Download: {url}" + (f" (removed {', '.join(gone)})" if gone else "") + drive)
            self._log(f"{row['version']}: download ready at {url}" + (f"; removed {', '.join(gone)}" if gone else ""))
        except Exception as e:
            self.update(row, publish="failed", publish_reason=f"{type(e).__name__}: {e}"[:400])
            self._log(f"{row['version']}: publish failed: {e}")

    # ---- this PC ----
    def next_pc(self) -> dict | None:
        """The oldest build waiting for this PC."""
        with self.lock:
            waiting = [r for r in self.rows if r["state"] == "waiting" and r.get("route") == "pc" and r.get("sha")]
        return min(waiting, key=lambda r: (r["at"], r["created"])) if waiting else None

    def run_pc(self, row: dict) -> None:
        """Run in a thread. Uses the merge checkout (rounds.prepare_worktree waits for the heavy lease and keeps a
        dirty checkout aside), checks out the build's commit, runs the package steps, then cleans up."""
        import rounds
        import node_worker
        repo, cfg = self.engine.ctx.repo, self.engine.cfg
        try:
            path = rounds.prepare_worktree(repo, f"fletchery/build-{row['version']}", cfg.heavy_lease_file)
        except rounds.CheckoutBusy as e:
            self.update(row, reason=f"Waiting for this PC: {e}"[:300])
            return
        except (rounds.CheckoutRecovery, RuntimeError) as e:
            self.update(row, reason=f"Build checkout unavailable; it tries again: {e}"[:300])
            return
        try:
            repo.git("fetch", repo.remote, row["sha"], cwd=path)
            repo.git("checkout", "--no-overwrite-ignore", "-B", f"fletchery/build-{row['version']}", row["sha"], cwd=path)
            # The merge checkout may hold LFS pointer text for assets a round never needed: 0.4.0 failed to cook on
            # 60 "Package is unloadable" Silhouette meshes (2026-11-12). Fetch the real files before building.
            repo.git("lfs", "pull", cwd=path)
        except RuntimeError as e:
            self.update(row, state="failed", finished=iso(utc()), reason=f"Could not check out {row['sha'][:10]}: {e}"[:300])
            return
        out = node_worker.package_out(self.out_dir(), self.engine.cfg.name or "Build", row["version"], row["sha"])
        self.update(row, state="running", machine="This PC", started=iso(utc()), output=str(out),
                    reason=f"Building on this PC into {out}.")
        self._log(f"{row['version']}: building {row['sha'][:10]} on this PC into {out}")
        engine_dir = self.conf.get("engine") or r"E:\UE_5.8"
        steps = node_worker.package_steps({"version": row["version"], "tests": row["tests"], "out_dir": str(out)},
                                          Path(path), engine_dir)
        deadline_s = float(self.conf.get("timeoutMinutes") or 300) * 60
        log_file = Path(self.engine.state_dir) / "build-logs" / f"{row['id']}.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        failed, started = "", datetime.now(timezone.utc)
        flags = 0x00004000 if sys.platform == "win32" else 0  # below normal: the PC is shared
        with open(log_file, "a", encoding="utf-8", errors="replace") as log:
            for name, step, always in steps:
                if failed and not always:
                    continue
                left = deadline_s - (datetime.now(timezone.utc) - started).total_seconds()
                if not always and left <= 0:
                    failed = failed or "Timed out"
                    continue
                argv = step() if callable(step) else step
                log.write(f"\n==> {name}\n")
                log.flush()
                try:
                    p = subprocess.run(argv, cwd=str(path), stdout=log, stderr=subprocess.STDOUT, timeout=max(60, left) if not always else 600,
                                       creationflags=flags, env=dict(os.environ, FLETCHERY_BUILD=row["id"]))
                    code = p.returncode
                except subprocess.TimeoutExpired:
                    code = 124
                except OSError as e:
                    log.write(f"could not start: {e}\n")
                    code = 127
                if code and not always and not failed:
                    failed = f"{name} failed (exit {code})"
        tail = _tail(log_file)
        if failed:
            self.update(row, state="failed", finished=iso(utc()), reason=f"{failed}. Log: {log_file}", log=str(log_file), tail=tail)
        else:
            self.update(row, state="done", finished=iso(utc()), reason=f"Packaged to {out}.", log=str(log_file), tail=tail)
        self._log(f"{row['version']}: {failed or 'packaged to ' + str(out)}")


def _out_from(summary: str) -> str:
    """Where to find the build: the copy on this PC when the node sent one back, else the node's folder."""
    m = re.search(r"; copied to (.+?) on the PC", summary or "") or re.match(r"Packaged to (.+?) on ", summary or "")
    return m.group(1) if m else ""


def _tail(path: Path, lines: int = 40) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.rstrip().splitlines()[-lines:])
