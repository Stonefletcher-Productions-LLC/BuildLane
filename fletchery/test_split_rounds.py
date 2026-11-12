"""A failed round is tried in parts, so one bad PR no longer stops the rest landing; conflicting PRs stay out of rounds.
No real services."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import rounds
from config import Config
from github import FakeGitHub, PullRequest
from graph import Task
from loop import Engine
from pipeline import Context
from repo import FakeRepo
from runtime import FakeRuntime


def pr(n, head=None, body=""):
    return PullRequest(n, f"PR {n}", f"branch-{n}", "Test-Staging", head or str(n % 10) * 40, ["ready"], body)


def halves(prs):
    return rounds.split_halves(prs, {}, set())


class SplitLogicTests(unittest.TestCase):
    def test_failed_batch_with_no_culprit_is_halved(self):
        batch = [pr(11), pr(12), pr(13), pr(14)]
        res = {"round": 140, "result": "failed", "stop_reason": "RunM1CommandTest failed: click appends Shower"}
        split, out = rounds.after_round({}, batch, res, False, halves)
        self.assertEqual(out, [])
        self.assertEqual([[x["pr"] for x in g] for g in split["groups"]], [[11, 12], [13, 14]])
        self.assertEqual(split["from_round"], 140)
        self.assertIn("Shower", split["why"])

    def test_named_culprit_or_landed_round_needs_no_split(self):
        batch = [pr(11), pr(12)]
        for res in ({"result": "failed", "left_out": [{"pr": 12, "why": "spec failed"}]},
                    {"result": "landed", "merged": [11, 12]}, {"result": "nothing"}):
            self.assertEqual(rounds.after_round({}, batch, res, False, halves), ({}, []))

    def test_a_pr_failing_alone_is_left_out(self):
        split = {"from_round": 140, "why": "x", "groups": [[{"pr": 12, "head": "2" * 40}], [{"pr": 13, "head": "3" * 40}]]}
        res = {"round": 142, "result": "failed", "stop_reason": "RunM1CommandTest failed again"}
        split, out = rounds.after_round(split, [pr(12)], res, True, halves)
        self.assertEqual([n for n, _ in out], [12])
        self.assertIn("on its own", out[0][1])
        self.assertEqual([[x["pr"] for x in g] for g in split["groups"]], [[13]])

    def test_no_verdict_failures_never_blame_a_lone_pr(self):
        for why in ("RunFlowTest was cancelled without a result; RunFourPlayerSmokeTest had not started.",
                    "node-checks exit 2: no node free", "the node was lost", "timed out after 120 minutes"):
            with self.subTest(why=why):
                split, out = rounds.after_round({"groups": [[{"pr": 12, "head": "2" * 40}]]}, [pr(12)],
                                                {"result": "failed", "stop_reason": why}, True, halves)
                self.assertEqual((split, out), ({}, []))

    def test_split_survives_limits_and_interruptions(self):
        split = {"from_round": 140, "why": "x", "groups": [[{"pr": 12, "head": "2" * 40}]]}
        for result in ("limited", "unreachable", "waiting", "interrupted", "crashed", "checkout_failed"):
            self.assertEqual(rounds.after_round(split, [pr(12)], {"result": result}, True, halves)[0], split)

    def test_landed_part_moves_to_the_next(self):
        split = {"from_round": 140, "why": "x", "groups": [[{"pr": 11, "head": "1" * 40}], [{"pr": 12, "head": "2" * 40}]]}
        split, out = rounds.after_round(split, [pr(11)], {"result": "landed", "merged": [11]}, True, halves)
        self.assertEqual([[x["pr"] for x in g] for g in split["groups"]], [[12]])

    def test_split_batch_drops_changed_heads_and_finished_groups(self):
        split = {"groups": [[{"pr": 11, "head": "old" + "1" * 37}], [{"pr": 12, "head": "2" * 40}, {"pr": 13, "head": "3" * 40}]]}
        ready = [pr(11), pr(12), pr(14)]
        self.assertEqual([p.number for p in rounds.split_batch(split, ready)], [12])
        self.assertEqual(split["groups"], [[{"pr": 12, "head": "2" * 40}]])
        self.assertEqual(rounds.split_batch({"groups": [[{"pr": 99, "head": "x"}]]}, ready), [])

    def test_halves_keep_a_prerequisite_with_its_dependent(self):
        # #12 depends on #13, which falls in the second half: #12 moves there too.
        batch = [pr(11), pr(12, body="Depends on: #13"), pr(13), pr(14)]
        self.assertEqual([[p.number for p in g] for g in halves(batch)], [[11], [12, 13, 14]])
        self.assertEqual([[p.number for p in g] for g in halves([pr(11)])], [[11]])

    def test_split_prompt_runs_the_failing_check_first(self):
        text = rounds.split_text({"from_round": 140, "why": "RunM1CommandTest failed"}, [pr(11), pr(12)])
        self.assertIn("#11, #12", text)
        self.assertIn("RunM1CommandTest failed", text)
        self.assertIn("Run the check that failed first", text)
        self.assertNotIn("Base is red", text)
        lone = rounds.split_text({"from_round": 140, "why": "x"}, [pr(12)], "Test-Staging")
        self.assertIn("`origin/Test-Staging` alone", lone)
        self.assertIn("Base is red:", lone)

    def test_red_base_blames_no_lone_pr_and_lone_rounds_outside_a_split_blame_none(self):
        res = {"result": "failed", "stop_reason": "Base is red: RunM1CommandTest fails on Test-Staging too"}
        self.assertEqual(rounds.after_round({"groups": [[{"pr": 12, "head": "2" * 40}]]}, [pr(12)], res, True, halves),
                         ({}, []))
        self.assertEqual(rounds.after_round({}, [pr(12)], {"result": "failed", "stop_reason": "Build failed."}, False,
                                            halves), ({}, []))


class SplitEngineTests(unittest.IsolatedAsyncioTestCase):
    def engine(self, answer):
        folder = tempfile.TemporaryDirectory(prefix="fletchery-split-")
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        cfg = Config(project_root=str(root), base_branch="Test-Staging", watch_failures=False, auto_prerequisites=False,
                     merge_rounds=True, max_parallel=0)
        gh = FakeGitHub({}, set())
        runtime = FakeRuntime({("merger", 0): answer})
        ctx = Context(cfg, gh, FakeRepo(root / "worktrees"), runtime, run_gates=lambda *_: [], log=lambda _: None)
        return Engine(ctx, root / "state"), gh, runtime

    async def run_round(self, engine):
        before = engine.state.merge_last.get("round")
        await engine.once()
        if engine.round_job is not None:
            await engine.round_job
            engine.round_job, engine.round_info = None, {}
        self.assertNotEqual(engine.state.merge_last.get("round"), before, engine.merge_why)
        return engine.state.merge_last

    async def test_bad_pr_is_found_and_the_rest_land(self):
        prompts = []

        def answer(prompt):
            prompts.append(prompt)
            part = prompt.split("# This round tries part of a failed batch", 1)
            if len(part) == 1 or "#12" in part[1].split("The failure was", 1)[0]:
                return {"state": "failed", "landed_pr": 0, "merged": [], "left_out": [],
                        "stop_reason": "RunM1CommandTest failed: click appends Shower", "summary": "red"}
            return {"state": "landed", "landed_pr": 500, "merged": [11], "left_out": [], "summary": "landed"}

        engine, gh, _ = self.engine(answer)
        gh.prs = [pr(11), pr(12)]
        first = await self.run_round(engine)
        self.assertEqual(first["result"], "failed")
        self.assertEqual(first["key"], "", "the next part must not wait out mergeRetryMinutes")
        second = await self.run_round(engine)
        self.assertEqual(second["result"], "landed")
        self.assertIn("#11", prompts[1].split("# This round tries part")[1][:400])
        third = await self.run_round(engine)
        self.assertEqual(third["result"], "failed")
        self.assertEqual(engine.state.merge_split, {})
        self.assertEqual(engine.state.merge_excluded["12"]["head"], "2" * 40)
        self.assertEqual(third["left_out"][0]["pr"], 12)

    async def test_conflicting_pr_stays_out_of_the_round(self):
        engine, gh, _ = self.engine({"state": "nothing", "summary": "ok"})
        gh.prs = [pr(11), pr(12), pr(13, body="Depends on: #12")]
        gh.conflicts = {12}
        with patch.object(gh, "pr_conflicts", wraps=gh.pr_conflicts) as lookup:
            await engine.once()
            progress = rounds.read_file(str(engine.state_dir / "merge-round.json"))
            self.assertEqual(progress["prs"], [11], "the conflicting PR and the PR that needs it wait")
            self.assertTrue(engine.state.merge_excluded["12"]["conflict"])
            self.assertIn("conflicts with Test-Staging", engine.state.merge_excluded["12"]["why"])
            if engine.round_job is not None:
                await engine.round_job
                engine.round_job, engine.round_info = None, {}
            calls = lookup.call_count
            await engine.once()
            self.assertNotIn(12, [c.args[0] for c in lookup.call_args_list[calls:]],
                             "a PR already left out at its head is not looked up again")


if __name__ == "__main__":
    unittest.main()
