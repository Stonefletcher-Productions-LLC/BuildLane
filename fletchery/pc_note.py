"""Let a job that runs outside Fletchery say what it is, so the dashboard can show it under Building.

The PC's heavy lease only names the script that holds it right now ("Build.ps1 in release-037") and is free between
two scripts of the same job. A release build or a thread's own PC job keeps a small note beside the lease instead:

    python pc_note.py --lease E:/GitHubRunner/ResourceLeases/heavy.json --workspace E:/lifeshare-scratch/release-037 \
        --label "0.3.7 release build" --step "Packaging"
    python pc_note.py --lease E:/GitHubRunner/ResourceLeases/heavy.json --clear

Call it again at each step (the dashboard forgets a note that is not refreshed for 20 minutes) and clear it at the
end. The note never takes, changes or releases the lease. Standard library only; prints one JSON line.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def note_file(lease: str) -> Path:
    p = Path(lease)
    return p.with_name(p.stem + ".note.json")


def write(lease: str, label: str, step: str = "", workspace: str = "") -> dict:
    target = note_file(lease)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        old = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        old = {}
    same = isinstance(old, dict) and old.get("label") == label and (old.get("workspace") or "") == workspace
    note = {"label": label, "step": step, "workspace": workspace, "updated": now,
            "started": old.get("started") if same and old.get("started") else now, "pid": os.getppid()}
    tmp = target.with_name(target.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(note), encoding="utf-8")
    os.replace(tmp, target)
    return {"ok": True, "note": str(target), **note}


def clear(lease: str) -> dict:
    target = note_file(lease)
    try:
        target.unlink()
    except FileNotFoundError:
        pass
    return {"ok": True, "note": str(target), "cleared": True}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--lease", default=os.environ.get("FLETCHERY_HEAVY_LEASE", ""),
                        help="the heavy lease file (profile workers.heavyLeaseFile); or set FLETCHERY_HEAVY_LEASE")
    parser.add_argument("--label", default="", help='what the job is, e.g. "0.3.7 release build"')
    parser.add_argument("--step", default="", help='what it is doing now, e.g. "Packaging"')
    parser.add_argument("--workspace", default="", help="the folder the job works in (its checkout)")
    parser.add_argument("--clear", action="store_true", help="remove the note: the job is finished")
    args = parser.parse_args(argv)
    if not args.lease:
        print(json.dumps({"ok": False, "error": "pass --lease or set FLETCHERY_HEAVY_LEASE"}))
        return 1
    if args.clear:
        result = clear(args.lease)
    elif not args.label:
        print(json.dumps({"ok": False, "error": "pass --label (or --clear)"}))
        return 1
    else:
        result = write(args.lease, args.label, args.step, args.workspace)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
