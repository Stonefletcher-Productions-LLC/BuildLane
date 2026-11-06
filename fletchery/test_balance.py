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


if __name__ == "__main__":
    unittest.main()
