"""PC check: worker PRs are built and quick-tested on the PC before they get the merge label."""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import graph
import prcheck
from config import Config
from github import PullRequest


def pr(n, labels=None, head=None, base="Test-Staging"):
    labels = (graph.PREFIX,) if labels is None else labels
    return PullRequest(n, f"PR {n}", f"branch-{n}", base, head or str(n % 10) * 40, list(labels))


class CandidateTests(unittest.TestCase):
    def test_only_unlabelled_unchecked_worker_prs_oldest_first(self):
        cfg = Config(base_branch="Test-Staging")
        prs = [pr(30), pr(12), pr(13, (graph.PREFIX, "ready")), pr(14, ()), pr(15, base="main"), pr(16)]
        checked = {"16": {"head": prs[-1].head_sha, "passed": False}}
        got = [p.number for p in prcheck.candidates(prs, cfg, checked, "ready", graph.PREFIX)]
        self.assertEqual(got, [12, 30])

    def test_new_head_is_checked_again(self):
        cfg = Config(base_branch="Test-Staging")
        checked = {"16": {"head": "a" * 40}}
        self.assertEqual([p.number for p in prcheck.candidates([pr(16, head="b" * 40)], cfg, checked, "ready", graph.PREFIX)], [16])

    def test_failure_comment_reaches_the_fixer(self):
        from github import _is_lane_feedback
        text = prcheck.failure_comment(pr(5), prcheck.CheckResult(1, "error C2248"), "Test-Staging")
        self.assertTrue(_is_lane_feedback(text))
        self.assertIn("error C2248", text)


class FakeGitRepo:
    def __init__(self, path, fail_merge=False):
        self.path, self.fail_merge, self.calls = path, fail_merge, []
        self.remote, self.base = "origin", "Test-Staging"

    def git(self, *args, cwd=None, check=True, stdin=None):
        self.calls.append(args)
        if args[0] == "merge" and args[1] != "--abort" and self.fail_merge:
            raise RuntimeError("CONFLICT (content): Merge conflict in a.cpp")
        return ""


class RunTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def cfg(self, command):
        return Config(base_branch="Test-Staging", pr_check_command=command)

    def run_with(self, repo, command):
        with patch("rounds.prepare_worktree", return_value=self.dir):
            return prcheck.run(repo, pr(7), self.cfg(command))

    def test_pass_fail_and_busy_exit_codes(self):
        py = f'"{sys.executable}" -c'
        self.assertEqual(self.run_with(FakeGitRepo(self.dir), f'{py} "print(\'BUILD OK\')"').code, 0)
        failed = self.run_with(FakeGitRepo(self.dir), f'{py} "import sys; print(\'FAIL RunSpecs\'); sys.exit(1)"')
        self.assertEqual(failed.code, 1)
        self.assertIn("FAIL RunSpecs", failed.output)
        self.assertEqual(self.run_with(FakeGitRepo(self.dir), f'{py} "import sys; sys.exit(2)"').code, prcheck.NO_RESULT)

    def test_merge_conflict_is_a_failure_and_aborts(self):
        repo = FakeGitRepo(self.dir, fail_merge=True)
        res = self.run_with(repo, "echo never")
        self.assertEqual(res.code, 1)
        self.assertIn("does not merge cleanly", res.output)
        self.assertIn(("merge", "--abort"), repo.calls)

    def test_busy_checkout_is_no_result(self):
        import rounds
        with patch("rounds.prepare_worktree", side_effect=rounds.CheckoutBusy("UnrealEditor.exe holds it")):
            res = prcheck.run(FakeGitRepo(self.dir), pr(7), self.cfg("echo x"))
        self.assertEqual(res.code, prcheck.NO_RESULT)


class EngineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import test_dispatch_resilience as fixtures
        self.factory = fixtures.DispatchResilienceTests.engine

    async def finish(self, engine):
        job = engine.round_job
        self.assertIsNotNone(job)
        await job
        engine.round_job, engine.round_info = None, {}

    async def test_pass_labels_ready_and_does_not_hold_workers(self):
        engine, gh, rt = self.factory(self, merge_rounds=True, max_parallel=0, pr_check_command="check")
        gh.prs = [pr(21)]
        with patch("prcheck.run", return_value=prcheck.CheckResult(0, "ALL PASSED")):
            engine._merge_round(False)
            self.assertEqual(engine.round_info.get("check"), 21)
            self.assertFalse(engine.merge_priority)
            self.assertTrue(engine._round_shares(None))
            await self.finish(engine)
        self.assertIn("ready", gh.prs[0].labels)
        self.assertTrue(engine.state.pr_checks["21"]["passed"])
        self.assertIn("PC check passed", gh.comments[21][-1])

    async def test_failure_comments_and_is_not_retried_at_same_head(self):
        engine, gh, rt = self.factory(self, merge_rounds=True, max_parallel=0, pr_check_command="check")
        gh.prs = [pr(22)]
        with patch("prcheck.run", return_value=prcheck.CheckResult(1, "error C2248: protected")) as run:
            engine._merge_round(False)
            await self.finish(engine)
            engine._merge_round(False)
        self.assertNotIn("ready", gh.prs[0].labels)
        self.assertIn("PC check failed", gh.comments[22][-1])
        self.assertEqual(run.call_count, 1)

    async def test_busy_records_nothing_and_rounds_take_turns(self):
        engine, gh, rt = self.factory(self, merge_rounds=True, max_parallel=0, pr_check_command="check")
        gh.prs = [pr(23), pr(24, (graph.PREFIX, "ready"))]
        with patch("prcheck.run", return_value=prcheck.CheckResult(prcheck.NO_RESULT, "busy")):
            engine._merge_round(False)
            await self.finish(engine)
        self.assertNotIn("23", engine.state.pr_checks)
        # after a check, a waiting ready PR gets the next turn
        engine._last_pc_job = "check"
        self.assertFalse(engine._start_pr_check())
        self.assertTrue(engine._start_pr_check())
        await asyncio.sleep(0)
        engine.round_job.cancel()
        engine.round_job, engine.round_info = None, {}

    async def test_check_with_no_result_gives_a_waiting_round_the_next_turn(self):
        engine, gh, rt = self.factory(self, merge_rounds=True, max_parallel=0, pr_check_command="check")
        gh.prs = [pr(25), pr(26, (graph.PREFIX, "ready"))]
        with patch("prcheck.run", return_value=prcheck.CheckResult(prcheck.NO_RESULT, "busy")) as run:
            engine._merge_round(False)
            await self.finish(engine)
            self.assertEqual(run.call_count, 1)
            # the busy check does not start again straight away: the ready PR's round gets the turn
            self.assertFalse(engine._start_pr_check())
            self.assertEqual(run.call_count, 1)
            # and the check comes back on the turn after that
            self.assertTrue(engine._start_pr_check())
            await asyncio.sleep(0)
            engine.round_job.cancel()
            engine.round_job, engine.round_info = None, {}

    async def test_new_prs_open_without_ready_when_checks_are_on(self):
        import pipeline
        self.assertIn("pr_check_command", Path(pipeline.__file__).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
