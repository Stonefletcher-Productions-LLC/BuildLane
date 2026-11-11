"""The Build Lane, as the Fletchery dashboard sees it: read the same files the Buildlane app reads (Docs/BuildLane.md,
"The app"), and pause or resume builds the same way Tools/CI/Pause.ps1 does. Nothing here talks to GitHub."""
from __future__ import annotations

import getpass
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

STALE_MINUTES = 10  # the Buildlane app warns when a running build has said nothing for 10 minutes
OLD_HOURS = 24  # a finished status this old is a leftover: its queue is a snapshot, not PRs still waiting


def _parse_time(s: str) -> datetime | None:
    if not s:
        return None
    s = re.sub(r"(\.\d{6})\d+", r"\1", s.strip())  # PowerShell writes 7 fractional digits
    try:
        t = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def read(status_file: str, pause_file: str, drive: str = "", min_free_gb: float = 12.0,
         foreman_prs: set[int] | None = None, now: datetime | None = None) -> dict:
    """Everything the lane panel shows. Never raises: a missing or half-written file is reported, not fatal."""
    now = now or datetime.now(timezone.utc)
    foreman_prs = foreman_prs or set()
    out: dict = {"available": False, "paused": False, "paused_note": "", "free_gb": None, "low_disk": False}

    pf = Path(pause_file) if pause_file else None
    if pf and pf.exists():
        out["paused"] = True
        try:
            out["paused_note"] = pf.read_text(encoding="utf-8-sig", errors="replace").strip()[:200]
        except OSError:
            pass

    if drive:
        root = drive if drive.endswith((":\\", "/")) else (drive.rstrip(":") + ":\\" if len(drive.rstrip(":")) == 1 else drive)
        try:
            out["free_gb"] = round(shutil.disk_usage(root).free / 1024 ** 3, 1)
            out["low_disk"] = out["free_gb"] < min_free_gb
            out["min_free_gb"] = min_free_gb
        except OSError:
            pass

    sf = Path(status_file) if status_file else None
    if not sf or not sf.exists():
        out["error"] = f"No Build Lane status at {status_file}." if status_file else "No Build Lane status file configured."
        return out
    try:
        st = json.loads(sf.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        out["error"] = f"Build Lane status could not be read ({type(e).__name__}); it is probably being rewritten."
        return out

    out["available"] = True
    phases = [str(p) for p in (st.get("phases") or [])]
    done = str(st.get("done") or "")
    started, updated = _parse_time(st.get("started", "")), _parse_time(st.get("updated", ""))
    what = str(st.get("what") or "")
    progress = st.get("progress") or None
    in_build = sorted({int(n) for n in re.findall(r"#(\d+)", what)})
    out.update({
        "what": what, "step": str(st.get("step") or ""), "detail": str(st.get("detail") or ""), "done": done,
        "phase": int(st.get("phase", -1)) if str(st.get("phase", "")).lstrip("-").isdigit() else -1,
        "phases": phases, "progress": progress if isinstance(progress, dict) else None,
        "started": started.isoformat() if started else "", "updated": updated.isoformat() if updated else "",
        "minutes": int((((updated if done else now) - started).total_seconds()) // 60) if started else None,
        "run_url": str(st.get("runUrl") or ""), "issue_url": str(st.get("issueUrl") or ""),
        "notes": [str(n) for n in (st.get("notes") or [])][-3:],
        "building_prs": in_build, "foreman_building": sorted(set(in_build) & foreman_prs),
        "queue": [{"number": int(q.get("number", 0)), "title": str(q.get("title", "")), "state": str(q.get("state", "")),
                   "foreman": int(q.get("number", 0)) in foreman_prs}
                  for q in (st.get("queue") or []) if isinstance(q, dict)],
    })
    out["stale"] = bool(not done and updated and (now - updated).total_seconds() > STALE_MINUTES * 60)
    # The lane rewrites its queue only when it runs. Once it has sat finished for a day (or was retired in favour of
    # merge rounds) that list is days old and names PRs long merged, so don't show it as "waiting for a build".
    out["old"] = bool(done and updated and (now - updated).total_seconds() > OLD_HOURS * 3600)
    if out["old"]:
        out["old_queue"] = len(out["queue"])
        out["queue"] = []
    return out


def set_paused(pause_file: str, paused: bool) -> None:
    """Same file and wording as Tools/CI/Pause.ps1: while it exists the lane starts nothing new, and a job that starts
    waits in its first step. A build already running finishes."""
    pf = Path(pause_file)
    if paused:
        pf.parent.mkdir(parents=True, exist_ok=True)
        pf.write_text(f"Paused {datetime.now().strftime('%Y-%m-%dT%H:%M:%S')} by {getpass.getuser()} (Fletchery app)",
                      encoding="utf-8")
    else:
        try:
            pf.unlink()
        except FileNotFoundError:
            pass
