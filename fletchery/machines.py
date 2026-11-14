"""Machines: Fletchery as the job queue for other build machines (test nodes), and the history of what they ran.

A node runs `node_worker.py`. It connects out to this coordinator over the private network (Tailscale), proves who it
is with its own key, and asks for work. Fletchery never connects to a node and never sends it a command line: a job
names one of the node's own recipes (node_worker.RECIPES) plus checked parameters, and the exact commit to test.

    machines   one row per node: a hash of its key (the key itself is shown once, when the node is added), its last
               labels, RAM and free disk as it reported them, when it was last seen, and whether it was revoked
    jobs       the queue and history: recipe, commit, parameters, the labels and RAM it needs, which node took it,
               heartbeats, result, and the GitHub commit status context it reports under

Profile (workers.machines):
    {"listen": "100.83.43.111:8771",            # the worker API: the coordinator's Tailscale address, never 0.0.0.0
     "shadow": true,                            # commit statuses go out as "shadow/<context>" until cutover
     "nightly": {"at": "07:00", "branch": "main", "recipe": "nightly-full"},   # optional; UTC
     "assetRoots": ["E:\\lifeshare-scratch\\meshy_export"]}   # PC folders every node mirrors (same path) before imports

The worker API (all POST, JSON, `Authorization: Bearer <key>` and `X-Fletchery-Machine: <name>`):
    /v1/hello      {labels, ram_gb, disk_free_gb, version}          -> {poll_seconds}
    /v1/claim      {stats}                                          -> {job} or {job: null}
    /v1/heartbeat  {job_id, stats}                                  -> {cancel}
    /v1/log        {job_id, text}                                   -> {ok}
    /v1/result     {job_id, exit_code, summary, result}             -> {ok}
    /v1/upload     {job_id, name, offset, data (base64), done, sha256} -> {ok, path}: a node copies a packaged
                   build back to this PC (<state>/builds), in chunks; the last call checks the hash and unpacks it
    /v1/assets/list {}                                              -> {host, roots: [{root, files: [[path, size,
                   sha256]]}], missing}: the PC folders nodes mirror before imports (workers.machines.assetRoots)
    /v1/assets/read {root, path, offset}                            -> {data (base64), size}: one chunk of one file

Command line (on the PC, against the same state folder as the backend):
    python machines.py --state-dir <dir> add <name>        print a new node's key (once)
    python machines.py --state-dir <dir> revoke <name>
    python machines.py --state-dir <dir> list
    python machines.py --state-dir <dir> enqueue --recipe node-checks --sha <40 hex> [--only A,B] [--strict-slots]
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ONLINE_SECONDS = 90          # a node that has not called for this long shows as offline
RETURN_SECONDS = 15 * 60     # `run` waits for a node seen this recently: its worker is probably restarting
RETURN_WAIT_MINUTES = 8      # how long `run` waits for such a node to call in again before "no result"
LOST_SECONDS = 180           # a running job without a heartbeat for this long is lost (the node stopped or rebooted)
MAX_ATTEMPTS = 2             # a lost job is queued again once, then fails
MAX_BODY = 1024 * 1024
MAX_UPLOAD_BODY = 8 * 1024 * 1024   # /v1/upload chunks (base64 of up to 4 MiB)
UPLOAD_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.zip$")
LOG_MAX_BYTES = 50 * 1024 * 1024
# The lines of a job's log that mark its progress (Testing tab): the worker's own steps, the project script's sections
# ("== Build LifeShareEditor") and each check's result ("PASS  RunSpecs (200s)"). Each is kept with the time it arrived.
MARK = re.compile(r"^(##\[step\] .+|== .+|(PASS|FAIL|SKIP|WARN)\s+\S.*)$")
STEP_END = re.compile(r"^##\[step\] (.+): exit (-?\d+)$")
RESULT = re.compile(r"^(PASS|FAIL|SKIP|WARN)\s+(.+?)(?:\s+\(([^)]*)\))?\s*$")
RESULT_STATE = {"PASS": "passed", "FAIL": "failed", "SKIP": "skipped", "WARN": "warning"}
FALLBACK_LOG_BYTES = 5 * 1024 * 1024
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
SHA = re.compile(r"^[0-9a-f]{40}$")
LABEL = re.compile(r"^[A-Za-z0-9_.:+-]{1,64}$")
ONLY = re.compile(r"^[A-Za-z0-9_]+(,[A-Za-z0-9_]+)*$")
IMPORT_LIST = re.compile(r"^[A-Za-z0-9_]+(:[A-Za-z0-9_./-]+)?(,[A-Za-z0-9_]+(:[A-Za-z0-9_./-]+)?)*$")
ROUND_BRANCH = re.compile(r"^fletchery/round-[0-9]{1,6}$")
IMPORT_LABEL = "role:imports"   # a node that has a write key (gitPushKey) and so can import and push round content
# A worker that mirrors the PC's asset folders before an import (node_worker.sync_assets) says so with this label. When
# the profile shares asset folders, an import job only goes to a node that carries it (Alex, 2026-11-13: "Ensure that
# this is always going to be used"; #1681 failed because the node had no meshy_export).
ASSET_SYNC_LABEL = "cap:asset-sync"
ASSET_ROOTS_FILE = "asset_roots.json"   # in the state folder, so `machines.py run` (a round's command) sees the profile
ASSET_CHUNK = 4 * 1024 * 1024
ASSET_SKIP = (".fletchery-assets.json", ".fletchery-part")   # a mirror's own bookkeeping, never shared back

# What Fletchery can ask a node to run. The node has the same list and refuses anything else. `context` is the GitHub
# commit status the job reports under: the names the Buildlane runner used, so ci.yml and lane.yml read them unchanged.
RECIPES = {
    "node-checks": {"context": "Node checks", "labels": ["os:windows", "role:tests"], "min_ram_gb": 16, "timeout_minutes": 120},
    "nightly-full": {"context": "Nightly full tests (node)", "labels": ["os:windows", "role:tests"], "min_ram_gb": 16,
                     "timeout_minutes": 150},
    "checkout-only": {"context": "", "labels": [], "min_ram_gb": 0, "timeout_minutes": 30},
    # A worker PR merged onto the base branch, then the project's PC check (LifeShare: Tools/CI/PrCheck.ps1). No commit
    # status: Fletchery labels the PR or posts the failure itself, as it does for a check on the PC (nodecheck.py).
    "pr-check": {"context": "", "labels": ["os:windows", "role:tests"], "min_ram_gb": 16, "timeout_minutes": 90},
    # A packaged build Alex scheduled on Building -> Builds (builds.py). No commit status: a build is not a check.
    "package": {"context": "", "labels": ["os:windows", "role:cook"], "min_ram_gb": 16, "timeout_minutes": 300},
}
# Each recipe is one kind of work. Alex can pause a single machine from a single kind (Building -> Machines).
KIND = {"pr-check": "checks", "node-checks": "tests", "nightly-full": "tests", "checkout-only": "tests", "package": "builds"}
NODE_KINDS = ("checks", "tests", "builds")
BUILD_VERSION = re.compile(r"^(0|[1-9]\d{0,5})\.(0|[1-9]\d{0,5})\.(0|[1-9]\d{0,5})$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS machines (
    name TEXT PRIMARY KEY, key_hash TEXT NOT NULL, added TEXT NOT NULL, revoked TEXT,
    labels TEXT NOT NULL DEFAULT '[]', ram_gb REAL NOT NULL DEFAULT 0, version TEXT NOT NULL DEFAULT '',
    stats TEXT NOT NULL DEFAULT '{}', last_seen TEXT, address TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, recipe TEXT NOT NULL, repo TEXT NOT NULL, sha TEXT NOT NULL,
    ref TEXT NOT NULL DEFAULT '', params TEXT NOT NULL DEFAULT '{}', labels TEXT NOT NULL DEFAULT '[]',
    min_ram_gb REAL NOT NULL DEFAULT 0, timeout_minutes REAL NOT NULL DEFAULT 120, context TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'queued', machine TEXT NOT NULL DEFAULT '', attempts INTEGER NOT NULL DEFAULT 0,
    created TEXT NOT NULL, started TEXT, heartbeat TEXT, finished TEXT, exit_code INTEGER,
    summary TEXT NOT NULL DEFAULT '', result TEXT NOT NULL DEFAULT '', cancel INTEGER NOT NULL DEFAULT 0,
    requested_by TEXT NOT NULL DEFAULT '', status_sent TEXT NOT NULL DEFAULT '', log_bytes INTEGER NOT NULL DEFAULT 0,
    pr INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state, id);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

DONE = ("passed", "failed", "cancelled", "lost")


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _age(ts: str | None, at: datetime | None = None) -> float:
    if not ts:
        return float("inf")
    try:
        t = datetime.fromisoformat(ts)
    except ValueError:
        return float("inf")
    return ((at or datetime.now(timezone.utc)) - t).total_seconds()


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def check_listen(listen: str) -> tuple[str, int]:
    """host:port for the worker API. A wildcard or public bind is refused: the API must only be on the private
    network (the coordinator's Tailscale address) or this machine."""
    host, _, port = (listen or "").rpartition(":")
    host = host.strip("[]")
    if not host or not port.isdigit():
        raise ValueError(f"machines.listen must be host:port, got {listen!r}")
    if host in ("0.0.0.0", "::", "*", ""):
        raise ValueError("machines.listen must name one private address (the Tailscale IP), not every interface")
    import ipaddress
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        raise ValueError(f"machines.listen must be an IP address, got {host!r}") from None
    cgnat = ipaddress.ip_network("100.64.0.0/10")  # Tailscale's addresses
    if not (ip.is_loopback or ip.is_private or ip in cgnat):
        raise ValueError(f"machines.listen {host} is a public address; use the coordinator's Tailscale IP")
    return host, int(port)



def claim_rank(requested_by: str) -> int:
    """Node queue priority: 0 for a merge round's jobs, 1 for builds, 2 for everything else."""
    who = (requested_by or "").lower()
    return 0 if who.startswith("round ") else 1 if who.startswith("build ") else 2

class Machines:
    """The registry and the job queue, in <state_dir>/machines.db. Safe to use from several threads and from the
    command line while the backend runs (SQLite with a busy timeout; every change is one short transaction)."""

    def __init__(self, state_dir: str | Path, repo: str = "", shadow: bool = True):
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "machines.db"
        self.logs = self.dir / "job-logs"
        self.logs.mkdir(exist_ok=True)
        self.repo = repo
        self.shadow = shadow
        self._lock = threading.RLock()
        self._partial: dict[int, str] = {}   # a job's last log line while it is still arriving
        self._steps_cache: dict[int, tuple] = {}
        with self._db() as db:
            db.executescript(SCHEMA)
            have = {r[1] for r in db.execute("PRAGMA table_info(jobs)")}
            for col, decl in (("pr", "INTEGER NOT NULL DEFAULT 0"), ("handled", "INTEGER NOT NULL DEFAULT 0")):  # columns added after the first release
                if col not in have:
                    db.execute(f"ALTER TABLE jobs ADD COLUMN {col} {decl}")

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        return _Closing(db)

    # ---- the registry ----
    def add_machine(self, name: str) -> str:
        """Register a node and return its key. Only the key's hash is kept, so this is the one time it is shown.
        Adding a name that exists (revoked or not) replaces its key: the old key stops working."""
        if not NAME.match(name or ""):
            raise ValueError("A machine name is letters, digits, '.', '_' or '-' (up to 63)")
        key = "fnk_" + secrets.token_urlsafe(32)
        with self._lock, self._db() as db:
            db.execute("INSERT INTO machines(name, key_hash, added) VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET "
                       "key_hash=excluded.key_hash, revoked=NULL, added=excluded.added", (name, hash_key(key), now()))
        return key

    def revoke(self, name: str) -> bool:
        with self._lock, self._db() as db:
            cur = db.execute("UPDATE machines SET revoked=? WHERE name=? AND revoked IS NULL", (now(), name))
            if cur.rowcount:  # its running job can't report any more: queue it again for another node
                for row in db.execute("SELECT id, attempts FROM jobs WHERE machine=? AND state='running'", (name,)).fetchall():
                    self._lose(db, row["id"], row["attempts"], f"{name} was revoked")
            return bool(cur.rowcount)

    def authenticate(self, name: str, key: str) -> bool:
        if not name or not key or not NAME.match(name):
            hmac.compare_digest("x", "y")
            return False
        with self._db() as db:
            row = db.execute("SELECT key_hash, revoked FROM machines WHERE name=?", (name,)).fetchone()
        good = hmac.compare_digest(row["key_hash"] if row else hash_key("none"), hash_key(key))
        return bool(row) and good and not row["revoked"]

    def seen(self, name: str, address: str = "", labels=None, ram_gb=None, version=None, stats=None) -> None:
        sets, args = ["last_seen=?"], [now()]
        if address:
            sets.append("address=?"); args.append(address)
        if labels is not None:
            clean = sorted({str(x) for x in labels if LABEL.match(str(x))})[:40]
            sets.append("labels=?"); args.append(json.dumps(clean))
        if ram_gb is not None:
            sets.append("ram_gb=?"); args.append(float(ram_gb))
        if version is not None:
            sets.append("version=?"); args.append(str(version)[:40])
        if stats is not None:
            sets.append("stats=?"); args.append(json.dumps(_clean_stats(stats)))
        with self._lock, self._db() as db:
            db.execute(f"UPDATE machines SET {', '.join(sets)} WHERE name=?", (*args, name))

    # ---- the queue ----
    def enqueue(self, recipe: str, sha: str, params: dict | None = None, ref: str = "", requested_by: str = "",
                labels: list | None = None, context: str | None = None, pr: int = 0) -> int:
        if recipe not in RECIPES:
            raise ValueError(f"Unknown recipe {recipe!r}; known: {', '.join(RECIPES)}")
        sha = (sha or "").lower()
        if not SHA.match(sha):
            raise ValueError("A job needs the full 40-character commit SHA")
        spec = RECIPES[recipe]
        params = check_params(recipe, params or {})
        need = list(spec["labels"]) + [x for x in (labels or []) if x not in spec["labels"]]
        if any(not LABEL.match(x) for x in need):
            raise ValueError("A label is letters, digits and _ . : + -")
        with self._lock, self._db() as db:
            cur = db.execute("INSERT INTO jobs(recipe, repo, sha, ref, params, labels, min_ram_gb, timeout_minutes, context, "
                             "created, requested_by, pr) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                             (recipe, self.repo, sha, ref[:200], json.dumps(params), json.dumps(need), spec["min_ram_gb"],
                              spec["timeout_minutes"], spec["context"] if context is None else context, now(),
                              requested_by[:100], max(0, int(pr or 0))))
            return int(cur.lastrowid)

    # ---- builds copied back from a node (Alex, 2026-11-12: the 0.4.0 zip on the PC was empty) ----
    def upload_dir(self) -> Path:
        return Path(getattr(self, "builds_dir", "") or self.dir / "builds")

    def upload(self, machine: str, job_id: int, name: str, offset: int, chunk: bytes, done: bool,
               sha256: str = "") -> dict:
        """Write one chunk of a packaged build that `machine` made in job `job_id`. On the last chunk check the size
        and SHA-256, keep <name> and unpack it beside it (<name without .zip>, or with the job id when that folder
        already has files)."""
        import hashlib
        import zipfile
        if not UPLOAD_NAME.match(name or ""):
            raise ValueError("bad build name")
        job = self.job(job_id)
        if not job or job["machine"] != machine or job["recipe"] != "package":
            raise PermissionError("only the node that packaged a build can send it")
        root = self.upload_dir()
        root.mkdir(parents=True, exist_ok=True)
        part = root / (name + f".part-{job_id}")
        with self._lock:
            size = part.stat().st_size if part.exists() else 0
            if offset != size:
                return {"ok": False, "offset": size}  # the node resends from here
            with open(part, "ab") as f:
                f.write(chunk)
        if not done:
            return {"ok": True, "offset": size + len(chunk)}
        h = hashlib.sha256()
        with open(part, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        if sha256 and h.hexdigest() != sha256.lower():
            part.unlink(missing_ok=True)
            raise ValueError("the copy does not match the node's file; send it again")
        final = root / name
        final.unlink(missing_ok=True)
        part.replace(final)
        folder = root / name[:-4]
        if folder.exists() and any(folder.iterdir()):
            folder = root / f"{name[:-4]}-job{job_id}"
        with zipfile.ZipFile(final) as z:
            for member in z.namelist():  # no absolute paths or .. in a build zip
                if member.startswith(("/", "\\")) or ".." in Path(member).parts:
                    raise ValueError(f"unsafe path in the zip: {member}")
            z.extractall(folder)
        return {"ok": True, "path": str(folder), "zip": str(final)}

    # ---- asset folders nodes mirror from this PC (Alex, 2026-11-13: a permanent way to share files with nodes) ----
    def set_asset_roots(self, roots) -> list[str]:
        """Keep the profile's workers.machines.assetRoots in the state folder: the worker API serves them and a round's
        `machines.py run` checks them before it queues an import."""
        clean = []
        for r in roots or []:
            r = str(r or "").strip()
            if r and Path(r).is_absolute() and r not in clean:
                clean.append(r)
        path = self.dir / ASSET_ROOTS_FILE
        text = json.dumps(clean, indent=2)
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
        return clean

    def asset_roots(self) -> list[str]:
        try:
            roots = json.loads((self.dir / ASSET_ROOTS_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [str(r) for r in roots if isinstance(r, str) and r] if isinstance(roots, list) else []

    def missing_asset_roots(self) -> list[str]:
        """Shared asset folders that are not on this PC, or are empty: an import would fail on every node."""
        out = []
        for r in self.asset_roots():
            p = Path(r)
            try:
                if not p.is_dir() or not any(p.iterdir()):
                    out.append(r)
            except OSError:
                out.append(r)
        return out

    def _hash(self, path: Path, size: int, mtime: int) -> str:
        cache = self.__dict__.setdefault("_asset_hashes", {})
        key = str(path)
        hit = cache.get(key)
        if hit and hit[0] == size and hit[1] == mtime:
            return hit[2]
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        cache[key] = (size, mtime, h.hexdigest())
        return cache[key][2]

    def asset_list(self) -> dict:
        """Every file under each shared folder with its size and SHA-256 (cached by size and modified time, so only
        new or changed files are hashed again)."""
        import socket
        roots, missing = [], self.missing_asset_roots()
        for r in self.asset_roots():
            if r in missing:
                continue
            base, files = Path(r), []
            for p in sorted(base.rglob("*")):
                if not p.is_file() or p.is_symlink() or p.name.endswith(ASSET_SKIP):
                    continue
                st = p.stat()
                files.append([p.relative_to(base).as_posix(), st.st_size, self._hash(p, st.st_size, st.st_mtime_ns)])
            roots.append({"root": r, "files": files})
        return {"host": socket.gethostname(), "roots": roots, "missing": missing}

    def asset_read(self, root: str, rel: str, offset: int) -> dict:
        import base64
        if root not in self.asset_roots():
            raise PermissionError("that folder is not shared with nodes")
        base = Path(root).resolve()
        parts = Path(str(rel or "")).parts
        if not rel or Path(rel).is_absolute() or ".." in parts or str(rel).startswith(("/", "\\")):
            raise ValueError("bad path")
        p = (base / rel).resolve()
        if base not in p.parents or not p.is_file() or p.name.endswith(ASSET_SKIP):
            raise ValueError("no such file")
        size = p.stat().st_size
        with open(p, "rb") as f:
            f.seek(max(0, int(offset)))
            data = f.read(ASSET_CHUNK)
        return {"data": base64.b64encode(data).decode("ascii"), "size": size}

    def cancel(self, job_id: int) -> str:
        with self._lock, self._db() as db:
            row = db.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise ValueError(f"No job {job_id}")
            if row["state"] == "queued":
                db.execute("UPDATE jobs SET state='cancelled', finished=?, summary='Cancelled before it started' WHERE id=?",
                           (now(), job_id))
                return "cancelled"
            if row["state"] == "running":  # the node stops it at its next heartbeat and reports the result
                db.execute("UPDATE jobs SET cancel=1 WHERE id=?", (job_id,))
                return "stopping"
            return row["state"]

    def claim(self, machine: str) -> dict | None:
        """The first queued job this node can run: every label it needs is one of the node's, and the node has the
        RAM. Merge-round jobs go first (a round waits on them), then builds, then PR checks and the rest, oldest first
        within each (Alex, 2026-11-12: "nodes merge with crit prio"). A node runs one job at a time."""
        with self._lock, self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                m = db.execute("SELECT labels, ram_gb FROM machines WHERE name=? AND revoked IS NULL", (machine,)).fetchone()
                busy = db.execute("SELECT id FROM jobs WHERE machine=? AND state='running'", (machine,)).fetchone()
                if not m or busy:
                    db.execute("COMMIT")
                    return None
                have = set(json.loads(m["labels"] or "[]"))
                off = set(self._paused(db, machine))
                rows = db.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY id").fetchall()
                for row in sorted(rows, key=lambda r: (claim_rank(r["requested_by"]), r["id"])):
                    if KIND.get(row["recipe"]) in off:
                        continue
                    if set(json.loads(row["labels"])) <= have and float(row["min_ram_gb"] or 0) <= float(m["ram_gb"] or 0):
                        t = now()
                        db.execute("UPDATE jobs SET state='running', machine=?, started=?, heartbeat=?, attempts=attempts+1 "
                                   "WHERE id=?", (machine, t, t, row["id"]))
                        db.execute("COMMIT")
                        return {"id": row["id"], "recipe": row["recipe"], "repo": row["repo"], "sha": row["sha"],
                                "ref": row["ref"], "params": json.loads(row["params"]),
                                "timeout_minutes": row["timeout_minutes"]}
                db.execute("COMMIT")
                return None
            except Exception:
                db.execute("ROLLBACK")
                raise

    def preempt_for_rounds(self, at: datetime | None = None) -> list[tuple[int, str, int]]:
        """Stop a PR check (or other low-priority job) so a waiting merge-round job gets a node now. Claim order alone
        made round jobs wait for the PR check in front of them: up to 9 minutes on 2026-11-12 (rounds 140, 141), close to
        `run`'s 10-minute no-node limit. Only when no node that could take the round job is idle, only jobs ranked below
        builds, at most one per waiting round job, and the most recently started first (least work lost). The stopped
        check gives no result, so it simply runs again later. Returns [(stopped job, machine, round job)]."""
        at = at or datetime.now(timezone.utc)
        out = []
        with self._lock, self._db() as db:
            waiting = [r for r in db.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY id").fetchall()
                       if claim_rank(r["requested_by"]) == 0]
            if not waiting:
                return out
            nodes = []
            for m in db.execute("SELECT * FROM machines WHERE revoked IS NULL").fetchall():
                if _age(m["last_seen"], at) > ONLINE_SECONDS or self._held_off(db, m, json.loads(m["stats"] or "{}")):
                    continue
                run = db.execute("SELECT * FROM jobs WHERE machine=? AND state='running'", (m["name"],)).fetchone()
                nodes.append((m, set(json.loads(m["labels"] or "[]")), set(self._paused(db, m["name"])), run))
            used: set[str] = set()
            for job in waiting:
                able = [n for n in nodes if n[0]["name"] not in used and KIND.get(job["recipe"]) not in n[2]
                        and set(json.loads(job["labels"])) <= n[1]
                        and float(job["min_ram_gb"] or 0) <= float(n[0]["ram_gb"] or 0)]
                if not able:
                    continue
                free = [n for n in able if n[3] is None or n[3]["cancel"]]  # idle, or already being stopped
                if free:
                    used.add(free[0][0]["name"])
                    continue
                low = [n for n in able if claim_rank(n[3]["requested_by"]) >= 2]
                if not low:
                    continue
                m, _, _, run = max(low, key=lambda n: str(n[3]["started"] or ""))
                db.execute("UPDATE jobs SET cancel=1 WHERE id=?", (run["id"],))
                used.add(m["name"])
                out.append((run["id"], m["name"], job["id"]))
        return out

    def _own(self, db, job_id, machine):
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row or row["machine"] != machine or row["state"] != "running":
            raise PermissionError("That job is not running on this machine")
        return row

    def heartbeat(self, machine: str, job_id: int) -> dict:
        with self._lock, self._db() as db:
            row = self._own(db, job_id, machine)
            db.execute("UPDATE jobs SET heartbeat=? WHERE id=?", (now(), job_id))
            return {"cancel": bool(row["cancel"])}

    def append_log(self, machine: str, job_id: int, text: str) -> None:
        with self._lock, self._db() as db:
            row = self._own(db, job_id, machine)
            data = (text or "").encode("utf-8", "replace")
            if row["log_bytes"] >= LOG_MAX_BYTES:
                return
            if row["log_bytes"] + len(data) > LOG_MAX_BYTES:
                data = data[:max(0, LOG_MAX_BYTES - row["log_bytes"])] + b"\n[log cut at 50 MB]\n"
            with open(self.logs / f"{int(job_id)}.log", "ab") as f:
                f.write(data)
            self._mark(int(job_id), data.decode("utf-8", "replace"))
            db.execute("UPDATE jobs SET log_bytes=log_bytes+?, heartbeat=? WHERE id=?", (len(data), now(), job_id))

    def finish(self, machine: str, job_id: int, exit_code: int, summary: str = "", result=None) -> None:
        with self._lock, self._db() as db:
            row = self._own(db, job_id, machine)
            state = "cancelled" if row["cancel"] else ("passed" if int(exit_code) == 0 else "failed")
            tail = self._partial.pop(int(job_id), "")
            if tail:
                self._mark(int(job_id), tail + "\n")
            text = json.dumps(result)[:200_000] if result is not None else ""
            db.execute("UPDATE jobs SET state=?, finished=?, exit_code=?, summary=?, result=? WHERE id=?",
                       (state, now(), int(exit_code), (summary or "")[:500], text, job_id))

    def _lose(self, db, job_id: int, attempts: int, why: str) -> None:
        if attempts < MAX_ATTEMPTS:
            db.execute("UPDATE jobs SET state='queued', machine='', started=NULL, heartbeat=NULL, summary=? WHERE id=?",
                       (f"Queued again: {why}", job_id))
        else:
            db.execute("UPDATE jobs SET state='lost', finished=?, summary=? WHERE id=?", (now(), why[:500], job_id))

    def reap(self, at: datetime | None = None) -> list[int]:
        """Running jobs whose node stopped answering: queued again once, then lost."""
        out = []
        with self._lock, self._db() as db:
            for row in db.execute("SELECT id, machine, heartbeat, attempts FROM jobs WHERE state='running'").fetchall():
                if _age(row["heartbeat"], at) > LOST_SECONDS:
                    self._lose(db, row["id"], row["attempts"], f"{row['machine']} stopped answering")
                    out.append(row["id"])
        return out

    # ---- results Fletchery acts on (nodecheck.py) ----
    def active(self, recipe: str) -> list[dict]:
        """Queued and running jobs of one recipe."""
        with self._db() as db:
            return [_job(r) for r in db.execute("SELECT * FROM jobs WHERE recipe=? AND state IN ('queued','running') "
                                                "ORDER BY id", (recipe,)).fetchall()]

    def unhandled(self, recipe: str) -> list[dict]:
        """Finished jobs of one recipe whose result Fletchery has not acted on yet, oldest first."""
        with self._db() as db:
            return [_job(r) for r in db.execute(f"SELECT * FROM jobs WHERE recipe=? AND handled=0 AND state IN "
                                                f"({','.join('?' * len(DONE))}) ORDER BY id", (recipe, *DONE)).fetchall()]

    def mark_handled(self, job_id: int) -> None:
        with self._lock, self._db() as db:
            db.execute("UPDATE jobs SET handled=1 WHERE id=?", (int(job_id),))

    # ---- pausing one machine from one kind of work ----
    @staticmethod
    def _held_off(db, m, stats: dict) -> bool:
        """Paused for new work. A node that shows paused only because the heavy lease is taken while it runs one of
        Fletchery's own jobs is busy, not paused: its next job waits for it (2026-11-13: a busy SF-NODE-01 looked
        paused, so `run` found no node and rounds fell back to the PC)."""
        if not stats.get("paused"):
            return False
        if stats.get("lease") and not stats.get("checkout_failing") and not stats.get("pause_file"):
            return db.execute("SELECT 1 FROM jobs WHERE machine=? AND state='running'", (m["name"],)).fetchone() is None
        return True

    @staticmethod
    def _paused(db, name: str) -> list[str]:
        row = db.execute("SELECT value FROM meta WHERE key=?", ("paused:" + name,)).fetchone()
        try:
            return [k for k in json.loads(row["value"]) if k in NODE_KINDS] if row else []
        except ValueError:
            return []

    def paused_work(self, name: str) -> list[str]:
        with self._db() as db:
            return self._paused(db, name)

    def set_paused(self, name: str, kind: str, on: bool) -> list[str]:
        """Pause or resume one kind of work (checks, tests, builds) on one node. A running job finishes."""
        if kind not in NODE_KINDS:
            raise ValueError(f"Unknown kind of work {kind!r}; known: {', '.join(NODE_KINDS)}")
        with self._lock, self._db() as db:
            if not db.execute("SELECT 1 FROM machines WHERE name=?", (name,)).fetchone():
                raise ValueError(f"No machine {name!r}")
            kinds = set(self._paused(db, name))
            kinds = kinds | {kind} if on else kinds - {kind}
            out = [k for k in NODE_KINDS if k in kinds]
            db.execute("INSERT INTO meta(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       ("paused:" + name, json.dumps(out)))
            return out

    def eligible(self, labels: list, min_ram_gb: float = 0, recipe: str = "") -> list[str]:
        """Online, unrevoked, unpaused nodes that carry every label and have the RAM; with `recipe`, also not paused
        from that kind of work."""
        at = datetime.now(timezone.utc)
        out = []
        with self._db() as db:
            for m in db.execute("SELECT * FROM machines WHERE revoked IS NULL ORDER BY name").fetchall():
                stats = json.loads(m["stats"] or "{}")
                if recipe and KIND.get(recipe) in self._paused(db, m["name"]):
                    continue
                if (_age(m["last_seen"], at) <= ONLINE_SECONDS and not self._held_off(db, m, stats)
                        and set(labels) <= set(json.loads(m["labels"] or "[]")) and float(m["ram_gb"] or 0) >= min_ram_gb):
                    out.append(m["name"])
        return out

    def returning(self, labels: list, min_ram_gb: float = 0, recipe: str = "") -> list[str]:
        """Nodes that would be eligible but have not called in for over ONLINE_SECONDS, though they were seen within
        RETURN_SECONDS: most likely a worker restarting (after a package job, an update, a reboot), not a node gone.
        A paused node is not returning: it was paused on purpose."""
        at = datetime.now(timezone.utc)
        out = []
        with self._db() as db:
            for m in db.execute("SELECT * FROM machines WHERE revoked IS NULL ORDER BY name").fetchall():
                stats = json.loads(m["stats"] or "{}")
                if recipe and KIND.get(recipe) in self._paused(db, m["name"]):
                    continue
                if (ONLINE_SECONDS < _age(m["last_seen"], at) <= RETURN_SECONDS and not stats.get("pause_file")
                        and set(labels) <= set(json.loads(m["labels"] or "[]")) and float(m["ram_gb"] or 0) >= min_ram_gb):
                    out.append(m["name"])
        return out

    def carriers(self, labels: list) -> list[str]:
        """Unrevoked nodes that carry every label, online or not."""
        with self._db() as db:
            return [m["name"] for m in db.execute("SELECT * FROM machines WHERE revoked IS NULL ORDER BY name").fetchall()
                    if set(labels) <= set(json.loads(m["labels"] or "[]"))]

    # ---- GitHub commit statuses ----
    def status_for(self, row) -> tuple[str, str] | None:
        """(state, description) for a job's commit status now, or None when it reports nothing."""
        if not row["context"]:
            return None
        s = row["state"]
        if s == "queued":
            return "pending", "Queued in Fletchery" + (f" ({row['summary']})" if row["summary"] else "")
        if s == "running":
            return "pending", f"Running on {row['machine']}"
        if s == "passed":
            return "success", row["summary"] or f"Passed on {row['machine']}"
        if s == "failed":
            return "failure", row["summary"] or f"Failed on {row['machine']} (exit {row['exit_code']})"
        return "error", row["summary"] or s.capitalize()

    def post_statuses(self, post) -> int:
        """Send each job's commit status once per change. `post(sha, state, context, description)`; a failure is
        retried on the next call."""
        sent = 0
        with self._db() as db:
            rows = db.execute("SELECT * FROM jobs WHERE context != '' AND status_sent != state || ':' || summary "
                              "ORDER BY id DESC LIMIT 50").fetchall()
        for row in rows:
            st = self.status_for(row)
            if not st:
                continue
            context = ("shadow/" + row["context"]) if self.shadow else row["context"]
            try:
                post(row["sha"], st[0], context, _desc(st[1]))
            except Exception:
                continue
            with self._lock, self._db() as db:
                db.execute("UPDATE jobs SET status_sent=? WHERE id=?", (f"{row['state']}:{row['summary']}", row["id"]))
            sent += 1
        return sent

    # ---- nightly ----
    def nightly_due(self, at_hhmm: str, when: datetime | None = None) -> str | None:
        """The UTC date to run the nightly for, once per day after `at_hhmm` (and within 3 hours of it)."""
        when = when or datetime.now(timezone.utc)
        try:
            h, m = (int(x) for x in at_hhmm.split(":"))
        except ValueError:
            return None
        start = when.replace(hour=h, minute=m, second=0, microsecond=0)
        if not (start <= when < start + timedelta(hours=3)):
            return None
        day = start.date().isoformat()
        with self._db() as db:
            row = db.execute("SELECT value FROM meta WHERE key='nightly'").fetchone()
        return None if row and row["value"] == day else day

    def nightly_done(self, day: str) -> None:
        with self._lock, self._db() as db:
            db.execute("INSERT INTO meta(key, value) VALUES('nightly', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (day,))

    # ---- the dashboard ----
    def job(self, job_id: int) -> dict | None:
        with self._db() as db:
            r = db.execute("SELECT * FROM jobs WHERE id=?", (int(job_id),)).fetchone()
        return _job(r) if r else None

    def log_text(self, job_id: int, tail_bytes: int = 2 * 1024 * 1024) -> str:
        p = self.logs / f"{int(job_id)}.log"
        if not p.exists():
            return ""
        with open(p, "rb") as f:
            size = p.stat().st_size
            if size > tail_bytes:
                f.seek(size - tail_bytes)
            return f.read().decode("utf-8", "replace")

    def _mark(self, job_id: int, text: str) -> None:
        """Keep the progress lines of a job's log, each with the time it arrived, in job-logs/<id>.marks."""
        lines = (self._partial.pop(job_id, "") + text).split("\n")
        tail = lines.pop()
        if tail:
            self._partial[job_id] = tail[-4000:]
        hits = [ln.rstrip("\r") for ln in lines if MARK.match(ln.rstrip("\r"))]
        if hits:
            at = now()
            with open(self.logs / f"{job_id}.marks", "a", encoding="utf-8") as f:
                f.write("".join(f"{at}\t{h[:400]}\n" for h in hits))

    def _marks(self, job_id: int) -> list[tuple[str, str]]:
        p = self.logs / f"{int(job_id)}.marks"
        if p.exists():
            out = []
            for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
                at, _, line = ln.partition("\t")
                out.append((at, line))
            return out
        log = self.logs / f"{int(job_id)}.log"   # a job from before marks were kept: its steps, without times
        if not log.exists() or log.stat().st_size > FALLBACK_LOG_BYTES:
            return []
        return [("", ln.rstrip("\r")) for ln in log.read_text(encoding="utf-8", errors="replace").splitlines()
                if MARK.match(ln.rstrip("\r"))]

    def job_steps(self, job_id: int) -> dict:
        """A job's steps for the Testing tab: {"steps": [...], "expected": [names the last passed run of its recipe had]}."""
        with self._db() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (int(job_id),)).fetchone()
            if not row:
                return {"steps": [], "expected": []}
            prev = db.execute("SELECT id FROM jobs WHERE recipe=? AND state='passed' AND id<? ORDER BY id DESC LIMIT 1",
                              (row["recipe"], int(job_id))).fetchone()
        key = (row["log_bytes"], row["state"])
        hit = self._steps_cache.get(int(job_id))
        if hit and hit[0] == key:
            steps = hit[1]
        else:
            steps = parse_steps(self._marks(job_id))
            self._steps_cache[int(job_id)] = (key, steps)
        expected = []
        if prev and row["state"] in ("queued", "running"):
            expected = [st["name"] for st in self.job_steps(prev["id"])["steps"] if not st.get("cleanup")]
        return {"steps": steps, "expected": expected}

    def view(self, jobs: int = 50) -> dict:
        """Machines, and the newest `jobs` jobs, newest first."""
        at = datetime.now(timezone.utc)
        with self._db() as db:
            machines = db.execute("SELECT * FROM machines ORDER BY name").fetchall()
            recent = db.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (jobs,)).fetchall()
            running = {r["machine"]: r for r in db.execute("SELECT * FROM jobs WHERE state='running'").fetchall()}
            last = {}
            for r in db.execute("SELECT * FROM jobs WHERE state IN ('passed','failed','cancelled','lost') AND machine != '' "
                                "ORDER BY id DESC LIMIT 200").fetchall():
                last.setdefault(r["machine"], r)
            queued = db.execute("SELECT COUNT(*) FROM jobs WHERE state='queued'").fetchone()[0]
            paused = {m["name"]: self._paused(db, m["name"]) for m in machines}
        out = []
        for m in machines:
            job = running.get(m["name"])
            prev = last.get(m["name"])
            out.append({"name": m["name"], "revoked": bool(m["revoked"]), "online": _age(m["last_seen"], at) <= ONLINE_SECONDS,
                        "last_seen": m["last_seen"] or "", "labels": json.loads(m["labels"] or "[]"), "ram_gb": m["ram_gb"],
                        "version": m["version"], "stats": json.loads(m["stats"] or "{}"),
                        "job": dict(_job(job), **self.job_steps(job["id"])) if job else None, "last": _job(prev) if prev else None,
                        "paused_work": paused.get(m["name"], [])})
        return {"machines": out, "jobs": [_job(r) for r in recent], "queued": queued, "shadow": self.shadow,
                "recipes": {k: {"context": v["context"], "kind": KIND.get(k, "")} for k, v in RECIPES.items()},
                "kinds": list(NODE_KINDS)}


class _Closing:
    """sqlite3's own context manager commits but never closes; this one closes."""
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self.db

    def __exit__(self, *exc):
        self.db.close()


def parse_steps(marks: list[tuple[str, str]]) -> list[dict]:
    """Steps from a job's progress lines. Each step: {name, state, exit, start, end, cleanup, sections: [{name, state,
    start, end, detail, checks: [{name, state, detail, at}]}], checks: [...]}. Steps after the first failure only
    tidy up ("cleanup"), so their exit codes don't read as more failures."""
    steps: list[dict] = []
    cur = sec = None
    failed = False
    for at, line in marks:
        m = STEP_END.match(line)
        if m and cur is not None and cur["name"] == m.group(1):
            code = int(m.group(2))
            cur.update(state="passed" if code == 0 else "failed", exit=code, end=at)
            if sec is not None and sec["state"] == "running":
                sec.update(state="passed" if code == 0 else "failed", end=at)
            failed = failed or (code != 0 and not cur["cleanup"])
            cur = sec = None
            continue
        if line.startswith("##[step] "):
            cur = {"name": line[9:].strip(), "state": "running", "exit": None, "start": at, "end": "", "cleanup": failed,
                   "sections": [], "checks": []}
            steps.append(cur)
            sec = None
            continue
        if cur is None:
            continue
        if line.startswith("== "):
            if sec is not None and sec["state"] == "running":
                sec.update(state="passed", end=at)
            sec = {"name": line[3:].strip(), "state": "running", "start": at, "end": "", "detail": "", "checks": []}
            cur["sections"].append(sec)
            continue
        m = RESULT.match(line)
        if m:
            state, name, detail = RESULT_STATE[m.group(1)], m.group(2).strip(), (m.group(3) or "").strip()
            if sec is not None and sec["name"] == name:
                sec.update(state=state, end=at, detail=detail)
                sec = None
                continue
            check = {"name": name, "state": state, "detail": detail, "at": at}
            (sec["checks"] if sec is not None else cur["checks"]).append(check)
            if state == "failed" and sec is not None:
                sec["failed_check"] = True
    for st in steps:
        for sc in st["sections"]:
            if sc.pop("failed_check", False) and sc["state"] == "passed":
                sc["state"] = "failed"
    return steps


def _job(r) -> dict:
    return {"id": r["id"], "recipe": r["recipe"], "sha": r["sha"], "ref": r["ref"], "params": json.loads(r["params"] or "{}"),
            "state": r["state"], "machine": r["machine"], "created": r["created"], "started": r["started"] or "",
            "finished": r["finished"] or "", "exit_code": r["exit_code"], "summary": r["summary"], "context": r["context"],
            "attempts": r["attempts"], "requested_by": r["requested_by"], "cancel": bool(r["cancel"]),
            "log_bytes": r["log_bytes"], "pr": r["pr"]}


def _desc(text: str) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= 140 else text[:137] + "..."


def _clean_stats(stats) -> dict:
    out = {}
    if isinstance(stats, dict):
        for k in ("ram_free_gb", "ram_total_gb", "cpu_percent", "disk_free_gb", "paused"):
            v = stats.get(k)
            if isinstance(v, bool):
                out[k] = v
            elif isinstance(v, (int, float)):
                out[k] = round(float(v), 1)
    return out


def check_params(recipe: str, params: dict) -> dict:
    """Only the parameters a recipe takes, each checked: a job can choose tests, never a command."""
    out = {}
    if recipe == "node-checks":
        only = ",".join(x.strip() for x in str(params.get("only") or "").split(",")) if params.get("only") else ""
        if only and not ONLY.match(only):
            raise ValueError("`only` is a comma list of check names (letters, digits, _)")
        out = {"only": only, "strict_slots": bool(params.get("strict_slots"))}
        imports = ",".join(x.strip() for x in str(params.get("imports") or "").split(",") if x.strip())
        if imports:
            branch = str(params.get("push_branch") or "")
            if not IMPORT_LIST.match(imports):
                raise ValueError("`imports` is a comma list of import steps (Name or Name:map)")
            if not ROUND_BRANCH.match(branch):
                raise ValueError("A job with imports pushes them only to fletchery/round-N (`push_branch`)")
            out.update(imports=imports, push_branch=branch)
    elif recipe == "pr-check":
        try:
            pr = int(params.get("pr") or 0)
        except (TypeError, ValueError):
            pr = 0
        head = str(params.get("head") or "").lower()
        if pr <= 0 or not SHA.match(head):
            raise ValueError("A pr-check job needs `pr` (a number) and `head` (the PR's 40-character head SHA)")
        out = {"pr": pr, "head": head}
    elif recipe == "package":
        version = str(params.get("version") or "").strip()
        if not BUILD_VERSION.match(version):
            raise ValueError("A build's version looks like 0.3.9")
        out = {"version": version, "tests": "full" if params.get("tests") == "full" else ""}
    return out


# ---- the worker API ----
class WorkerApi:
    def __init__(self, machines: Machines, listen: str, log=print):
        self.machines = machines
        self.host, self.port = check_listen(listen)
        self.log = log
        self.server: ThreadingHTTPServer | None = None
        self._fails: dict[str, list[float]] = {}

    def _throttled(self, ip: str) -> bool:
        t = time.monotonic()
        recent = [x for x in self._fails.get(ip, []) if t - x < 60]
        self._fails[ip] = recent
        return len(recent) >= 10

    def handle(self, path: str, machine: str, key: str, ip: str, data: dict) -> tuple[int, dict]:
        if self._throttled(ip):
            return 429, {"error": "too many failed sign-ins; wait a minute"}
        if not self.machines.authenticate(machine, key):
            self._fails.setdefault(ip, []).append(time.monotonic())
            return 401, {"error": "unknown machine or key"}
        m = self.machines
        stats = data.get("stats") if isinstance(data.get("stats"), dict) else None
        try:
            if path == "/v1/hello":
                m.seen(machine, ip, data.get("labels") or [], data.get("ram_gb") or 0, data.get("version") or "", stats)
                self.log(f"machines: {machine} connected from {ip}")
                return 200, {"poll_seconds": 15}
            if path == "/v1/claim":
                m.seen(machine, ip, stats=stats)
                job = None if data.get("paused") else m.claim(machine)
                if job:
                    self.log(f"machines: {machine} took job {job['id']} ({job['recipe']} at {job['sha'][:10]})")
                return 200, {"job": job}
            job_id = int(data.get("job_id") or 0)
            if path == "/v1/heartbeat":
                m.seen(machine, ip, stats=stats)
                return 200, m.heartbeat(machine, job_id)
            if path == "/v1/log":
                text = data.get("text")
                if not isinstance(text, str):
                    return 400, {"error": "text"}
                m.append_log(machine, job_id, text)
                return 200, {"ok": True}
            if path == "/v1/upload":
                import base64
                return 200, m.upload(machine, job_id, str(data.get("name") or ""), int(data.get("offset") or 0),
                                     base64.b64decode(str(data.get("data") or "")), bool(data.get("done")),
                                     str(data.get("sha256") or ""))
            if path == "/v1/assets/list":
                return 200, m.asset_list()
            if path == "/v1/assets/read":
                return 200, m.asset_read(str(data.get("root") or ""), str(data.get("path") or ""),
                                         int(data.get("offset") or 0))
            if path == "/v1/result":
                m.seen(machine, ip, stats=stats)
                m.finish(machine, job_id, int(data.get("exit_code", 1)), str(data.get("summary") or ""), data.get("result"))
                self.log(f"machines: {machine} finished job {job_id} (exit {data.get('exit_code')})")
                return 200, {"ok": True}
        except PermissionError as e:
            return 409, {"error": str(e)}
        except (TypeError, ValueError) as e:
            return 400, {"error": str(e)}
        return 404, {"error": "not found"}

    def start(self) -> None:
        api = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "Fletchery-Machines"

            def log_message(self, *a):
                pass

            def _json(self, code, obj):
                body = json.dumps(obj).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self._json(404, {"error": "not found"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                if length > (MAX_UPLOAD_BODY if self.path == "/v1/upload" else MAX_BODY):
                    return self._json(413, {"error": "request too large"})
                try:
                    data = json.loads(self.rfile.read(length) or b"{}") if length else {}
                    if not isinstance(data, dict):
                        raise ValueError
                except ValueError:
                    return self._json(400, {"error": "bad JSON"})
                auth = self.headers.get("Authorization") or ""
                key = auth[7:] if auth.startswith("Bearer ") else ""
                code, obj = api.handle(self.path, self.headers.get("X-Fletchery-Machine") or "", key,
                                       self.client_address[0], data)
                self._json(code, obj)

        self.server = ThreadingHTTPServer((self.host, self.port), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, name="fletchery-machines-api", daemon=True).start()

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()


class Coordinator:
    """What the backend runs for the machines: the worker API, and a thread that requeues lost jobs, starts the
    nightly and sends commit statuses."""

    def __init__(self, cfg, gh, log=print):
        conf = getattr(cfg, "machines", None) or {}
        self.conf = conf
        self.gh = gh
        self.log = log
        self.machines = Machines(cfg.state_dir, cfg.repo, shadow=conf.get("shadow", True) is not False)
        roots = self.machines.set_asset_roots(conf.get("assetRoots") or [])
        for r in self.machines.missing_asset_roots():
            log(f"machines: shared asset folder {r} is missing or empty on this PC; node imports will refuse to start")
        if roots:
            log(f"machines: nodes mirror {', '.join(roots)} before imports")
        self.api = WorkerApi(self.machines, conf["listen"], log) if conf.get("listen") else None
        self._stop = threading.Event()
        self.error = ""

    def start(self) -> None:
        if self.api:
            try:
                self.api.start()
                self.log(f"machines: worker API on {self.api.host}:{self.api.port}")
            except (OSError, ValueError) as e:
                self.error = f"Worker API not started: {e}"
                self.log("machines: " + self.error)
                self.api = None
        threading.Thread(target=self._run, name="fletchery-machines", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        if self.api:
            self.api.stop()

    def post(self, sha, state, context, description) -> None:
        self.gh.api(f"repos/{self.machines.repo}/statuses/{sha}", "POST",
                    {"state": state, "context": context, "description": description})

    def resolve(self, ref: str) -> str:
        ref = (ref or "").strip()
        if SHA.match(ref.lower()):
            return ref.lower()
        if not ref or not re.match(r"^[A-Za-z0-9._/-]{1,200}$", ref) or ".." in ref:
            raise ValueError("Give a branch name or a full commit SHA")
        data = self.gh.api(f"repos/{self.machines.repo}/commits/{ref}", fresh=True)
        return str((data or {}).get("sha") or "")

    def tick(self, at: datetime | None = None) -> None:
        for job in self.machines.reap(at):
            self.log(f"machines: job {job} lost its machine")
        for stopped, machine, wanted in self.machines.preempt_for_rounds(at):
            self.log(f"machines: stopping job {stopped} on {machine} so merge-round job {wanted} can run now; "
                     "it runs again later")
        nightly = self.conf.get("nightly") or {}
        if nightly.get("at"):
            day = self.machines.nightly_due(nightly["at"], at)
            if day:
                try:
                    branch = nightly.get("branch") or "main"
                    sha = self.resolve(branch)
                    job = self.machines.enqueue(nightly.get("recipe") or "nightly-full", sha, ref=branch,
                                                requested_by=f"nightly {day}")
                    self.machines.nightly_done(day)
                    self.log(f"machines: nightly {day} queued as job {job} ({branch} at {sha[:10]})")
                except Exception as e:
                    self.log(f"machines: nightly not queued yet: {type(e).__name__}: {e}")
        self.machines.post_statuses(self.post)

    def _run(self) -> None:
        while not self._stop.wait(10):
            try:
                self.tick()
            except Exception as e:  # never take the backend down
                self.log(f"machines: {type(e).__name__}: {e}")


NO_NODE_MINUTES = 10   # `run`: give up (no result) when no node has taken the job by then


def split_checks(only: str, parts: int) -> list[str]:
    """The round's check names dealt round-robin into `parts` lists (largest first), so two nodes share one run."""
    names = [x.strip() for x in str(only or "").split(",") if x.strip()]
    parts = max(1, min(parts, len(names)))
    return [",".join(names[i::parts]) for i in range(parts)] if names else [""]


def run_job(m: "Machines", recipe: str, sha: str, params: dict | None = None, requested_by: str = "", labels=None,
            timeout_minutes: float = 0, poll: float = 15, out=print, clock=None, sleep=None) -> int:
    """Queue the job, wait for it, print its result and the end of its log. 0 passed, 1 failed (the tests failed),
    2 no result: no node can take it, nobody took it within NO_NODE_MINUTES, it was lost or cancelled, or the job
    itself reported "no result" (exit 2). A merge round runs its long tests this way (rounds.node_tests_text).
    With two or more nodes free for it and a list of checks (`only`), the checks are split across the nodes and run at
    the same time (Alex, 2026-11-12: "Can both machines run in parallel and speed things up?"); a run that imports
    content is never split, because the imports must happen once."""
    import time
    clock, sleep = clock or time.monotonic, sleep or time.sleep
    params = dict(params or {})
    labels = list(labels or [])
    if params.get("imports") and m.asset_roots():
        missing = m.missing_asset_roots()
        if missing:
            out(f"No result: the shared asset folder {', '.join(missing)} is missing or empty on this PC, so the "
                f"imports cannot run anywhere. Restore it (or fix workers.machines.assetRoots) and run the round again.")
            return 2
        if ASSET_SYNC_LABEL not in labels:   # only a worker that mirrors the asset folders first may import
            labels.append(ASSET_SYNC_LABEL)
    need = list(RECIPES[recipe]["labels"]) + [x for x in labels if x not in RECIPES[recipe]["labels"]]
    names = m.eligible(need, RECIPES[recipe]["min_ram_gb"], recipe)
    if not names:
        # A node that dropped off a minute ago is usually its worker restarting (2026-11-13, round 165: SF-NODE-01
        # restarted after a package job, both `run` attempts fell inside the gap and the round imported on the PC).
        back = m.returning(need, RECIPES[recipe]["min_ram_gb"], recipe)
        if back:
            out(f"{', '.join(back)} was seen recently but is not calling in; waiting up to {RETURN_WAIT_MINUTES} min "
                "for it to come back...")
            start = clock()
            while not names and clock() - start < RETURN_WAIT_MINUTES * 60:
                sleep(poll)
                names = m.eligible(need, RECIPES[recipe]["min_ram_gb"], recipe)
    if not names:
        if not m.carriers(need):
            out(f"No result: no node has the labels {' '.join(need)} (no node carries them at all).")
        else:
            out(f"No result: no online node takes {recipe} with labels {' '.join(need)} (offline, paused or busy "
                "elsewhere).")
        return 2
    chunks = [params.get("only", "")] if params.get("imports") else split_checks(params.get("only", ""), len(names))
    ids = []
    for chunk in chunks:
        ids.append(m.enqueue(recipe, sha, {**params, "only": chunk} if len(chunks) > 1 else params,
                             requested_by=requested_by, labels=labels))
    if len(ids) > 1:
        out(f"Jobs {', '.join(map(str, ids))} queued: {recipe} at {sha[:10]}, the checks split across {len(ids)} nodes "
            f"({' | '.join(chunks)}). Waiting for the nodes...")
    else:
        out(f"Job {ids[0]} queued: {recipe} at {sha[:10]}. Waiting for a node...")
    start, limit = clock(), (timeout_minutes or RECIPES[recipe]["timeout_minutes"] + NO_NODE_MINUTES) * 60
    seen_running: set = set()
    jobs: dict = {}
    while True:
        jobs = {i: m.job(i) for i in ids}
        for i, job in jobs.items():
            if job["state"] == "running" and i not in seen_running:
                seen_running.add(i)
                out(f"Job {i} running on {job['machine']}.")
        if all(j["state"] in DONE for j in jobs.values()):
            break
        waited = clock() - start
        # A split job still queued behind its sibling is waiting for a node to free up, not abandoned.
        nobody = waited > NO_NODE_MINUTES * 60 and all(j["state"] == "queued" for j in jobs.values() if j["state"] not in DONE)
        if nobody and not any(j["state"] == "running" for j in jobs.values()) or waited > limit:
            for i, job in jobs.items():
                if job["state"] not in DONE:
                    m.cancel(i)
            out(f"No result: job {', '.join(str(i) for i, j in jobs.items() if j['state'] not in DONE)} "
                f"{'was not taken by any node' if nobody else 'ran too long'}; cancelled.")
            return 2
        sleep(poll)
    codes = []
    for i, job in jobs.items():
        tail = "\n".join(m.log_text(i, 256 * 1024).replace("\r\n", "\n").rstrip().split("\n")[-60:])
        if tail:
            out(tail)
        out(f"Job {i} on {job['machine']}: {job['state']}: {job['summary']}")
        codes.append(0 if job["state"] == "passed" else 1 if job["state"] == "failed" and job.get("exit_code") != 2 else 2)
    return 1 if 1 in codes else max(codes)


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Fletchery machines: nodes and their jobs")
    ap.add_argument("--state-dir", required=True)
    ap.add_argument("--repo", default="")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("add").add_argument("name")
    sub.add_parser("revoke").add_argument("name")
    sub.add_parser("list")
    q = sub.add_parser("enqueue")
    q.add_argument("--recipe", required=True, choices=sorted(RECIPES))
    q.add_argument("--sha", required=True)
    q.add_argument("--only", default="")
    q.add_argument("--strict-slots", action="store_true")
    q.add_argument("--pr", type=int, default=0)
    c = sub.add_parser("cancel")
    c.add_argument("id", type=int)
    r = sub.add_parser("run", help="queue a job, wait for it and exit 0 passed / 1 failed / 2 no result")
    r.add_argument("--recipe", required=True, choices=["node-checks"])
    r.add_argument("--sha", required=True)
    r.add_argument("--only", default="")
    r.add_argument("--strict-slots", action="store_true")
    r.add_argument("--imports", default="", help="Lane-Imports steps to run and push first (needs --push-branch)")
    r.add_argument("--push-branch", default="", help="fletchery/round-N: where the node pushes the imported content")
    r.add_argument("--label", action="append", default=[])
    r.add_argument("--requested-by", default="command line")
    r.add_argument("--timeout-minutes", type=float, default=0)
    a = ap.parse_args(argv)
    m = Machines(a.state_dir, a.repo)
    if a.cmd == "add":
        print(m.add_machine(a.name))
        print(f"Keep this key on {a.name} only (it is not shown again). Revoke with: revoke {a.name}")
    elif a.cmd == "revoke":
        print("revoked" if m.revoke(a.name) else "no such machine, or already revoked")
    elif a.cmd == "list":
        print(json.dumps(m.view(), indent=2))
    elif a.cmd == "enqueue":
        print(m.enqueue(a.recipe, a.sha, {"only": a.only, "strict_slots": a.strict_slots}, requested_by="command line",
                        pr=a.pr))
    elif a.cmd == "cancel":
        print(m.cancel(a.id))
    elif a.cmd == "run":
        params = {"only": a.only, "strict_slots": a.strict_slots}
        if a.imports:
            params.update(imports=a.imports, push_branch=a.push_branch)
            a.label = list(a.label) + ([IMPORT_LABEL] if IMPORT_LABEL not in a.label else [])
        return run_job(m, a.recipe, a.sha, params, a.requested_by[:100],
                       a.label, a.timeout_minutes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
