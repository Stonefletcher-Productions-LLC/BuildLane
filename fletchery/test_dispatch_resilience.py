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
from loop import Engine, State, today
from pipeline import Context, Outcome
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

    async def test_external_ready_pr_dependency_is_verified_even_without_harness_label(self):
        for answer in (True, False, RuntimeError("GitHub down")):
            with self.subTest(answer=answer):
                engine, gh, runtime = self.engine(merge_rounds=True, max_parallel=0)
                external = PullRequest(12, "External feature", "external", "Test-Staging", "a" * 40,
                                       ["ready"], "Depends on: #90")
                independent = PullRequest(13, "Independent", "independent", "Test-Staging", "b" * 40, ["ready"])
                options = {"side_effect": answer} if isinstance(answer, Exception) else {"return_value": answer}
                with patch.object(gh, "open_prs", return_value=[]), \
                     patch.object(gh, "labelled_prs", return_value=[external, independent]), \
                     patch.object(gh, "issue_closed", **options) as lookup:
                    await engine.once()
                lookup.assert_called_once_with(90)
                progress = __import__("rounds").read_file(str(engine.state_dir / "merge-round.json"))
                self.assertEqual(progress["prs"], [12, 13] if answer is True else [13])

    async def test_feedback_arriving_during_repair_is_not_marked_consumed(self):
        task = Task(1, "Fix existing feature", labels=[label_for(PR)])
        engine, gh, _ = self.engine({1: task})
        pr = PullRequest(12, "Feature", task.branch, "Test-Staging", "a" * 40, ["ready"], "Closes #1")
        gh.prs = [pr]
        first = "2026-11-04T00:00:00Z"
        later = "2026-11-04T00:01:00Z"
        gh.feedback[12] = [Feedback(12, "comment", "Initial correction", first)]
        entered, release = asyncio.Event(), asyncio.Event()
        seen = []

        async def fixer(task, pr, feedback, attempt, ctx):
            seen.append([f.text for f in feedback])
            if len(seen) == 1:
                entered.set()
                await release.wait()
            return Outcome(1, "fixed", pr_number=12)

        with patch("loop.fix_pr", side_effect=fixer):
            engine.dispatch()
            await asyncio.wait_for(entered.wait(), 2)
            gh.feedback[12].append(Feedback(12, "comment", "New correction during repair", later))
            release.set()
            await engine._wait_for_jobs(None)
            self.assertEqual(engine.state.seen["12"], first)
            await engine.once()
        self.assertEqual(seen, [["Initial correction"], ["New correction during repair"]])

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

    async def test_once_finishes_its_final_status_write_before_exit(self):
        engine, gh, _ = self.engine()
        entered, release = threading.Event(), threading.Event()
        original = gh.write_status

        def slow_status(text):
            entered.set()
            if not release.wait(2):
                raise RuntimeError("fixture status write timed out")
            original(text)

        with patch.object(gh, "write_status", side_effect=slow_status):
            command = asyncio.create_task(engine.once())
            try:
                while not entered.is_set():
                    await asyncio.sleep(0)
                self.assertFalse(command.done(), "once must not exit with its status write still pending")
            finally:
                release.set()
            await asyncio.wait_for(command, timeout=3)
        self.assertEqual(gh.status_text, (engine.state_dir / "status.md").read_text(encoding="utf-8"))

    async def test_background_status_coalesces_to_latest_snapshot(self):
        engine, gh, _ = self.engine()
        engine.dispatch()
        entered, release = threading.Event(), threading.Event()
        writes = []

        def slow_status(text):
            writes.append(text)
            if len(writes) == 1:
                entered.set()
                if not release.wait(2):
                    raise RuntimeError("fixture writer timed out")
            gh.status_text = text

        with patch.object(gh, "write_status", side_effect=slow_status):
            engine.publish([])
            try:
                while not entered.is_set():
                    await asyncio.sleep(0)
                engine.state.held = True
                engine.publish([])
                newest = (engine.state_dir / "status.md").read_text(encoding="utf-8")
                self.assertNotEqual(writes[0], newest)
            finally:
                release.set()
            await asyncio.wait_for(engine._status_job, timeout=3)
        self.assertEqual(len(writes), 2)
        self.assertEqual(gh.status_text, newest)

    async def test_lease_race_is_waiting_without_crash_issue_or_failure_backoff(self):
        import rounds
        engine, gh, _ = self.engine(merge_rounds=True)
        batch = [PullRequest(12, "Feature", "feature", "Test-Staging", "a" * 40, ["ready"])]
        progress = str(engine.state_dir / "round.json")
        with patch("rounds.prepare_worktree", side_effect=rounds.CheckoutBusy("PC held by Hearth")), \
             patch("rounds.report_failure") as report:
            result = await engine._run_round(1, batch, "", rounds.key_of(batch), progress)
        self.assertEqual(result["result"], "waiting")
        self.assertEqual(result["key"], "")
        self.assertEqual(engine.state.merge_excluded, {})
        report.assert_not_called()
        self.assertEqual(rounds.read_file(progress)["state"], "waiting")
        self.assertFalse(any(s["state"] == "failed" for s in rounds.read_file(progress)["steps"]))
        self.assertEqual(rounds.should_start(engine.cfg, result, False, False, False, batch), (True, ""))

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

    async def test_prerequisite_closed_by_pr_merged_into_base_branch_is_done(self):
        # #1243's work landed on Test-Staging through "Closes #1243", but GitHub only closes issues for default-branch
        # merges; six tasks then waited on a "missing issue" forever (2026-11-09).
        tasks = {1: Task(1, "Dependent", "Depends on: #95")}
        engine, gh, runtime = self.engine(tasks)
        gh.closed_by_merge = {95: (91, "Test-Staging", "2026-11-09T00:00:00Z")}
        await engine.once()
        self.assertIn(1, {n for role, n, _ in runtime.calls if role == "worker"})
        self.assertIn("close #95", gh.writes)
        self.assertTrue(any("PR #91 merged into Test-Staging" in c for c in gh.comments.get(95, [])))

    async def test_prerequisite_with_no_merged_closing_pr_still_waits(self):
        tasks = {1: Task(1, "Dependent", "Depends on: #95")}
        engine, gh, runtime = self.engine(tasks)
        await engine.once()
        self.assertNotIn(1, {n for role, n, _ in runtime.calls if role == "worker"})
        self.assertIn("missing issue #95", engine._waiting[1])

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

    async def test_external_prerequisites_require_verified_completion_for_merge(self):
        for direct_pr_dependency in (False, True):
            for answer in (False, True, RuntimeError("GitHub unavailable")):
                with self.subTest(direct=direct_pr_dependency, answer=answer):
                    task = Task(1, "Feature", "" if direct_pr_dependency else "Depends on: #90",
                                [label_for(PR)])
                    engine, gh, runtime = self.engine({1: task}, merge_rounds=True, max_parallel=0)
                    body = "Closes #1" + ("\nDepends on: #90" if direct_pr_dependency else "")
                    gh.prs = [PullRequest(12, "Feature", task.branch, "Test-Staging", "a" * 40,
                                          ["ready"], body)]
                    options = {"side_effect": answer} if isinstance(answer, Exception) else {"return_value": answer}
                    with patch.object(gh, "issue_closed", **options):
                        await engine.once()
                    self.assertEqual(bool([call for call in runtime.calls if call[0] == "merger"]), answer is True)

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


class StatePersistenceTests(unittest.TestCase):
    def test_failed_atomic_replace_preserves_previous_valid_state(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            State(held=True).save(path)
            previous = path.read_bytes()
            with patch("loop.os.replace", side_effect=OSError("interrupted replacement")):
                with self.assertRaises(OSError):
                    State(held=False).save(path)
            self.assertEqual(path.read_bytes(), previous)
            self.assertTrue(State.load(path).held)
            self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_overlapping_saves_cannot_replace_newer_checkpoint_with_older_snapshot(self):
        import os
        import json
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            entered, release, second_started = threading.Event(), threading.Event(), threading.Event()
            serialized_second = threading.Event()
            original_replace, original_dumps = os.replace, json.dumps

            def serialize(data, **kwargs):
                if data.get("held"):
                    serialized_second.set()
                return original_dumps(data, **kwargs)

            def replace(source, target):
                if threading.current_thread().name == "old-checkpoint":
                    entered.set()
                    if not release.wait(2):
                        raise RuntimeError("fixture save timed out")
                original_replace(source, target)

            def newer_save():
                second_started.set()
                State(held=True).save(path)

            with patch("loop.os.replace", side_effect=replace), patch("loop.json.dumps", side_effect=serialize):
                older = threading.Thread(target=lambda: State(held=False).save(path), name="old-checkpoint")
                newer = threading.Thread(target=newer_save)
                older.start()
                try:
                    self.assertTrue(entered.wait(1))
                    newer.start()
                    self.assertTrue(second_started.wait(1))
                    self.assertFalse(serialized_second.wait(0.1), "serialize must wait for the earlier save")
                finally:
                    release.set()
                    older.join(2)
                    if newer.ident is not None:
                        newer.join(2)
            self.assertFalse(older.is_alive())
            self.assertFalse(newer.is_alive())
            self.assertTrue(State.load(path).held)

    def test_serialization_failure_does_not_truncate_checkpoint(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            State(merge_held=True).save(path)
            broken = State()
            broken.merge_last = {"invalid": object()}
            with self.assertRaises(TypeError):
                broken.save(path)
            self.assertTrue(State.load(path).merge_held)


if __name__ == "__main__":
    unittest.main()
