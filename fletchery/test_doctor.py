"""`fletchery doctor` names every reason new work is not starting and groups recent failures with a hint."""
import unittest
from datetime import datetime, timezone

import doctor


class DoctorTests(unittest.TestCase):
    def test_reasons_and_failures(self):
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        status = {"project": "Harbor", "repo": "example/harbor", "max_parallel": 6, "held": True, "update_pending": True,
                  "live": [], "merge_auto": {"why": "no PRs are ready", "backlog": {"paused": True, "count": 32, "low": 6}},
                  "agents": {"list": [{"name": "opus", "enabled": True}, {"name": "codex", "enabled": False}]}}
        ledger = [{"at": now, "result": "requeue", "cost_usd": 4.5,
                   "detail": "push or PR failed: gh api PATCH repos/x/issues/1093: gh: Validation Failed (HTTP 422)"},
                  {"at": now, "result": "requeue", "cost_usd": 2.0,
                   "detail": "push or PR failed: gh api PATCH repos/x/issues/1094: gh: Validation Failed (HTTP 422)"},
                  {"at": now, "result": "blocked", "detail": ""}]
        text = doctor.report(status, ledger)
        self.assertIn("press Resume", text)
        self.assertIn("32 ready PRs are waiting to merge; new tasks start again at 6", text)
        self.assertIn("Agents on: opus", text)
        self.assertIn("2x push or PR failed", text)
        self.assertIn("#108", text)
        self.assertIn("blocked with no detail", text)
        self.assertIn("$6.50", text)

    def test_quiet_state(self):
        text = doctor.report({"project": "Harbor", "max_parallel": 4, "merge_auto": {}}, [])
        self.assertIn("Nothing is holding new tasks back", text)
        self.assertIn("no results recorded", text)


if __name__ == "__main__":
    unittest.main()
