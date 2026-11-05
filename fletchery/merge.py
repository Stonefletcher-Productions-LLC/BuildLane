"""What the merge round is doing right now, for the Merge section: one plain headline plus its steps.

Two sources, both plain files on the build PC:

    merge-round.json   written by whoever runs the release round (the release thread, or a person), in Fletchery's
                       state folder unless the profile's `workers.mergeRoundFile` names another path:
        {"round": "7", "branch": "claude/release-round7", "worktree": "E:\\lifeshare-scratch\\release-034-round",
         "owner": "release thread", "started": "2026-11-03T13:40:00Z", "updated": "2026-11-03T15:05:00Z",
         "prs": [835, 830, 843],
         "steps": [{"name": "Merge the PRs", "state": "done"}, {"name": "BuildTown", "state": "waiting"},
                   {"name": "NavCheck", "state": "todo"}, {"name": "Game specs", "state": "todo"}],
         "note": "waiting for the PC"}
       step states: todo, running, waiting, done, failed. "state": "done" at the top level means the round landed.
    heavy.json         the PC's heavy lease (Tools/CI/HeavyLease.ps1): who holds the PC for editor/import/test work.

Nothing here writes or merges; it only reads.
"""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from functools import lru_cache
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

STATES = ("todo", "running", "waiting", "done", "failed")
STALE_HOURS = 2.0
TERMINAL_STATES = {"done", "landed", "merged", "closed", "failed", "crashed", "cancelled",
                   "canceled", "interrupted", "aborted", "stopped", "limited", "unreachable",
                   "checkout_failed", "nothing"}


def _read(path) -> dict:
    try:
        p = Path(path)
        data = json.loads(p.read_text(encoding="utf-8-sig")) if path and p.exists() else {}
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _age_h(iso: str, now: datetime) -> float | None:
    try:
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (now - t).total_seconds() / 3600


def _short(iso: str) -> str:
    """'2026-11-03T15:16:56.71Z' -> '15:16Z'."""
    s = str(iso or "")
    return s[11:16] + "Z" if len(s) >= 16 else s


def pc_holder(lease_path) -> dict:
    """Who holds the PC's heavy lease: {} when free. `what` is a short plain name (the script and its workspace)."""
    d = _read(lease_path)
    if not d.get("token"):
        return {}
    purpose = Path(str(d.get("purpose") or "?").replace("\\", "/")).name
    ws = Path(str(d.get("workspaces") or "").split(";")[0].replace("\\", "/")).name
    return {"what": f"{purpose} in {ws}" if ws else purpose, "since": d.get("acquiredUtc", ""), "pid": d.get("pid")}


def in_branch(worktree: str, branch: str, sha: str) -> bool | None:
    """Is commit `sha` already in the round branch? None when it can't be told (no worktree, unknown commit)."""
    if not (worktree and branch and sha) or not Path(worktree).exists():
        return None
    try:
        p = subprocess.run(["git", "-C", worktree, "merge-base", "--is-ancestor", sha, branch],
                           capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return True if p.returncode == 0 else False if p.returncode == 1 else None



def _text(value, limit=1600) -> str:
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", str(value or ""))[:limit]


def _step(raw: dict) -> dict:
    state = str(raw.get("state", "todo")).lower()
    out = {"name": _text(raw.get("name", "?"), 160), "state": state if state in STATES else "todo"}
    for key in ("started", "updated", "ended"):
        if raw.get(key):
            out[key] = _text(raw[key], 64)
    message = raw.get("message") or raw.get("note")
    if message:
        out["message"] = _text(message)
    duration = raw.get("duration_seconds")
    if isinstance(duration, (int, float)) and not isinstance(duration, bool) and 0 <= duration <= 365*86400:
        out["duration_seconds"] = duration
    out["checks"] = []
    checks = raw.get("checks", [])
    for check in checks[:80] if isinstance(checks, list) else []:
        if isinstance(check, dict):
            child = _step({k:v for k,v in check.items() if k not in ("checks", "activity", "files")})
            child.pop("checks", None)
            out["checks"].append(child)
    activity = raw.get("activity", [])
    if isinstance(activity, list):
        out["activity"] = [{"line": _text(item.get("line")), "at": _text(item.get("at"), 64)}
                           for item in activity[-16:] if isinstance(item, dict) and item.get("line")]
    files = raw.get("files", [])
    if isinstance(files, list):
        out["files"] = [{"name": _text(item.get("name") if isinstance(item, dict) else item, 200)} for item in files[:16]]
    return out


def _linked(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


@lru_cache(maxsize=96)
def _tail(path: str, size: int, modified_ns: int) -> tuple[str, ...]:
    # Cache by identity; at most 32 KiB is read, never a multi-megabyte engine log.
    with Path(path).open("rb") as stream:
        stream.seek(max(0, size - 32768))
        data = stream.read(32768)
    lines = data.decode("utf-8-sig", errors="replace").splitlines()
    if size > 32768 and lines:
        lines = lines[1:]
    return tuple(_text(line, 500) for line in lines[-100:] if line.strip())


def _checks(lines: tuple[str, ...]) -> list[dict]:
    result = {}
    for line in lines:
        match = re.match(r"^(START|PASS|FAIL|FAILED)\s+(.+?)(?:\s+at\s+(\d\d:\d\d:\d\d)|\s+\((\d+(?:\.\d+)?)s\))?$", line.strip())
        if not match:
            continue
        kind, name, started, duration = match.groups()
        if len(name) > 160:
            continue
        item = {"name":name,"state":"running" if kind == "START" else "done" if kind == "PASS" else "failed"}
        if started:
            item["started"] = started  # Local clock text, not an invented date/zone.
        if duration:
            item["duration_seconds"] = float(duration)
        result[name] = item
    return list(result.values())[-60:]


def _log_step(name: str, steps: list[dict]) -> dict | None:
    name = name.lower()
    if "auto-game" in name:
        words = ("game", "spec")
    elif "navcheck" in name:
        words = ("nav",)
    elif "spec" in name:
        words = ("spec",)
    elif "package" in name:
        words = ("package",)
    elif "content" in name or "import" in name:
        words = ("content", "import")
    elif "build" in name:
        words = ("build", "compile")
    else:
        return None
    return next((step for step in steps if any(word in step["name"].lower() for word in words)), None)


def _log_folder(r: dict, root) -> Path:
    number = str(r.get("round", ""))
    if not root or not number.isdigit() or not r.get("worktree"):
        raise ValueError("No current round log folder")
    original_root = Path(root).absolute()
    if any(_linked(part) for part in (original_root, *original_root.parents)):
        raise ValueError("Linked configured log root")
    allowed = original_root.resolve()
    checkout = Path(r["worktree"])
    if not checkout.is_absolute() or checkout.resolve().parent != allowed:
        raise ValueError("Unapproved worktree")
    folder = checkout / "Saved" / ("MergeRound" + number)
    for part in (checkout, checkout / "Saved", folder):
        if _linked(part) or not part.resolve().is_relative_to(allowed):
            raise ValueError("Linked log folder")
    return folder


def _activity(r: dict, steps: list[dict], root, now: datetime) -> tuple[dict, list[dict]]:
    activity = {"updated":r.get("updated") or r.get("started") or "", "source":"Progress checkpoint",
                "lines":[], "health":"unknown", "age_seconds":None}
    files = []
    number = str(r.get("round", ""))
    try:
        if root and number.isdigit() and r.get("worktree"):
            folder = _log_folder(r, root)
            candidates = []
            with os.scandir(folder) as entries:
                for index, entry in enumerate(entries):
                    if index >= 256:
                        break
                    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,150}\.(?:log|txt)", entry.name):
                        continue
                    info = entry.stat(follow_symlinks=False)
                    if not stat.S_ISREG(info.st_mode) or entry.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                        continue
                    candidates.append((entry.name, info))
            candidates.sort(key=lambda pair:pair[1].st_mtime_ns, reverse=True)
            assigned = set()
            checkpoint_stamp = datetime.fromisoformat(str(r.get("updated") or r.get("started") or "").replace("Z", "+00:00"))
            if checkpoint_stamp.tzinfo is None:
                checkpoint_stamp = checkpoint_stamp.replace(tzinfo=timezone.utc)
            for name, info in candidates[:12]:
                relative = "Saved/MergeRound" + number + "/" + name
                stamp = datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat()
                url = "/api/merge/log?" + urlencode({"round":number,"name":name})
                files.append({"name":relative,"updated":stamp,"bytes":info.st_size,"url":url})
                lines = _tail(str(folder/name), info.st_size, info.st_mtime_ns)
                target = _log_step(name, steps)
                # A fresh checkpoint can report repair while old logs remain.
                # Show their source/age, never change the checkpoint's step state.
                if target and id(target) not in assigned:
                    if not target.get("activity"):
                        target["activity"] = [{"line":line} for line in lines[-12:]]
                        target["activity_source"] = relative
                        target["activity_updated"] = stamp
                    else:
                        target["activity_source"] = "Progress checkpoint"
                        target["activity_updated"] = target.get("updated") or r.get("updated") or ""
                    target["files"] = (target.get("files") or [])[:15] + [{"name":relative,"url":url}]
                    target["log_updated"] = stamp
                    target["log_source"] = relative
                    target["checks"] = target.get("checks") or _checks(lines)
                    assigned.add(id(target))
                if not activity["lines"] and lines and info.st_mtime >= checkpoint_stamp.timestamp() - 900:
                    activity.update(updated=stamp,source=relative,lines=[{"line":line} for line in lines[-12:]])
    except (OSError, ValueError, TypeError):
        pass  # Missing/unapproved logs are unknown evidence, never a stalled job.
    age = _age_h(activity["updated"], now)
    if age is not None:
        activity["age_seconds"] = max(0, int(age*3600))
        activity["health"] = "active" if age*3600 <= 180 else "quiet" if age*3600 < 900 else "possibly_stuck"
    return activity, files


def log_text(round_path, worktree_root, number: str, name: str) -> str:
    """Only the current round's exposed log files, bounded to the latest 1 MiB."""
    r = _read(round_path)
    if not str(number).isdigit() or str(r.get("round")) != str(number):
        raise ValueError("That is not the current round")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,150}\.(?:log|txt)", name):
        raise ValueError("Invalid log name")
    folder = _log_folder(r, worktree_root)
    _, files = _activity(r, [], worktree_root, datetime.now(timezone.utc))
    expected = "Saved/MergeRound" + str(number) + "/" + name
    if expected not in {item["name"] for item in files}:
        raise ValueError("Log is unavailable")
    path = folder/name
    if _linked(path) or path.resolve().parent != folder.resolve() or not path.is_file():
        raise ValueError("Linked log is unavailable")
    with path.open("rb") as stream:
        size = path.stat().st_size
        truncated = size > 1024*1024
        stream.seek(max(0, size-1024*1024))
        body = stream.read(1024*1024).decode("utf-8-sig", errors="replace")
    return ("[Showing the latest 1 MiB; earlier output omitted.]\n" if truncated else "") + body


def refresh(round_path, lease_path, cached: dict, worktree_root, now: datetime | None = None) -> dict:
    """Fresh read-only status without Git subprocesses on each browser poll."""
    out = view(round_path, lease_path, now=now, worktree_root=worktree_root)
    if out.get("active") and str(out.get("round")) == str(cached.get("round")):
        out["in_branch"] = dict(cached.get("in_branch") or {})
    return out

def view(round_path, lease_path, head_of: dict[int, str] | None = None, now: datetime | None = None,
         worktree_root=None) -> dict:
    """The Merge section's top: {"headline", "steps", "round", "prs", "in_branch": {pr: bool}, "pc", ...}."""
    now = now or datetime.now(timezone.utc)
    r = _read(round_path)
    pc = pc_holder(lease_path)
    pc_text = f"the PC is busy with {pc['what']} (since {_short(pc['since'])})" if pc else "the PC is free"
    out = {"round": "", "prs": [], "in_branch": {}, "steps": [], "pc": pc, "pc_text": pc_text, "state": "idle",
           "headline": "", "next": "", "started": "", "updated": "", "owner": "", "note": "", "stale": False,
           "active": False, "previous": None}
    raw_steps = r.get("steps", [])
    steps = [_step(s) for s in raw_steps[:64] if isinstance(s, dict)] if isinstance(raw_steps, list) else []
    for s in steps:
        if s["state"] not in STATES:
            s["state"] = "todo"
    prs = [int(n) for n in r.get("prs", []) if str(n).isdigit()]
    state = str(r.get("state", "")).lower()
    active = bool(r) and state not in TERMINAL_STATES
    if not active:
        if r:
            # A finished round is history. Never query its branches or reuse its
            # failed steps/PR membership as the current scheduler's status.
            ended = r.get("ended") or r.get("finished") or r.get("updated") or ""
            duration = None
            try:
                end = datetime.fromisoformat(str(ended).replace("Z", "+00:00"))
                if end.tzinfo is None:
                    end = end.replace(tzinfo=timezone.utc)
                age = _age_h(r.get("started", ""), end)
                if age is not None and age >= 0:
                    duration = int(round(age * 3600))
            except ValueError:
                pass
            out["previous"] = {"round": str(r.get("round", "")), "result": r.get("result") or state,
                               "steps": steps, "prs": prs, "started": r.get("started", ""),
                               "ended": ended, "duration_seconds": duration,
                               "note": r.get("note", ""), "owner": r.get("owner", "")}
        out["headline"] = "No merge round running. Finished PRs below wait for the next one; " + pc_text + "."
        return out
    out["active"] = True
    out.update(round=str(r.get("round", "")), prs=prs, steps=steps, started=r.get("started", ""),
               updated=r.get("updated", ""), owner=r.get("owner", ""), note=r.get("note", ""))
    for n in prs:
        sha = (head_of or {}).get(n, "")
        out["in_branch"][n] = in_branch(r.get("worktree", ""), r.get("branch", ""), sha) if sha else None
    merged = sum(1 for v in out["in_branch"].values() if v)
    known = sum(1 for v in out["in_branch"].values() if v is not None)
    out["activity"], out["files"] = _activity(r, steps, worktree_root, now)
    out["failed_steps"] = [s["name"] for s in steps if s["state"] == "failed"]
    out["reported_activity"] = [s["name"] for s in steps if s["state"] == "running"]
    cur = next((i for i, s in enumerate(steps) if s["state"] == "failed"), None)
    if cur is None:
        cur = next((i for i, s in enumerate(steps) if s["state"] in ("running", "waiting")), None)
    if cur is None:
        cur = next((i for i, s in enumerate(steps) if s["state"] == "todo"), None)
    nxt = next((s["name"] for s in steps[(cur + 1 if cur is not None else 0):] if s["state"] == "todo"), "")
    if out["failed_steps"]:
        nxt = "Repair or exclude the failing PRs, then revalidate the remaining candidate before landing"
    out["next"] = nxt
    name = f"round {out['round']}" if out["round"] else "the merge round"
    bits = [f"Merging {name}"]
    if prs:
        many = "PRs" if len(prs) != 1 else "PR"
        bits.append(f"{merged} of {len(prs)} {many} in the round branch" if known else f"{len(prs)} {many}")
    if cur is not None and steps:
        s = steps[cur]
        verb = {"running": "running", "waiting": "waiting to run", "failed": "FAILED at", "todo": "next is"}[s["state"]]
        bits.append(f"step {cur + 1} of {len(steps)}, {verb} {s['name']}")
        out["state"] = "failed" if s["state"] == "failed" else "waiting" if s["state"] == "waiting" else "running"
    elif steps and all(s["state"] == "done" for s in steps):
        bits.append("every step done, landing")
        out["state"] = "running"
    head = ": ".join([bits[0], ", ".join(bits[1:])]) if len(bits) > 1 else bits[0]
    tail = []
    if out["state"] == "waiting":
        tail.append(f"Waiting because {pc_text}" if pc else "Waiting, and the PC is free, so it should start soon")
    elif out["state"] != "failed":
        tail.append(pc_text[0].upper() + pc_text[1:])
    if out["started"]:
        tail.append(f"started {_short(out['started'])}")
    if nxt and out["state"] != "failed":
        tail.append(f"then {nxt}")
    age = _age_h(out["updated"] or out["started"], now)
    if age is not None and age >= STALE_HOURS and not pc:
        out["stale"] = True
        tail.append(f"no update for {age:.0f} h and nothing holds the PC, so it may be stuck")
    out["headline"] = head + ". " + "; ".join(tail) + "." if tail else head + "."
    if out["note"]:
        out["headline"] += f" Note: {out['note']}"
    return out
