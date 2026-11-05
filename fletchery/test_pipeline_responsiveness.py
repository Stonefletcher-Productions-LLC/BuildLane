"""Keep slow checks async, reserve fixer capacity early and bound durable retries."""
import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from agents import AgentPool, AgentSpec
from config import Config
from gates import GateResult
from github import FakeGitHub, PullRequest
from graph import Task
from pipeline import Context, Outcome, _fix_pr, _requeue_or_block, _run_task, _fail, _run_task_body
from repo import FakeRepo
from runtime import FakeRuntime


class PipelineResponsivenessTests(unittest.IsolatedAsyncioTestCase):
    def context(self, pool=None):
        temp = tempfile.TemporaryDirectory(prefix="fletchery-responsive-")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        tasks = {n: Task(n, f"Task {n}") for n in (1, 2)}
        return Context(Config(project_root=str(root)), FakeGitHub(tasks),
                       FakeRepo(root / "worktrees"), FakeRuntime(),
                       run_gates=lambda *_, **__: [], agents=pool, log=lambda _: None)

    async def check_responsive(self, command, target, method, block_call=1):
        entered, release = threading.Event(), threading.Event()
        original = getattr(target, method)
        count = 0

        def slow(*args, **kwargs):
            nonlocal count
            count += 1
            if count == block_call:
                entered.set()
                if not release.wait(2):
                    raise AssertionError("asyncio stalled during a blocking operation")
            return original(*args, **kwargs)

        async def progress():
            while not entered.is_set():
                await asyncio.sleep(0)
            release.set()

        with patch.object(target, method, side_effect=slow):
            result, _ = await asyncio.wait_for(asyncio.gather(command, progress()), 5)
        self.assertGreaterEqual(count, block_call)
        return result

    async def test_all_worker_gate_stages_leave_event_loop_free(self):
        for stage in ("initial", "gate_fix", "review_fix"):
            with self.subTest(stage=stage):
                ctx = self.context()
                calls = 0
                def gates(*_, **__):
                    nonlocal calls
                    calls += 1
                    return [GateResult("fixture", stage != "gate_fix" or calls > 1, "fixture")]
                ctx.run_gates = gates
                reviews = 0
                def review(_):
                    nonlocal reviews
                    reviews += 1
                    return {"verdict": "needs_work" if stage == "review_fix" and reviews == 1 else "approve", "findings": []}
                ctx.runtime = FakeRuntime({("reviewer", 1): review})
                command = _run_task(ctx.gh.tasks[1], ctx, Outcome(1, "requeue"))
                result = await self.check_responsive(command, ctx, "run_gates", 1 if stage == "initial" else 2)
                self.assertEqual(result.result, "pr")

    async def test_slow_commit_does_not_stall_other_tasks(self):
        ctx = self.context()
        with patch.object(ctx.repo, "dirty", return_value=True):
            command = _run_task(ctx.gh.tasks[1], ctx, Outcome(1, "requeue"))
            result = await self.check_responsive(command, ctx.repo, "commit_all")
        self.assertEqual(result.result, "pr")

    def pr(self, n):
        return PullRequest(100 + n, "Existing PR", f"existing-{n}", "Test-Staging", "a" * 40, ["ready"])

    async def test_fixer_gate_leaves_event_loop_free(self):
        ctx = self.context()
        command = _fix_pr(ctx.gh.tasks[1], self.pr(1), [], 1, ctx, Outcome(1, "gave_up"))
        result = await self.check_responsive(command, ctx, "run_gates")
        self.assertEqual(result.result, "fixed")

    def pool(self):
        return AgentPool([AgentSpec("first", kind="fake", max_parallel=1),
                          AgentSpec("second", kind="fake", max_parallel=1)],
                         {"mode": "lists", "medium": ["first", "second"]}, lambda _: FakeRuntime())

    async def test_fixer_capacity_is_reserved_during_setup(self):
        ctx = self.context(self.pool())
        ctx.task_agents = {"1": "first", "2": "first"}
        entered, release = threading.Event(), threading.Event()
        def slow_fetch():
            entered.set()
            if not release.wait(2):
                raise AssertionError("fixture fetch was not released")
        with patch.object(ctx.repo, "fetch_base", side_effect=slow_fetch):
            first = asyncio.create_task(_fix_pr(ctx.gh.tasks[1], self.pr(1), [], 1, ctx, Outcome(1, "gave_up")))
            while not entered.is_set():
                await asyncio.sleep(0)
            second = asyncio.create_task(_fix_pr(ctx.gh.tasks[2], self.pr(2), [], 1, ctx, Outcome(2, "gave_up")))
            try:
                for _ in range(1000):
                    if ctx.agents.busy.get("second") == 1:
                        break
                    await asyncio.sleep(0)
                self.assertEqual(ctx.agents.busy.get("first"), 1)
                self.assertEqual(ctx.agents.busy.get("second"), 1)
            finally:
                release.set()
            results = await asyncio.gather(first, second)
        self.assertEqual([r.agent for r in results], ["first", "second"])
        self.assertEqual(ctx.agents.busy, {"first": 0, "second": 0})

    def escalation_context(self):
        pool = AgentPool([AgentSpec("weak", kind="fake", tier=1),
                          AgentSpec("strong", kind="fake", tier=3, max_parallel=1)],
                         {"mode": "lists"}, lambda _: FakeRuntime())
        return self.context(pool)

    async def test_escalation_reserves_capacity_before_slow_setup_and_transfers_once(self):
        ctx = self.escalation_context()
        entered, release = threading.Event(), threading.Event()
        original = ctx.repo.restart_task_worktree

        def slow(*args):
            entered.set()
            if not release.wait(2):
                raise RuntimeError("fixture checkout timed out")
            return original(*args)

        with patch.object(ctx.repo, "restart_task_worktree", side_effect=slow) as setup:
            first = asyncio.create_task(_fail(ctx, ctx.gh.tasks[1], Outcome(1, "requeue"),
                                             Path("first-attempt"), "weak", "needs stronger agent"))
            try:
                while not entered.is_set():
                    await asyncio.sleep(0)
                self.assertEqual(ctx.agents.busy.get("strong"), 1)
                second = await _fail(ctx, ctx.gh.tasks[2], Outcome(2, "requeue"),
                                     Path("second-attempt"), "weak", "needs stronger agent")
                self.assertEqual(second.result, "blocked")
                self.assertEqual(setup.call_count, 1)
            finally:
                release.set()
            escalation = await first
        self.assertTrue(escalation.reserved)

        async def next_attempt(*args):
            self.assertEqual(ctx.agents.busy["strong"], 1, "handoff must not acquire the slot twice")
            return Outcome(1, "pr")

        with patch("pipeline._attempt", side_effect=next_attempt):
            await _run_task_body(ctx.gh.tasks[1], ctx, Outcome(1, "requeue"),
                                 escalation.agent, [escalation.note], escalation.reserved)
        self.assertEqual(ctx.agents.busy["strong"], 0)

    async def test_escalation_setup_failure_releases_reserved_capacity(self):
        ctx = self.escalation_context()
        with patch.object(ctx.repo, "restart_task_worktree", side_effect=RuntimeError("setup failed")):
            result = await _fail(ctx, ctx.gh.tasks[1], Outcome(1, "requeue"), Path("attempt"), "weak", "failed")
        self.assertEqual(result.result, "blocked")
        self.assertEqual(ctx.agents.busy["strong"], 0)

    async def test_cancelled_escalation_setup_releases_reserved_capacity(self):
        ctx = self.escalation_context()
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        def slow(*args):
            entered.set()
            try:
                if not release.wait(2):
                    raise RuntimeError("fixture checkout timed out")
                return Path("fresh")
            finally:
                finished.set()
        with patch.object(ctx.repo, "restart_task_worktree", side_effect=slow):
            job = asyncio.create_task(_fail(ctx, ctx.gh.tasks[1], Outcome(1, "requeue"), Path("attempt"), "weak", "failed"))
            try:
                while not entered.is_set():
                    await asyncio.sleep(0)
                job.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await job
                self.assertEqual(ctx.agents.busy["strong"], 0)
            finally:
                release.set()
                self.assertTrue(await asyncio.to_thread(finished.wait, 2))

    async def test_setup_failure_releases_fixer_capacity(self):
        ctx = self.context(self.pool())
        for stage in ("fetch_base", "repair_worktree", "ensure_repair_checkout"):
            with self.subTest(stage=stage), patch.object(ctx.repo, stage, side_effect=RuntimeError("setup failed")):
                with self.assertRaisesRegex(RuntimeError, "setup failed"):
                    await _fix_pr(ctx.gh.tasks[1], self.pr(1), [], 1, ctx, Outcome(1, "gave_up"))
                self.assertEqual(ctx.agents.busy.get("first"), 0)


class PersistentRetryTests(unittest.TestCase):
    class RealInterfaceGitHub:
        """Deliberately has no FakeGitHub-only .comments attribute."""
        def __init__(self, stored):
            self.stored = stored
        def issue_thread(self, number):
            return {"comments": [{"body": text} for text in self.stored]}
        def comment(self, number, body):
            self.stored.append(body)
        def set_status(self, number, status):
            self.status = status

    def context(self, stored):
        return Context(Config(), self.RealInterfaceGitHub(stored), None, FakeRuntime(), log=lambda _: None)

    def test_retry_limit_survives_new_production_client(self):
        stored = []
        task = Task(1, "Push the PR")
        first = _requeue_or_block(self.context(stored), task, Outcome(1, "requeue"), "push failed")
        self.assertEqual(first.result, "requeue")
        second = _requeue_or_block(self.context(stored), task, Outcome(1, "requeue"), "push failed again")
        self.assertEqual(second.result, "blocked")
        self.assertIn("second infrastructure failure", second.detail)

    def test_history_lookup_failure_does_not_allow_unbounded_retries(self):
        ctx = self.context([])
        with patch.object(ctx.gh, "issue_thread", side_effect=RuntimeError("GitHub unavailable")):
            result = _requeue_or_block(ctx, Task(1, "Push"), Outcome(1, "requeue"), "push failed")
        self.assertEqual(result.result, "blocked")
        self.assertIn("could not verify infrastructure retry history", result.detail)


if __name__ == "__main__":
    unittest.main()
