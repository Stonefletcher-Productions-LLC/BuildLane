"""Finished rounds are history, not the next merge's current status."""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import merge


class MergeStatusTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="fletchery-merge-status-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.progress = self.root / "round.json"
        self.lease = self.root / "heavy.json"
        self.now = datetime(2026, 11, 5, 8, tzinfo=timezone.utc)

    def save(self, **changes):
        data = {"round": "19", "state": "failed", "started": "2026-11-05T00:00:00Z",
                "updated": "2026-11-05T01:00:00Z", "prs": [100, 101], "worktree": str(self.root),
                "branch": "round-19", "steps": [{"name": "Build", "state": "failed"}],
                "note": "Agent stopped; candidate preserved"}
        data.update(changes)
        self.progress.write_text(json.dumps(data), encoding="utf-8")

    def test_every_terminal_round_is_detached_from_current_status(self):
        for state in merge.TERMINAL_STATES:
            with self.subTest(state=state):
                self.save(state=state)
                with patch("merge.in_branch") as query:
                    view = merge.view(self.progress, self.lease, {100: "a" * 40}, self.now)
                self.assertFalse(view["active"])
                self.assertEqual(view["state"], "idle")
                self.assertEqual((view["round"], view["prs"], view["steps"], view["in_branch"]), ("", [], [], {}))
                self.assertFalse(view["stale"])
                self.assertNotIn("FAILED", view["headline"])
                self.assertEqual(view["previous"]["round"], "19")
                self.assertEqual(view["previous"]["duration_seconds"], 3600)
                query.assert_not_called()

    def test_finished_duration_is_frozen_and_explicit_end_beats_later_metadata(self):
        self.save(ended="2026-11-05T00:30:00Z", updated="2026-11-06T01:00:00Z")
        first = merge.view(self.progress, self.lease, now=self.now)
        later = merge.view(self.progress, self.lease, now=datetime(2026, 11, 9, tzinfo=timezone.utc))
        self.assertEqual(first["previous"], later["previous"])
        self.assertEqual(first["previous"]["duration_seconds"], 1800)
        self.assertEqual(first["previous"]["ended"], "2026-11-05T00:30:00Z")

    def test_historical_failure_does_not_hide_current_reservation(self):
        self.save()
        self.lease.write_text(json.dumps({"token": "held", "purpose": "Hair review", "pid": 123,
                                         "workspaces": str(self.root), "acquiredUtc": "2026-11-05T07:50:00Z"}))
        view = merge.view(self.progress, self.lease, now=self.now)
        self.assertIn("Hair review", view["headline"])
        self.assertIn("No merge round running", view["headline"])
        self.assertEqual(view["previous"]["note"], "Agent stopped; candidate preserved")

    def test_failed_step_in_a_live_round_remains_current(self):
        self.save(state="running", updated="2026-11-05T07:59:00Z")
        with patch("merge.in_branch", return_value=True):
            view = merge.view(self.progress, self.lease, {100: "a" * 40}, self.now)
        self.assertTrue(view["active"])
        self.assertEqual(view["state"], "failed")
        self.assertIsNone(view["previous"])
        self.assertEqual(view["prs"], [100, 101])
        self.assertIn("FAILED at Build", view["headline"])

    def test_invalid_historical_timestamps_do_not_invent_a_duration(self):
        for started, ended in [("bad", "also bad"), ("2026-11-05T02:00:00Z", "2026-11-05T01:00:00Z")]:
            with self.subTest(started=started):
                self.save(started=started, ended=ended)
                view = merge.view(self.progress, self.lease, now=self.now)
                self.assertIsNone(view["previous"]["duration_seconds"])

    def test_non_object_progress_is_ignored(self):
        self.progress.write_text("[]")
        self.assertFalse(merge.view(self.progress, self.lease)["active"])


if __name__ == "__main__":
    unittest.main()
