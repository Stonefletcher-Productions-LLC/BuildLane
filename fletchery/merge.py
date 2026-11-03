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
import subprocess
from datetime import datetime, timezone
from pathlib import Path

STATES = ("todo", "running", "waiting", "done", "failed")
STALE_HOURS = 2.0


def _read(path) -> dict:
    try:
        p = Path(path)
        return json.loads(p.read_text(encoding="utf-8-sig")) if path and p.exists() else {}
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


def view(round_path, lease_path, head_of: dict[int, str] | None = None, now: datetime | None = None) -> dict:
    """The Merge section's top: {"headline", "steps", "round", "prs", "in_branch": {pr: bool}, "pc", ...}."""
    now = now or datetime.now(timezone.utc)
    r = _read(round_path)
    pc = pc_holder(lease_path)
    pc_text = f"the PC is busy with {pc['what']} (since {_short(pc['since'])})" if pc else "the PC is free"
    out = {"round": "", "prs": [], "in_branch": {}, "steps": [], "pc": pc, "pc_text": pc_text, "state": "idle",
           "headline": "", "next": "", "started": "", "updated": "", "owner": "", "note": "", "stale": False}
    active = r and str(r.get("state", "")).lower() not in ("done", "landed", "merged", "closed")
    if not active:
        out["headline"] = "No merge round running. Finished PRs below wait for the next one; " + pc_text + "."
        return out
    steps = [{"name": str(s.get("name", "?")), "state": str(s.get("state", "todo")).lower()}
             for s in r.get("steps", []) if isinstance(s, dict)]
    for s in steps:
        if s["state"] not in STATES:
            s["state"] = "todo"
    prs = [int(n) for n in r.get("prs", []) if str(n).isdigit()]
    out.update(round=str(r.get("round", "")), prs=prs, steps=steps, started=r.get("started", ""),
               updated=r.get("updated", ""), owner=r.get("owner", ""), note=r.get("note", ""))
    for n in prs:
        sha = (head_of or {}).get(n, "")
        out["in_branch"][n] = in_branch(r.get("worktree", ""), r.get("branch", ""), sha) if sha else None
    merged = sum(1 for v in out["in_branch"].values() if v)
    known = sum(1 for v in out["in_branch"].values() if v is not None)
    cur = next((i for i, s in enumerate(steps) if s["state"] in ("running", "waiting", "failed")), None)
    if cur is None:
        cur = next((i for i, s in enumerate(steps) if s["state"] == "todo"), None)
    nxt = next((s["name"] for s in steps[(cur + 1 if cur is not None else 0):] if s["state"] == "todo"), "")
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
