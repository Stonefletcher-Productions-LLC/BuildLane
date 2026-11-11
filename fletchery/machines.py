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
     "nightly": {"at": "07:00", "branch": "main", "recipe": "nightly-full"}}   # optional; UTC

The worker API (all POST, JSON, `Authorization: Bearer <key>` and `X-Fletchery-Machine: <name>`):
    /v1/hello      {labels, ram_gb, disk_free_gb, version}          -> {poll_seconds}
    /v1/claim      {stats}                                          -> {job} or {job: null}
    /v1/heartbeat  {job_id, stats}                                  -> {cancel}
    /v1/log        {job_id, text}                                   -> {ok}
    /v1/result     {job_id, exit_code, summary, result}             -> {ok}

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
LOST_SECONDS = 180           # a running job without a heartbeat for this long is lost (the node stopped or rebooted)
MAX_ATTEMPTS = 2             # a lost job is queued again once, then fails
MAX_BODY = 1024 * 1024
LOG_MAX_BYTES = 50 * 1024 * 1024
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
SHA = re.compile(r"^[0-9a-f]{40}$")
LABEL = re.compile(r"^[A-Za-z0-9_.:+-]{1,64}$")
ONLY = re.compile(r"^[A-Za-z0-9_]+(,[A-Za-z0-9_]+)*$")

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
        """The oldest queued job this node can run: every label it needs is one of the node's, and the node has the
        RAM. A node runs one job at a time."""
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
                for row in db.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY id").fetchall():
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
            db.execute("UPDATE jobs SET log_bytes=log_bytes+?, heartbeat=? WHERE id=?", (len(data), now(), job_id))

    def finish(self, machine: str, job_id: int, exit_code: int, summary: str = "", result=None) -> None:
        with self._lock, self._db() as db:
            row = self._own(db, job_id, machine)
            state = "cancelled" if row["cancel"] else ("passed" if int(exit_code) == 0 else "failed")
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
                if (_age(m["last_seen"], at) <= ONLINE_SECONDS and not stats.get("paused")
                        and set(labels) <= set(json.loads(m["labels"] or "[]")) and float(m["ram_gb"] or 0) >= min_ram_gb):
                    out.append(m["name"])
        return out

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
                        "job": _job(job) if job else None, "last": _job(prev) if prev else None,
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
                if length > MAX_BODY:
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
