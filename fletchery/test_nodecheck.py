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


class SplitRunTests(Harness):
    """Alex, 2026-11-12: two nodes share one round's checks and run them at the same time."""

    def test_two_nodes_split_the_checks(self):
        import machines
        self.online()
        self.m.add_machine("GAMING-PC")
        self.m.seen("GAMING-PC", "100.1.1.2", NODE, 32, "1", {})
        out, t = [], [0.0]

        def sleep(_):
            t[0] += 60
            for name in ("SF-NODE-01", "GAMING-PC"):
                job = self.m.claim(name)
                if job:
                    prm = job["params"] if isinstance(job["params"], dict) else __import__("json").loads(job["params"])
                    code = 1 if "RunFlowTest" in prm["only"] and name == "GAMING-PC" else 0
                    self.m.finish(name, job["id"], code, "done")
        code = machines.run_job(self.m, "node-checks", BASE, {"only": "RunSpecs,RunFlowTest,RunSmokeTest"}, "round 9",
                                [], out=out.append, clock=lambda: t[0], sleep=sleep)
        text = "\n".join(out)
        self.assertIn("split across 2 nodes (RunSpecs,RunSmokeTest | RunFlowTest)", text)
        self.assertEqual(code, 1, "one half failed, so the run failed")
        self.assertEqual(machines.split_checks("A,B,C", 5), ["A", "B", "C"])
        self.assertEqual(machines.split_checks("", 2), [""])

    def test_imports_never_split(self):
        import machines
        self.online()
        self.m.add_machine("GAMING-PC")
        self.m.seen("GAMING-PC", "100.1.1.2", NODE, 32, "1", {})
        out, t = [], [0.0]
        machines.run_job(self.m, "node-checks", BASE, {"only": "A,B", "imports": "ImportFurniture",
                                                       "push_branch": "fletchery/round-9"}, "round 9", [],
                         out=out.append, clock=lambda: t[0], sleep=lambda _: t.__setitem__(0, t[0] + 600))
        self.assertIn("Job ", out[0])
        self.assertNotIn("split", out[0])


class BuildCopyTests(Harness):
    """Alex, 2026-11-12: a build the node packages comes back to the PC (the 0.4.0 zip there was empty)."""

    def test_node_sends_the_build_and_the_pc_unpacks_it(self):
        import base64
        import node_worker
        self.online(NODE + ["role:cook"])
        job_id = self.m.enqueue("package", BASE, {"version": "0.4.0"}, requested_by="build 0.4.0")
        self.m.claim("SF-NODE-01")
        src = Path(self.tmp.name) / "node" / "LifeShare-0.4.0"
        (src / "Windows").mkdir(parents=True)
        (src / "Windows" / "LifeShare.exe").write_bytes(b"MZ" + bytes(range(256)) * 300)

        class Coord:
            def call(c, path, body, timeout=30):
                return self.m.upload("SF-NODE-01", body["job_id"], body["name"], body["offset"],
                                     base64.b64decode(body["data"]), body["done"], body["sha256"])
        where = node_worker.upload_build(Coord(), job_id, src, log=lambda *_: None, chunk=10000)
        self.assertTrue(where.endswith("LifeShare-0.4.0"))
        self.assertEqual((Path(where) / "Windows" / "LifeShare.exe").read_bytes(), b"MZ" + bytes(range(256)) * 300)
        self.assertTrue((Path(where).parent / "LifeShare-0.4.0.zip").exists())
        self.assertFalse((src.parent / "LifeShare-0.4.0.zip").exists(), "the node's temporary zip is removed")
        with self.assertRaises(PermissionError):
            self.m.upload("OTHER", job_id, "x.zip", 0, b"", True)
        with self.assertRaises(ValueError):
            self.m.upload("SF-NODE-01", job_id, "../x.zip", 0, b"", True)

    def test_summary_points_at_the_pc_copy(self):
        import builds
        s = "Packaged to E:\\n\\LifeShare-0.4.0 on SF-NODE-01; copied to E:\\pc\\LifeShare-0.4.0 on the PC"
        self.assertEqual(builds._out_from(s), "E:\\pc\\LifeShare-0.4.0")
        self.assertEqual(builds._out_from("Packaged to E:\\n\\B on SF-NODE-01"), "E:\\n\\B")


class PcLeaseTests(unittest.TestCase):
    """A node on the Gaming PC holds the PC's heavy lease per job and takes nothing while someone else holds it."""

    def test_lease(self):
        import node_worker
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        lease = Path(tmp.name) / "heavy.json"
        cfg = {"heavyLeaseFile": str(lease), "workRoot": tmp.name}
        self.assertFalse(node_worker.stats(cfg)["paused"])
        token = node_worker.take_lease(cfg, {"id": 5, "recipe": "node-checks", "sha": BASE}, Path(tmp.name))
        self.assertTrue(token and lease.exists())
        self.assertFalse(node_worker.stats(cfg)["paused"], "its own hold does not pause it")
        node_worker.drop_lease(cfg, token)
        self.assertFalse(lease.exists())
        lease.write_text('{"purpose": "merge round 140", "token": "x"}')
        st = node_worker.stats(cfg)
        self.assertTrue(st["paused"])
        self.assertIn("merge round 140", st["lease"])
        self.assertEqual(node_worker.take_lease(cfg, {"id": 6}, Path(tmp.name), wait=0, sleep=lambda _: None), "")
        node_worker.drop_lease(cfg, "other")
        self.assertTrue(lease.exists(), "never removes someone else's hold")


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
            json.dump({"round": "132", "state": "running", "started": "2026-11-11T21:26:27+00:00", "steps": [{"name": "Build", "state": "done"},
                       {"name": "Game specs", "state": "running"}]}, open(p, "w"))
            self.assertEqual(rounds.stop_orphan(p, 132, "x", "2026-11-11T23:00:00+00:00"), "")   # the live round is left alone
            self.assertEqual(rounds.stop_orphan(p, None, "x", "2026-11-11T21:00:00+00:00"), "")  # started after this Fletchery: not ours to stop
            self.assertEqual(rounds.stop_orphan(p, None, "restarted", "2026-11-11T23:00:00+00:00"), "132")
            r = json.load(open(p))
            self.assertEqual(r["state"], "stopped")
            self.assertEqual([s["state"] for s in r["steps"]], ["done", "todo"])
            self.assertEqual(rounds.stop_orphan(p, None, "again", ended={132}), "")     # already history
            v = merge.view(p, os.path.join(d, "none.json"))
            self.assertFalse(v["active"])
            json.dump({"round": "133", "state": "running", "started": "2026-11-11T23:10:00+00:00"}, open(p, "w"))
            self.assertEqual(rounds.stop_orphan(p, None, "cancelled", "2026-11-11T23:00:00+00:00", ended={133}), "133")


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


class NodeRoundTests(unittest.TestCase):
    def _coord(self, names, shadow=False):
        m = SimpleNamespace(eligible=lambda labels, ram, recipe: list(names), shadow=shadow)
        return SimpleNamespace(machines=m)

    def test_whole_round_on_node(self):
        import rounds
        cfg = SimpleNamespace(machines={"roundTests": {"labels": ["unreal:5.8.4"], "whole": True}}, state_dir="E:/s")
        prs = [SimpleNamespace(number=1, body="Closes #5"), SimpleNamespace(number=2, body="")]
        self.assertIn("SF-NODE-01", rounds.node_round(cfg, prs, self._coord(["SF-NODE-01"])))
        self.assertEqual(rounds.node_round(cfg, prs, self._coord([])), "")                       # no node free
        self.assertEqual(rounds.node_round(cfg, prs, self._coord(["N"], shadow=True)), "")      # shadow mode
        imp = prs + [SimpleNamespace(number=3, body="Lane-Imports: furniture")]
        self.assertEqual(rounds.node_round(cfg, imp, self._coord(["N"])), "")                   # imports stay on the PC
        bullet = prs + [SimpleNamespace(number=4, body="Checks: - **Lane-Imports:** ImportEnvironment")]
        self.assertIn("N", rounds.node_round(cfg, bullet, self._coord(["N"])))                 # mid-line mention is not a line
        bullet[-1].body = "Notes\n- **Lane-Imports:** ImportEnvironment"
        self.assertEqual(rounds.node_round(cfg, bullet, self._coord(["N"])), "")                # a bullet line counts
        task = SimpleNamespace(number=974, pr_number=1679, body="## Proof\n- Lane-Imports: ImportEnvironment")
        woods = prs + [SimpleNamespace(number=1679, body="Closes #974\nLane-Imports: ImportEnvironment acceptance not rerun")]
        woods[-1].body = "Closes #974\n- Prior Lane-Imports: ImportEnvironment acceptance was not rerun."
        self.assertIn("N", rounds.node_round(cfg, woods, self._coord(["N"])))
        self.assertEqual(rounds.node_round(cfg, woods, self._coord(["N"]), {974: task}), "")  # the issue asks for imports
        cfg.machines["roundTests"]["whole"] = False
        self.assertEqual(rounds.node_round(cfg, prs, self._coord(["N"])), "")
        text = rounds.node_tests_text(SimpleNamespace(machines={"roundTests": {"labels": []}}, state_dir="E:/s"), 7, "Test-Staging", whole=True)
        self.assertIn("no Unreal on this PC", text)
        self.assertIn("fletchery/round-7", text)


class NodeImportTests(unittest.TestCase):
    """Alex, 2026-11-12: let SF-NODE-01 push imported content so import rounds leave the PC."""

    def _coord(self, names, seen):
        def eligible(labels, ram, recipe):
            seen.append(list(labels))
            return list(names)
        return SimpleNamespace(machines=SimpleNamespace(eligible=eligible, shadow=False))

    def test_import_round_goes_to_a_node_with_a_write_key_only_when_allowed(self):
        import rounds
        cfg = SimpleNamespace(machines={"roundTests": {"labels": ["unreal:5.8.4"], "whole": True}}, state_dir="E:/s")
        prs = [SimpleNamespace(number=3, body="Lane-Imports: ImportFurniture")]
        seen = []
        self.assertEqual(rounds.node_round(cfg, prs, self._coord(["N"], seen)), "")   # not allowed: stays on the PC
        cfg.machines["roundTests"]["imports"] = True
        self.assertIn("N", rounds.node_round(cfg, prs, self._coord(["N"], seen)))
        self.assertEqual(seen[-1], ["unreal:5.8.4", "role:imports"])
        text = rounds.node_tests_text(cfg, 9, "Test-Staging", whole=True)
        self.assertIn("--push-branch fletchery/round-9", text)
        self.assertIn("git merge --ff-only FETCH_HEAD", text)
        self.assertIn("no node has the `role:imports` label", text)

    def test_params_accept_imports_only_with_a_round_branch(self):
        from machines import check_params
        ok = check_params("node-checks", {"imports": "ImportFurniture, BuildTown:Lakeview", "push_branch": "fletchery/round-9"})
        self.assertEqual(ok["imports"], "ImportFurniture,BuildTown:Lakeview")
        for bad in ({"imports": "x;rm", "push_branch": "fletchery/round-9"},
                    {"imports": "ImportFurniture", "push_branch": "main"},
                    {"imports": "ImportFurniture"}):
            with self.assertRaises(ValueError):
                check_params("node-checks", bad)
        self.assertNotIn("imports", check_params("node-checks", {"only": "RunSpecs"}))

    def test_worker_runs_imports_before_tests_and_needs_a_push_key(self):
        ws = Path("ws")
        params = {"only": "RunSpecs", "imports": "ImportFurniture", "push_branch": "fletchery/round-9"}
        names = [s[0] for s in node_worker.recipe_steps("node-checks", params, ws, {"gitPushKey": "k"}, Path("r"))]
        self.assertLess(names.index(node_worker.IMPORT_STEP), names.index("Build, BuildContent and tests"))
        step = dict((s[0], s[1]) for s in node_worker.recipe_steps("node-checks", params, ws, {"gitPushKey": "k"}, Path("r")))
        self.assertIn("fletchery/round-9", step[node_worker.IMPORT_STEP])
        with self.assertRaises(ValueError):
            node_worker.recipe_steps("node-checks", params, ws, {}, Path("r"))                     # no write key
        with self.assertRaises(ValueError):
            node_worker.recipe_steps("node-checks", dict(params, push_branch="main"), ws, {"gitPushKey": "k"}, Path("r"))
        plain = [s[0] for s in node_worker.recipe_steps("node-checks", {"only": "RunSpecs"}, ws, {}, Path("r"))]
        self.assertNotIn(node_worker.IMPORT_STEP, plain)
