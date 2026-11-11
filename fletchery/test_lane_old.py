"""A finished Build Lane status from days ago must not show its old queue as PRs waiting for a build."""
import json
import tempfile
from datetime import datetime, timedelta, timezone
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lane  # noqa: E402


def _status(tmp: Path, updated: datetime, done: str) -> str:
    p = tmp / "status.json"
    p.write_text(json.dumps({"what": "batch", "done": done, "started": (updated - timedelta(hours=1)).isoformat(),
                             "updated": updated.isoformat(), "queue": [{"number": 607, "title": "t", "state": "waiting"}]}),
                 encoding="utf-8")
    return str(p)


def main() -> None:
    now = datetime(2026, 11, 11, 18, 0, tzinfo=timezone.utc)
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        old = lane.read(_status(tmp, now - timedelta(days=8), "success"), "", now=now)
        assert old["old"] and old["queue"] == [] and old["old_queue"] == 1, old
        fresh = lane.read(_status(tmp, now - timedelta(hours=2), "success"), "", now=now)
        assert not fresh["old"] and len(fresh["queue"]) == 1, fresh
        running = lane.read(_status(tmp, now - timedelta(days=3), ""), "", now=now)
        assert not running["old"] and len(running["queue"]) == 1 and running["stale"], running
    print("test_lane_old: ok")


if __name__ == "__main__":
    main()
