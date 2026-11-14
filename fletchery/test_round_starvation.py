"""Rounds must not go quiet while `ready` PRs wait. No real services.

LifeShare 2026-11-14: after round 179 the backend, both machines and the workers were healthy, yet no round started for
hours. 23 ready PRs were left out at their heads (15 of them only for conflicts with the base), 3 were parked, and the
one free PR, #1568, carried "Depends on: stonefletcher-productions-llc/hearth#42", a Hearth issue closed days before.
The eligibility filter verified that issue as closed, but the batch step filtered again without the verified set and
dropped #1568. Nothing logged it, nothing alerted, and the restart logic had nothing to restart."""
import unittest
from datetime import datetime, timedelta, timezone

import test_dispatch_resilience as fixtures
import watchdog
from github import PullRequest
from graph import label_for, BLOCKED

HEARTH = ("stonefletcher-productions-llc/hearth", 42)


def ago(minutes):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat(timespec="seconds")


class CrossRepoPrerequisiteTests(unittest.IsolatedAsyncioTestCase):
    engine = fixtures.DispatchResilienceTests.engine

    async def test_closed_cross_repo_prerequisite_lets_the_round_start(self):
        engine, gh, rt = self.engine(merge_rounds=True, max_parallel=0)
        gh.repository_closed.add(HEARTH)
        gh.prs = [PullRequest(1568, "Pavement", "pavement", "Test-Staging", "a" * 40, ["ready"],
                              "Depends on: stonefletcher-productions-llc/hearth#42")]
        engine.dispatch()
        self.assertIsNotNone(engine.round_job, engine.merge_why)
        await engine.round_job

    async def test_open_cross_repo_prerequisite_still_holds_the_pr(self):
        engine, gh, rt = self.engine(merge_rounds=True, max_parallel=0)
        gh.prs = [PullRequest(1568, "Pavement", "pavement", "Test-Staging", "a" * 40, ["ready"],
                              "Depends on: Stonefletcher-Productions-LLC/Hearth#42")]
        engine.dispatch()
        self.assertIsNone(engine.round_job)


class StarvationTests(unittest.IsolatedAsyncioTestCase):
    engine = fixtures.DispatchResilienceTests.engine

    def setup(self, conflicts=True):
        engine, gh, rt = self.engine(merge_rounds=True, max_parallel=0)
        pr = PullRequest(12, "Feature", "feature", "Test-Staging", "a" * 40, ["ready"])
        gh.prs = [pr]
        engine.state.merge_excluded["12"] = {"head": pr.head_sha, "round": 0, "at": ago(200), "conflict": conflicts,
                                             "disposition": "failed", "why": "conflicts with Test-Staging",
                                             "repair_task": 7}
        return engine, gh, rt, pr

    def test_starvation_is_noticed_with_each_reason(self):
        engine, gh, rt, pr = self.setup()
        gh.conflicts = {12}
        engine.dispatch()
        self.assertIsNone(engine.round_job)
        starved = engine.state.merge_starved
        self.assertTrue(starved.get("since"))
        self.assertIn("conflicts", starved["blocked"]["12"])
        self.assertIn("none can join a round", engine.merge_why)

    async def test_conflict_that_cleared_on_the_base_is_retried(self):
        engine, gh, rt, pr = self.setup()
        gh.conflicts = set()  # the base moved; GitHub now merges it cleanly
        engine.dispatch()     # notices, rechecks, drops the stale exclusion
        self.assertNotIn("12", engine.state.merge_excluded)
        engine.dispatch()
        self.assertIsNotNone(engine.round_job, engine.merge_why)
        await engine.round_job
        engine.round_job = None
        engine.dispatch()
        self.assertEqual(engine.state.merge_starved, {})

    def test_still_conflicting_pr_is_rechecked_at_most_every_half_hour(self):
        engine, gh, rt, pr = self.setup()
        gh.conflicts = {12}
        calls = []
        real = gh.pr_conflicts
        gh.pr_conflicts = lambda n: calls.append(n) or real(n)
        engine.dispatch()
        engine.dispatch()
        self.assertEqual(calls, [12])
        self.assertIn("12", engine.state.merge_excluded)

    def test_failed_head_gets_one_retry_after_a_long_starve(self):
        engine, gh, rt, pr = self.setup(conflicts=False)
        engine.dispatch()
        self.assertIn("12", engine.state.merge_excluded)  # a fresh starve does not retry failures
        engine.state.merge_starved["since"] = ago(watchdog.RETRY_FAILED_AFTER_MINUTES + 1)
        engine.dispatch()
        self.assertNotIn("12", engine.state.merge_excluded)
        self.assertEqual(engine.state.merge_starved["retried"]["12"], pr.head_sha)
        # Failed again at the same head: never a second automatic retry.
        engine.state.merge_excluded["12"] = {"head": pr.head_sha, "round": 181, "at": ago(1), "why": "failed again"}
        engine.state.merge_starved["since"] = ago(watchdog.RETRY_FAILED_AFTER_MINUTES * 3)
        engine.dispatch()
        self.assertIn("12", engine.state.merge_excluded)

    def test_alert_once_when_it_cannot_recover_and_say_when_rounds_resume(self):
        engine, gh, rt, pr = self.setup()
        gh.conflicts = {12}
        engine.dispatch()
        self.assertFalse(engine.state.merge_starved.get("alerted"))  # not yet: recovery gets its chance first
        engine.state.merge_starved["since"] = ago(watchdog.ALERT_AFTER_MINUTES + 1)
        engine.dispatch()
        issue = engine.state.merge_starved.get("issue")
        self.assertTrue(issue)
        alerts = [t for t in gh.tasks.values() if t.title == "Fletchery round alerts"]
        self.assertEqual(len(alerts), 1)
        self.assertNotIn(label_for(BLOCKED), alerts[0].labels)  # no task label: no planner picks the alert up
        self.assertIn("#12", alerts[0].body)
        self.assertIn("conflicts", alerts[0].body)
        engine.dispatch()
        self.assertEqual(gh.comments.get(issue, []), [])  # one alert per starve, not one per pass
        # A new head clears the exclusion and a round starts: the alert issue hears that too.
        gh.prs = [PullRequest(12, "Feature", "feature", "Test-Staging", "b" * 40, ["ready"])]
        gh.conflicts = set()
        engine.dispatch()
        self.assertIsNotNone(engine.round_job, engine.merge_why)
        self.assertIn("can take PRs again", gh.comments[issue][-1])
        self.assertEqual(engine.state.merge_starved, {})

    def test_the_alert_issue_is_reused_although_it_carries_no_label(self):
        import rounds, tempfile
        from pathlib import Path
        engine, gh, rt, pr = self.setup()
        gh.marked_issues = lambda marker: []  # GitHub's search filters on the Fletchery label, which this issue lacks
        with tempfile.TemporaryDirectory() as d:
            first = rounds.alert_comment(gh, Path(d), "one")
            second = rounds.alert_comment(gh, Path(d), "two")
        self.assertEqual(first, second)
        self.assertEqual(gh.comments[first], ["two"])

    def test_paused_merging_is_not_starvation(self):
        engine, gh, rt, pr = self.setup()
        engine.state.merge_held = True
        engine.dispatch()
        self.assertEqual(engine.state.merge_starved, {})


if __name__ == "__main__":
    unittest.main()


class BackendWatchdogTests(unittest.IsolatedAsyncioTestCase):
    def test_hang_guard_fires_only_when_the_loop_stops_beating(self):
        t = [0.0]
        fired = []
        guard = watchdog.HangGuard(30, fired.append, clock=lambda: t[0])
        t[0] = 29 * 60
        self.assertFalse(guard.hung())
        guard.beat()
        t[0] = 58 * 60
        self.assertFalse(guard.hung())
        t[0] = 29 * 60 + 31 * 60
        self.assertTrue(guard.hung())
        self.assertFalse(watchdog.HangGuard(0, fired.append, clock=lambda: 10 ** 9).hung())  # 0 = never

    def test_hang_guard_thread_calls_back(self):
        import threading
        t = [0.0]
        done = threading.Event()
        guard = watchdog.HangGuard(1, lambda minutes: done.set(), clock=lambda: t[0])
        t[0] = 120
        guard.start(every=0.01)
        self.assertTrue(done.wait(2))

    async def test_a_reload_stops_holding_rounds_back_after_the_drain_limit(self):
        import asyncio
        import test_reload_dispatch
        engine, logs = test_reload_dispatch.ReloadDispatchTests.engine(self)
        engine.cfg.drain_max_minutes = 0.0005  # 30 ms
        engine.cfg.hang_minutes = 0
        long_job = asyncio.get_running_loop().create_future()
        engine.jobs[long_job] = (5, None, None)
        passes = []

        def dispatch(*, draining):
            passes.append(draining)
            if passes.count(False) >= 3 and not long_job.done():
                long_job.set_result(None)

        def settle(done):
            for job in done:
                engine.jobs.pop(job)
            return []

        engine.dispatch, engine._settle, engine.publish = dispatch, settle, lambda _: None
        await asyncio.wait_for(engine.run_forever(code_changed=lambda: True), timeout=5)
        self.assertTrue(passes[0], "the reload drains first")
        self.assertIn(False, passes, "past the limit, passes run undrained so rounds can start")
        self.assertTrue(any("rounds and workers continue" in line for line in logs))
        self.assertFalse(long_job.cancelled(), "the running session is never cancelled")
        self.assertFalse(engine.jobs)
