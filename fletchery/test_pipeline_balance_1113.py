"""2026-11-13: a crash already on Test-Staging parked healthy PRs, nobody heard about rounds landing nothing, a stale
git lock failed every job on one node for hours, and a busy node looked paused. Each fix is pinned here."""
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rounds  # noqa: E402
import node_worker  # noqa: E402


def pr(n, head="a" * 40, labels=()):
    return SimpleNamespace(number=n, head_sha=head, labels=list(labels))


class BaselineDeferrals(unittest.TestCase):
    def test_base_red_round_never_parks(self):
        parked = {}
        p = pr(1870)
        d = [{"pr": 1870, "why": "Dish.CarryRules crashes the editor", "disposition": "deferred"}]
        for n in (160, 162):
            rounds.record_deferrals(parked, [p], d, n, "2026-11-13T15:00:00+00:00", base_red=True)
        self.assertEqual(parked, {})

    def test_base_red_reason_never_parks(self):
        parked = {}
        p = pr(1703)
        for n, why in ((160, "Base is red: RunSpecs crashes in Dish.CarryRules on Test-Staging too"),
                       (162, "The crash is pre-existing failure on the baseline, not this PR")):
            rounds.record_deferrals(parked, [p], [{"pr": 1703, "why": why}], n, "2026-11-13T15:00:00+00:00")
        self.assertEqual(rounds.not_parked([p], parked), [p])

    def test_baseline_run_wording_still_parks(self):
        # "baseline run" is a people-only acceptance (a before/after run), not a red base
        parked = {}
        p = pr(5)
        why = "Its named acceptance needs a baseline run and a candidate run of 60 minutes"
        rounds.record_deferrals(parked, [p], [{"pr": 5, "why": why}], 1, "2026-11-13T10:00:00+00:00")
        self.assertEqual(rounds.record_deferrals(parked, [p], [{"pr": 5, "why": why}], 2, "2026-11-13T11:00:00+00:00"), [5])

    def test_base_is_red(self):
        self.assertTrue(rounds.base_is_red({"stop_reason": "Base is red: RunSpecs crashes on Test-Staging"}))
        self.assertFalse(rounds.base_is_red({"stop_reason": "RunSpecs failed in #1703"}))


class BaseRedGate(unittest.TestCase):
    def test_only_merge_next_while_red(self):
        now = datetime(2026, 11, 13, 16, tzinfo=timezone.utc)
        red = {"round": 162, "why": "Dish crash", "at": "2026-11-13T15:00:00+00:00"}
        ready = [pr(1870), pr(1887, labels=["merge-next"])]
        gate, why = rounds.base_red_gate(red, ready, "Test-Staging", now)
        self.assertEqual([p.number for p in gate], [1887])
        self.assertIn("Test-Staging itself failed in round 162", why)

    def test_hold_expires(self):
        now = datetime(2026, 11, 13, 22, tzinfo=timezone.utc)
        red = {"round": 162, "why": "x", "at": "2026-11-13T15:00:00+00:00"}
        self.assertEqual(rounds.base_red_gate(red, [pr(1)], "Test-Staging", now), ([], ""))


class Alerts(unittest.TestCase):
    def test_zero_landed(self):
        res = {"result": "failed", "merged": [], "stop_reason": "job 346 crashed"}
        self.assertIn("landed none of 2", rounds.round_alert(res, [pr(1), pr(2)], "Test-Staging"))

    def test_base_red_and_repeat(self):
        self.assertIn("itself fails", rounds.round_alert({"result": "failed", "stop_reason": "Base is red: x"}, [pr(1)], "B"))
        self.assertIn("same failure", rounds.round_alert({"result": "failed", "failure_repeated": True}, [pr(1)], "B"))

    def test_quiet_when_landed_or_interrupted(self):
        self.assertEqual(rounds.round_alert({"result": "landed", "merged": [1]}, [pr(1), pr(2)], "B"), "")
        self.assertEqual(rounds.round_alert({"result": "interrupted"}, [pr(1), pr(2)], "B"), "")

    def test_raise_alert_comments_one_issue(self):
        made, said = [], []

        class GH:
            def marked_issues(self, marker):
                return [{"number": 99}] if made else []

            def create_issue(self, title, body, labels):
                made.append((title, body))
                return 99

            def comment(self, number, text):
                said.append((number, text))
        with tempfile.TemporaryDirectory() as d:
            gh = GH()
            self.assertEqual(rounds.raise_alert(gh, d, {"round": 162, "result": "failed"}, "landed none"), 99)
            self.assertEqual(rounds.raise_alert(gh, d, {"round": 163, "result": "nothing"}, "landed none"), 99)
            lines = Path(d, "alerts.jsonl").read_text().splitlines()
        self.assertEqual(len(made), 1)
        self.assertIn(rounds.ALERT_MARKER, made[0][1])
        self.assertEqual(said[0][0], 99)
        self.assertEqual([json.loads(x)["round"] for x in lines], [162, 163])


class StaleLocks(unittest.TestCase):
    def test_old_locks_go_young_stay(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            (ws / ".git" / "refs" / "heads").mkdir(parents=True)
            old = ws / ".git" / "index.lock"
            ref = ws / ".git" / "refs" / "heads" / "main.lock"
            young = ws / ".git" / "HEAD.lock"
            for f in (old, ref, young):
                f.write_text("")
            t = time.time()
            os.utime(old, (t - 3600, t - 3600))
            os.utime(ref, (t - 3600, t - 3600))
            gone = node_worker.clear_stale_git_locks(ws, now=t)
            self.assertFalse(old.exists())
            self.assertFalse(ref.exists())
            self.assertTrue(young.exists())
            self.assertEqual(len(gone), 2)

    def test_backoff(self):
        self.assertEqual([node_worker.checkout_backoff(n) for n in (0, 1, 2, 3, 4, 9)], [0, 0, 300, 900, 1800, 1800])


class BusyNodeIsNotPaused(unittest.TestCase):
    def test_held_off(self):
        import machines
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.execute("CREATE TABLE jobs (id INTEGER, machine TEXT, state TEXT)")
        node = {"name": "SF-NODE-01"}
        lease = {"paused": True, "lease": "LaneGuard RunSpecs"}
        self.assertTrue(machines.Machines._held_off(db, node, lease), "idle and the lease is someone else's: paused")
        db.execute("INSERT INTO jobs VALUES (357, 'SF-NODE-01', 'running')")
        self.assertFalse(machines.Machines._held_off(db, node, lease), "its own job holds the lease: busy, not paused")
        self.assertTrue(machines.Machines._held_off(db, node, {**lease, "pause_file": True}))
        self.assertTrue(machines.Machines._held_off(db, node, {**lease, "checkout_failing": 3}))
        self.assertFalse(machines.Machines._held_off(db, node, {"paused": False}))


if __name__ == "__main__":
    unittest.main()
