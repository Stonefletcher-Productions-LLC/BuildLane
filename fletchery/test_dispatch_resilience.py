"""Queue isolation, spending gates and nonblocking merge metadata. No real services."""
import asyncio
import tempfile
import threading
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from config import Config
from github import FakeGitHub, Feedback, PullRequest
from graph import Task, PR, label_for
from loop import Engine, today
from pipeline import Context
from repo import FakeRepo
from runtime import FakeRuntime


class DispatchResilienceTests(unittest.IsolatedAsyncioTestCase):
    def engine(self, tasks=None, **settings):
        folder = tempfile.TemporaryDirectory(prefix="fletchery-dispatch-resilience-")
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        cfg = Config(project_root=str(root), base_branch="Test-Staging", watch_failures=False,
                     auto_prerequisites=False, **settings)
        gh = FakeGitHub(tasks or {}, {90})
        runtime = FakeRuntime({("merger", 0): {"state": "nothing", "summary": "Fixture finished"}})
        ctx = Context(cfg, gh, FakeRepo(root / "worktrees"), runtime,
                      run_gates=lambda *_: [], log=lambda _: None)
        return Engine(ctx, root / "state"), gh, runtime

    async def test_shared_dependency_queried_once_across_fixers_and_workers(self):
        tasks = {1: Task(1, "Existing PR", "Depends on: #90", [label_for(PR)]),
                 2: Task(2, "Ready one", "Depends on: #90"),
                 3: Task(3, "Ready two", "Depends on: #90")}
        engine, gh, runtime = self.engine(tasks)
        gh.prs = [PullRequest(12, "Existing PR", tasks[1].branch, "Test-Staging", "a" * 40, [], "Closes #1")]
        with patch.object(gh, "issue_closed", wraps=gh.issue_closed) as lookup:
            await engine.once()
        self.assertEqual(lookup.call_count, 1)
        self.assertEqual({n for role, n, _ in runtime.calls if role == "worker"}, {2, 3})

    async def test_failed_dependency_lookup_holds_only_dependent_work_and_recovers(self):
        tasks = {1: Task(1, "Dependent", "Depends on: #90"), 2: Task(2, "Independent")}
        engine, gh, runtime = self.engine(tasks)
        with patch.object(gh, "issue_closed", side_effect=RuntimeError("GitHub unavailable")):
            await engine.once()
        self.assertEqual({n for role, n, _ in runtime.calls if role == "worker"}, {2})
        self.assertIn("prerequisite status unavailable", engine._waiting[1])
        self.assertNotIn("missing issue", engine._waiting[1])
        self.assertIn("#90", engine.dispatch_warning)
        runtime.calls.clear()
        await engine.once()
        self.assertIn(1, {n for role, n, _ in runtime.calls if role == "worker"})
        self.assertEqual(engine.dispatch_warning, "")

    async def test_zero_budget_starts_neither_fixer_nor_merger(self):
        task = Task(1, "Fix the PR", labels=[label_for(PR)])
        engine, gh, runtime = self.engine({1: task}, daily_budget_usd=0, merge_rounds=True)
        gh.prs = [PullRequest(12, "Existing PR", task.branch, "Test-Staging", "a" * 40, ["ready"], "Closes #1")]
        gh.feedback[12] = [Feedback(12, "comment", "Concrete fix", "2026-11-04T00:00:00Z")]
        await engine.once()
        self.assertEqual(runtime.calls, [])
        self.assertEqual(engine.state.fix_attempts, {})
        self.assertEqual(engine.merge_why, "daily budget spent")
        engine.cfg.daily_budget_usd = 10
        engine.state.spent[today()] = 10
        await engine.once()
        self.assertEqual(runtime.calls, [])

    async def test_first_pass_merge_cannot_omit_open_issue_prerequisite(self):
        task = Task(1, "Dependent feature", "Depends on: #2", [label_for(PR)])
        prerequisite = Task(2, "Unfinished prerequisite")
        engine, gh, runtime = self.engine({1: task, 2: prerequisite},
                                          merge_rounds=True, max_parallel=0)
        gh.prs = [PullRequest(12, "Feature", task.branch, "Test-Staging", "a" * 40,
                              ["ready"], "Closes #1")]
        await engine.once()
        self.assertFalse([call for call in runtime.calls if call[0] == "merger"])
        self.assertIsNone(engine.round_job)
        # Once that issue closes, the same PR can be merged on a fresh pass.
        gh.close_issue(2)
        await engine.once()
        self.assertTrue([call for call in runtime.calls if call[0] == "merger"])

    async def test_slow_merge_file_reads_leave_event_loop_free_and_fetch_once(self):
        engine, gh, runtime = self.engine(merge_rounds=True,
            merge_flags=[{"paths": r"Content/Town", "flag": "strict NavCheck"}])
        prs = [PullRequest(12, "Town", "town", "Test-Staging", "a" * 40, ["ready"]),
               PullRequest(13, "Docs", "docs", "Test-Staging", "b" * 40, ["ready"])]
        entered, release = threading.Event(), threading.Event()
        calls = Counter()

        def slow_files(number):
            calls[number] += 1
            entered.set()
            if not release.wait(2):
                raise RuntimeError("event loop stalled during metadata read")
            return ["Content/Town/map.uasset"] if number == 12 else ["Docs/Guide.md"]

        async def concurrent_worker():
            while not entered.is_set():
                await asyncio.sleep(0)
            # This coroutine must be able to run while GitHub is blocked.
            release.set()

        async def run():
            with patch("rounds.prepare_worktree", return_value=engine.ctx.repo.worktree_root), \
                 patch.object(gh, "pr_files", side_effect=slow_files):
                return await asyncio.gather(
                    engine._run_round(1, prs, "", "fixture", str(engine.state_dir / "round.json")),
                    concurrent_worker())
        engine.ctx.repo.worktree_root.mkdir(parents=True)
        result, _ = await asyncio.wait_for(run(), timeout=5)
        self.assertEqual(result["result"], "nothing", result)
        self.assertEqual(calls, {12: 1, 13: 1})
        prompt = next(prompt for role, _, prompt in runtime.calls if role == "merger")
        self.assertIn("strict NavCheck", prompt)
        self.assertIn("Content/Town/map.uasset", prompt)


if __name__ == "__main__":
    unittest.main()
