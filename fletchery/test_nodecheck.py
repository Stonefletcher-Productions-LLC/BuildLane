"""PR checks on a test node (nodecheck.py): the queue, the node's recipe, and what Fletchery does with the result."""
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
import graph  # noqa: E402
import nodecheck  # noqa: E402
import node_worker  # noqa: E402
from config import Config  # noqa: E402
from github import PullRequest, _is_lane_feedback  # noqa: E402
from loop import Engine, State  # noqa: E402
from machines import Machines  # noqa: E402

BASE, HEAD, HEAD2 = "b" * 40, "c" * 40, "d" * 40
NODE = ["os:windows", "role:tests", "unreal:5.8.3"]


def pr(n, head=HEAD, labels=None):
    return PullRequest(n, f"PR {n}", f"branch-{n}", "Test-Staging", head, list(labels or [graph.PREFIX]))


class FakeGh:
    def __init__(self, prs):
        self.prs, self.labels, self.comments = prs, [], []

    def open_prs(self):
        return list(self.prs)

    def api(self, path, *a, **k):
        n = int(path.rsplit("/", 1)[1])
        p = next((x for x in self.prs if x.number == n), None)
        return {"state": "open", "head": {"sha": p.head_sha}} if p else {"state": "closed"}

    def add_labels(self, n, labels):
        self.labels.append((n, labels))

    def comment(self, n, body):
        self.comments.append((n, body))


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.m = Machines(self.tmp.name, "owner/repo", shadow=False)
        self.coord = SimpleNamespace(machines=self.m, conf={"prCheck": {"labels": ["unreal:5.8.3"]}},
                                     resolve=lambda ref: BASE)
        self.gh = FakeGh([pr(7)])
        self.logs = []
        self.eng = SimpleNamespace(cfg=Config(repo="owner/repo", base_branch="Test-Staging", pr_check_command="x"),
                                   ctx=SimpleNamespace(gh=self.gh, log=self.logs.append), state=State(),
                                   state_dir=Path(self.tmp.name))
        self.eng._node_pr_check_result = lambda coord, job: Engine._node_pr_check_result(self.eng, coord, job)

    def tearDown(self):
        self.tmp.cleanup()

    def online(self, labels=NODE, paused=False):
        self.m.add_machine("SF-NODE-01")
        self.m.seen("SF-NODE-01", "100.1.1.1", labels, 24, "1", {"paused": paused})

    def step(self):
        Engine._node_pr_check_step(self.eng, self.coord, False)

    def finish(self, code, summary, log=""):
        job = self.m.claim("SF-NODE-01")
        if log:
            self.m.append_log("SF-NODE-01", job["id"], log)
        self.m.finish("SF-NODE-01", job["id"], code, summary)
        return job


class QueueTests(Harness):
    def test_nothing_queued_without_an_eligible_node(self):
        self.step()
        self.online(labels=["os:windows", "role:tests", "unreal:5.8.4"])  # wrong engine
        self.step()
        self.assertEqual(self.m.active("pr-check"), [])

    def test_queues_one_check_with_the_base_tip_and_pr_head(self):
        self.gh.prs.append(pr(8))
        self.online()
        self.step()
        self.step()  # one at a time
        jobs = self.m.active("pr-check")
        self.assertEqual(len(jobs), 1)
        self.assertEqual((jobs[0]["sha"], jobs[0]["params"]), (BASE, {"pr": 7, "head": HEAD}))

    def test_paused_node_takes_nothing(self):
        self.online(paused=True)
        self.step()
        self.assertEqual(self.m.active("pr-check"), [])
        self.assertFalse(nodecheck.node_takes_checks(self.coord))

    def test_bad_params_are_refused(self):
        with self.assertRaises(ValueError):
            self.m.enqueue("pr-check", BASE, {"pr": 7, "head": "nope"})


class PauseTests(Harness):
    def test_paused_checks_take_the_node_out_and_give_back_the_queued_check(self):
        self.online()
        self.step()
        self.assertEqual(len(self.m.active("pr-check")), 1)
        self.assertEqual(self.m.set_paused("SF-NODE-01", "checks", True), ["checks"])
        self.assertIsNone(self.m.claim("SF-NODE-01"))           # the node does not take it
        self.assertFalse(nodecheck.node_takes_checks(self.coord))  # so the PC checks PRs again
        self.step()
        self.assertEqual(self.m.active("pr-check"), [])          # and the queued one is given back
        self.m.unhandled("pr-check") and Engine._node_pr_check_result(self.eng, self.coord, self.m.unhandled("pr-check")[0])
        self.assertEqual((self.gh.labels, self.gh.comments), ([], []))
        self.m.set_paused("SF-NODE-01", "checks", False)
        self.assertTrue(nodecheck.node_takes_checks(self.coord))

    def test_one_kind_paused_leaves_the_others(self):
        self.online()
        self.m.set_paused("SF-NODE-01", "tests", True)
        tests = self.m.enqueue("node-checks", BASE)
        check = self.m.enqueue("pr-check", BASE, {"pr": 7, "head": HEAD})
        self.assertEqual(self.m.claim("SF-NODE-01")["id"], check)  # skips the paused tests job
        view = {x["name"]: x for x in self.m.view()["machines"]}
        self.assertEqual(view["SF-NODE-01"]["paused_work"], ["tests"])
        self.assertEqual([j["id"] for j in self.m.active("node-checks")], [tests])
        with self.assertRaises(ValueError):
            self.m.set_paused("SF-NODE-01", "coding", True)
        with self.assertRaises(ValueError):
            self.m.set_paused("NOPE", "tests", True)


class RunJobTests(Harness):
    """machines.run_job: how a merge round runs its long tests on a node and reads the result."""
    def run_with(self, finish=None, online=True):
        import machines
        if online:
            self.online()
        out, t = [], [0.0]

        def sleep(_):
            t[0] += 60
            if not finish:
                return
            running = [j for j in self.m.active("node-checks") if j["state"] == "running"]
            if not running:
                self.m.claim("SF-NODE-01")
            elif t[0] >= 180:
                self.m.append_log("SF-NODE-01", running[0]["id"], "== RunFlowTest\nFAIL RunFlowTest\n")
                self.m.finish("SF-NODE-01", running[0]["id"], *finish)
        code = machines.run_job(self.m, "node-checks", BASE, {"only": "RunFlowTest"}, "round 9", ["unreal:5.8.3"],
                                out=out.append, clock=lambda: t[0], sleep=sleep)
        return code, "\n".join(out)

    def test_pass_fail_and_no_result(self):
        self.assertEqual(self.run_with((0, "Passed"))[0], 0)
        code, text = self.run_with((1, "1 of 1 failed: RunFlowTest"))
        self.assertEqual(code, 1)
        self.assertIn("FAIL RunFlowTest", text)
        self.assertEqual(self.run_with((2, "No result"))[0], 2)

    def test_no_node_means_no_result(self):
        code, text = self.run_with(online=False)
        self.assertEqual(code, 2)
        self.assertEqual(self.m.active("node-checks"), [])
        self.online()
        self.m.set_paused("SF-NODE-01", "tests", True)
        self.assertEqual(self.run_with(online=False)[0], 2)

    def test_nobody_takes_it(self):
        code, text = self.run_with(finish=None)
        self.assertEqual(code, 2)
        self.assertIn("not taken", text)
        self.assertEqual(self.m.active("node-checks"), [])


class RoundPromptTests(unittest.TestCase):
    def test_round_tests_text_only_when_on(self):
        import rounds
        cfg = SimpleNamespace(machines={"listen": "x"}, state_dir="E:/s")
        self.assertEqual(rounds.node_tests_text(cfg, 7, "Test-Staging"), "")
        cfg.machines["roundTests"] = {"labels": ["unreal:5.8.4"]}
        text = rounds.node_tests_text(cfg, 7, "Test-Staging")
        self.assertIn("fletchery/round-7", text)
        self.assertIn('run --recipe node-checks', text)
        self.assertIn("--label unreal:5.8.4", text)
        self.assertIn("Exit 2", text)


class PcPauseTests(unittest.TestCase):
    def test_pc_kinds(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        held = []
        eng = SimpleNamespace(state=State(), state_dir=Path(tmp.name), ctx=SimpleNamespace(log=lambda *_: None),
                              wake=lambda: None, PC_KINDS=Engine.PC_KINDS)
        eng.hold_merging = lambda on: (held.append(on), setattr(eng.state, "merge_held", on))
        eng.pc_paused_view = lambda: Engine.pc_paused_view(eng)
        self.assertEqual(Engine.pause_pc(eng, "coding", True), ["coding"])
        self.assertEqual(Engine.pause_pc(eng, "merging", True), ["coding", "merging"])
        self.assertEqual(held, [True])
        self.assertEqual(Engine.pause_pc(eng, "coding", False), ["merging"])
        self.assertEqual(State.load(Path(tmp.name) / "state.json").pc_paused, [])
        with self.assertRaises(ValueError):
            Engine.pause_pc(eng, "tests", True)


class ResultTests(Harness):
    def test_pass_labels_the_pr(self):
        self.online()
        self.step()
        self.finish(0, "Passed")
        self.step()
        self.assertEqual(self.gh.labels, [(7, ["ready"])])
        self.assertIn("SF-NODE-01", self.gh.comments[0][1])
        self.assertTrue(self.eng.state.pr_checks["7"]["passed"])
        self.assertEqual(self.m.unhandled("pr-check"), [])

    def test_check_failure_goes_to_the_fixer_with_the_step_output(self):
        self.online()
        self.step()
        log = ("##[step] PR check\n$ powershell ...\n== Build LifeShareEditor\nerror C2248: private\nFAIL Build (exit 1)\n"
               "##[step] PR check: exit 1\n##[step] Abort an unfinished merge\nfatal: no merge\n")
        self.finish(1, "PR check failed (exit 1)", log)
        self.step()
        body = self.gh.comments[0][1]
        self.assertTrue(_is_lane_feedback(body))
        self.assertIn("error C2248", body)
        self.assertNotIn("fatal: no merge", body)
        self.assertEqual(self.gh.labels, [])

    def test_no_result_and_infra_failures_record_nothing(self):
        self.online()
        for code, summary in ((2, "PR check failed (exit 2)"), (128, "Fetch the PR head failed (exit 128)")):
            self.step()
            self.finish(code, summary)
            Engine._node_pr_check_result(self.eng, self.coord, self.m.unhandled("pr-check")[0])
        self.assertEqual((self.gh.comments, self.eng.state.pr_checks), ([], {}))

    def test_moved_head_drops_the_result(self):
        self.online()
        self.step()
        self.gh.prs[0] = pr(7, head=HEAD2)
        self.finish(0, "Passed")
        Engine._node_pr_check_result(self.eng, self.coord, self.m.unhandled("pr-check")[0])
        self.assertEqual(self.gh.labels, [])

    def test_shadow_only_records(self):
        self.m.shadow = True
        self.online()
        self.step()
        self.finish(1, "Merge the PR onto the base failed (exit 1)")
        self.step()
        self.assertEqual((self.gh.labels, self.gh.comments), ([], []))
        self.assertFalse(self.eng.state.node_pr_checks["7"]["passed"])
        self.assertFalse(nodecheck.node_takes_checks(self.coord))


class RecipeTests(unittest.TestCase):
    def test_node_merges_the_head_and_checks_against_the_base(self):
        ws = Path("C:/w")
        steps = node_worker.recipe_steps("pr-check", {"pr": 7, "head": HEAD}, ws, {}, Path("r.json"), BASE)
        names = [s[0] for s in steps]
        self.assertLess(names.index(node_worker.MERGE_STEP), names.index(node_worker.CHECK_STEP))
        merge = dict((s[0], s[1]) for s in steps)[node_worker.MERGE_STEP]
        self.assertEqual(merge[-1], HEAD)
        check = dict((s[0], s[1]) for s in steps)[node_worker.CHECK_STEP]
        self.assertTrue(str(check[-3]).endswith("PrCheck.ps1"))
        self.assertEqual(check[-2:], ["-BaseRef", BASE])
        self.assertEqual((nodecheck.MERGE_STEP, nodecheck.CHECK_STEP), (node_worker.MERGE_STEP, node_worker.CHECK_STEP))
        with self.assertRaises(ValueError):
            node_worker.recipe_steps("pr-check", {"pr": 7, "head": HEAD}, ws, {}, Path("r.json"), "")


if __name__ == "__main__":
    unittest.main()


class OrphanRoundTests(unittest.TestCase):
    def test_left_over_round_is_marked_stopped(self):
        import json, os, tempfile, rounds, merge
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "merge-round.json")
            json.dump({"round": "132", "state": "running", "steps": [{"name": "Build", "state": "done"},
                       {"name": "Game specs", "state": "running"}]}, open(p, "w"))
            self.assertEqual(rounds.stop_orphan(p, 132, "x"), "")          # the live round is left alone
            self.assertEqual(rounds.stop_orphan(p, None, "restarted"), "132")
            r = json.load(open(p))
            self.assertEqual(r["state"], "stopped")
            self.assertEqual([s["state"] for s in r["steps"]], ["done", "todo"])
            self.assertEqual(rounds.stop_orphan(p, None, "again"), "")     # already history
            v = merge.view(p, os.path.join(d, "none.json"))
            self.assertFalse(v["active"])


class JobStepsTests(unittest.TestCase):
    LOG = ("##[step] Fetch the commit\n$ git fetch\n##[step] Fetch the commit: exit 0\n"
           "##[step] PR check\nPR check against abc\n== Build LifeShareEditor\nResult: Succeeded\n"
           "== Mapped preflight checks\nPASS  ScanUIText (5s)\nFAIL  RunSpecs (200s)\n"
           "##[step] PR check: exit 1\n##[step] Abort an unfinished merge\n##[step] Abort an unfinished merge: exit 128\n")

    def test_steps_sections_checks_and_cleanup(self):
        import machines
        with tempfile.TemporaryDirectory() as d:
            m = machines.Machines(d, "o/r")
            job = m.enqueue("pr-check", "a" * 40, params={"pr": 5, "head": "b" * 40})
            self.assertIsNotNone(job)
            jid = job if isinstance(job, int) else job["id"]
            # chunks split mid-line still give whole marks
            m._mark(jid, self.LOG[:30]); m._mark(jid, self.LOG[30:])
            steps = machines.parse_steps(m._marks(jid))
        self.assertEqual([s["name"] for s in steps], ["Fetch the commit", "PR check", "Abort an unfinished merge"])
        self.assertEqual(steps[0]["state"], "passed")
        check = steps[1]
        self.assertEqual(check["state"], "failed")
        self.assertEqual([(s["name"], s["state"]) for s in check["sections"]],
                         [("Build LifeShareEditor", "passed"), ("Mapped preflight checks", "failed")])
        self.assertEqual([(c["name"], c["state"], c["detail"]) for c in check["sections"][1]["checks"]],
                         [("ScanUIText", "passed", "5s"), ("RunSpecs", "failed", "200s")])
        self.assertTrue(steps[2]["cleanup"])
        self.assertTrue(all(s["start"] for s in steps))

    def test_failed_build_closes_its_section(self):
        import machines
        marks = [("t", l) for l in ["##[step] PR check", "== Build LifeShareEditor", "FAIL Build LifeShareEditor (exit 1)",
                                     "##[step] PR check: exit 1"]]
        steps = machines.parse_steps(marks)
        self.assertEqual(steps[0]["sections"][0]["state"], "failed")
        self.assertEqual(steps[0]["sections"][0]["detail"], "exit 1")
