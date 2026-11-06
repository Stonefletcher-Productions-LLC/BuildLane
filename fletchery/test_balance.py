"""Workers and merges stay balanced: a running round caps only its own account, and a full merge backlog pauses
new feature tasks (never fixes) until it drains to the low mark."""
import asyncio
import unittest

import test_dispatch_resilience as fixtures
from agents import AgentPool, AgentSpec
from github import PullRequest
from graph import Task
from loop import Engine


class BalanceTests(unittest.IsolatedAsyncioTestCase):
    engine = fixtures.DispatchResilienceTests.engine

    def with_pool(self, engine, rt, specs, routing=None):
        pool = AgentPool(specs, routing or {"mode": "lists"}, lambda _: rt)
        engine.ctx.agents = pool
        engine = Engine(engine.ctx, engine.state_dir)
        engine.check_accounts = lambda force=False: None
        return engine, pool

    def running_round(self, engine, pool, agent):
        job = asyncio.get_running_loop().create_future()
        engine.round_job, engine.round_info = job, {"agent": agent, "round": 1}
        pool.acquire(agent)
        self.addCleanup(lambda: job.done() or job.set_result({}))
        return job

    def ready_pr(self, number, head="a" * 40):
        return PullRequest(number, f"Ready {number}", f"branch-{number}", "Test-Staging", head, ["foreman", "ready"])

    async def test_round_lets_other_accounts_write(self):
        tasks = {1: Task(1, "Docs task", labels=["difficulty:easy"])}
        engine, gh, rt = self.engine(tasks, merge_rounds=True)
        engine, pool = self.with_pool(engine, rt, [AgentSpec("native", kind="fake", tier=3),
                                                   AgentSpec("writer", kind="fake", tier=1, roles=["worker", "reviewer"])],
                                      {"mode": "lists", "easy": ["writer"]})
        round_job = self.running_round(engine, pool, "native")
        engine.dispatch()
        self.assertTrue(engine.merge_priority)
        self.assertEqual(len(engine.jobs), 1, "the writer is on another account and must not wait for the round")
        self.assertFalse(round_job.done())
        await engine._wait_for_jobs(None)
        self.assertIn("worker", [role for role, _, _ in rt.calls])

    async def test_round_account_starts_at_most_the_shared_sessions(self):
        tasks = {n: Task(n, f"Task {n}") for n in (1, 2, 3)}
        engine, gh, rt = self.engine(tasks, merge_rounds=True, round_shared_sessions=1)
        engine, pool = self.with_pool(engine, rt, [AgentSpec("opus", kind="claude", tier=4),
                                                   AgentSpec("sonnet", kind="claude", tier=3)])
        self.running_round(engine, pool, "opus")
        engine.dispatch()
        self.assertEqual(pool.round_account, ("claude", 1))
        while engine.jobs:
            await engine._wait_for_jobs(None)
        workers = [task for role, task, _ in rt.calls if role == "worker"]
        self.assertEqual(len(workers), 1, "one new session on the round's own plan; the others go back to the queue")

    async def test_minus_one_keeps_the_old_freeze(self):
        tasks = {1: Task(1, "Docs task", labels=["difficulty:easy"])}
        engine, gh, rt = self.engine(tasks, merge_rounds=True, round_shared_sessions=-1)
        engine, pool = self.with_pool(engine, rt, [AgentSpec("native", kind="fake", tier=3),
                                                   AgentSpec("writer", kind="fake", tier=1, roles=["worker", "reviewer"])],
                                      {"mode": "lists", "easy": ["writer"]})
        self.running_round(engine, pool, "native")
        engine.dispatch()
        self.assertFalse(engine.jobs)
        self.assertIn("merge priority", engine._waiting[1])

    async def test_round_reservation_ends_with_the_round(self):
        engine, gh, rt = self.engine({1: Task(1, "Task")}, merge_rounds=False)
        engine, pool = self.with_pool(engine, rt, [AgentSpec("opus", kind="claude", tier=4)])
        job = self.running_round(engine, pool, "opus")
        engine.dispatch()
        self.assertIsNotNone(pool.round_account)
        job.set_result({})
        engine.round_job, engine.round_info = None, {}
        pool.release("opus")
        await engine._wait_for_jobs(None)
        engine.dispatch()
        self.assertIsNone(pool.round_account)

    async def test_full_backlog_pauses_new_tasks_until_the_low_mark(self):
        engine, gh, rt = self.engine({1: Task(1, "New feature")}, merge_rounds=True,
                                     merge_backlog_high=3, merge_backlog_low=1)
        engine.state.merge_held = True  # no round starts; only the backlog decides
        gh.prs = [self.ready_pr(n) for n in (11, 12, 13)]
        engine.dispatch()
        self.assertTrue(engine.backlog_paused)
        self.assertFalse(engine.jobs)
        self.assertIn("merge backlog full: 3 PRs", engine._waiting[1])
        gh.prs = gh.prs[:2]  # one merged: still above the low mark, still paused
        engine.dispatch()
        self.assertTrue(engine.backlog_paused)
        self.assertFalse(engine.jobs)
        gh.prs = gh.prs[:1]  # drained to the low mark: new work starts
        engine.dispatch()
        self.assertFalse(engine.backlog_paused)
        self.assertEqual(len(engine.jobs), 1)
        await engine._wait_for_jobs(None)

    async def test_failed_heads_do_not_count_toward_the_backlog(self):
        engine, gh, rt = self.engine({1: Task(1, "New feature")}, merge_rounds=True, merge_backlog_high=2)
        engine.state.merge_held = True
        gh.prs = [self.ready_pr(11), self.ready_pr(12, head="b" * 40)]
        engine.state.merge_excluded = {"12": {"head": "b" * 40, "why": "specs failed"}}
        engine.dispatch()
        self.assertEqual(engine.merge_backlog, 1)
        self.assertFalse(engine.backlog_paused)
        self.assertEqual(len(engine.jobs), 1)
        await engine._wait_for_jobs(None)

    async def test_backlog_is_ignored_when_rounds_are_off(self):
        engine, gh, rt = self.engine({1: Task(1, "New feature")}, merge_rounds=False, merge_backlog_high=1)
        gh.prs = [self.ready_pr(11), self.ready_pr(12)]
        engine.dispatch()
        self.assertFalse(engine.backlog_paused)
        self.assertEqual(len(engine.jobs), 1)
        await engine._wait_for_jobs(None)

    def test_default_marks_follow_round_size(self):
        cfg = fixtures.Config(merge_round_max=6)
        engine = Engine.__new__(Engine)
        engine.cfg = cfg
        self.assertEqual(engine._backlog_marks(), (12, 6))
        cfg.merge_backlog_high, cfg.merge_backlog_low = 4, 9
        self.assertEqual(engine._backlog_marks(), (4, 4))

    # ---- rounds: value order and parking ----

    def test_rounds_take_the_most_unblocking_pr_first(self):
        import rounds
        tasks = {1: Task(1, "Old leaf", labels=["foreman:pr"]), 2: Task(2, "Pets base", labels=["foreman:pr"]),
                 3: Task(3, "Tricks", "Depends on: #2"), 4: Task(4, "More tricks", "Depends on: #3")}
        tasks[1].pr_number, tasks[2].pr_number = 11, 22
        ready = [self.ready_pr(11), self.ready_pr(22)]
        self.assertEqual([p.number for p in rounds.by_value(ready, tasks)], [22, 11])
        self.assertEqual([p.number for p in rounds.by_value(ready, {})], [11, 22], "ties stay oldest first")

    def test_twice_deferred_head_is_parked_until_it_changes(self):
        import rounds
        parked = {}
        pr = self.ready_pr(20)
        cant = [{"pr": 20, "why": "Two-PC Steam acceptance cannot run on a single PC.", "disposition": "deferred"}]
        self.assertEqual(rounds.record_deferrals(parked, [pr], cant, 25, "2026-11-06T10:00:00+00:00"), [])
        self.assertEqual(rounds.not_parked([pr], parked), [pr], "one deferral is not enough")
        self.assertEqual(rounds.record_deferrals(parked, [pr], cant, 26, "2026-11-06T12:00:00+00:00"), [20])
        from datetime import datetime
        soon = datetime.fromisoformat("2026-11-06T13:00:00+00:00")
        self.assertEqual(rounds.not_parked([pr], parked, soon), [])
        later = datetime.fromisoformat("2026-11-09T13:00:00+00:00")
        self.assertEqual(rounds.not_parked([pr], dict(parked), later), [pr], "parking expires")
        moved = self.ready_pr(20, head="c" * 40)
        self.assertEqual(rounds.not_parked([moved], parked, soon), [moved], "a new head unparks")
        self.assertNotIn("20", parked)

    def test_unattempted_deferrals_never_park(self):
        import rounds
        parked = {}
        pr = self.ready_pr(34)
        for n, why in ((25, "Not attempted; head unchanged since round 24."), (26, "Round interrupted before its check")):
            rounds.record_deferrals(parked, [pr], [{"pr": 34, "why": why, "disposition": "deferred"}], n, "2026-11-06T10:00:00+00:00")
        self.assertEqual(rounds.not_parked([pr], parked), [pr])

    async def test_parked_pr_stays_out_of_the_next_round(self):
        engine, gh, rt = self.engine({}, merge_rounds=True)
        gh.prs = [self.ready_pr(11), self.ready_pr(12)]
        engine.state.merge_parked = {"11": {"head": "a" * 40, "count": 2, "parked": "2999-01-01T00:00:00+00:00"}}
        await engine.once()
        import rounds
        progress = rounds.read_file(str(engine.state_dir / "merge-round.json"))
        self.assertEqual(progress["prs"], [12])


if __name__ == "__main__":
    unittest.main()
