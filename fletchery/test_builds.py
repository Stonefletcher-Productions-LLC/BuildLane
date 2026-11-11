"""Scheduled builds (builds.py): schedule, daily repeat, routing to a node or this PC, following the node job, the PC
run, and the node's `package` recipe."""
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import builds  # noqa: E402
import machines  # noqa: E402
import node_worker  # noqa: E402
from builds import Builds  # noqa: E402

SHA = "b" * 40
NOW = datetime(2026, 11, 11, 22, 0, tzinfo=timezone.utc)


class FakeState:
    def __init__(self):
        self.builds = []
        self.saves = 0

    def save(self, path):
        self.saves += 1


class FakeCoord:
    def __init__(self, m):
        self.machines = m
        self.resolved = []

    def resolve(self, ref):
        self.resolved.append(ref)
        return SHA


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.logs = []
        self.engine = SimpleNamespace(
            state=FakeState(), state_dir=Path(self.tmp.name), dry_run=False, wake=lambda: None,
            cfg=SimpleNamespace(base_branch="Test-Staging", repo="o/r", name="LifeShare", builds=None, heavy_lease_file=""),
            ctx=SimpleNamespace(log=self.logs.append, gh=None, repo=None))
        self.m = machines.Machines(self.tmp.name, "o/r")
        self.engine.machines = FakeCoord(self.m)
        self.b = Builds(self.engine)

    def tearDown(self):
        self.tmp.cleanup()

    def node(self, labels=("os:windows", "role:tests", "role:cook"), at=None):
        self.m.add_machine("SF-NODE-01")
        self.m.seen("SF-NODE-01", "100.1.1.1", list(labels), 24, "1", {})

    def add(self, **kw):
        data = {"version": "0.3.9", "at": NOW.isoformat(), **kw}
        return self.b.add(data)


class ScheduleTests(Base):
    def test_checks_input(self):
        for bad in ({"version": "0.3"}, {"version": "v1.0.0"}, {"version": "0.3.9", "ref": "a..b"},
                    {"version": "0.3.9", "repeat": "weekly"}, {"version": "0.3.9", "where": "cloud"},
                    {"version": "0.3.9", "at": "2026-11-11T22:00"}):
            with self.assertRaises(ValueError, msg=bad):
                self.b.add(bad)

    def test_defaults_to_the_base_branch_and_waits_for_its_time(self):
        row = self.add()
        self.assertEqual((row["ref"], row["state"], row["where"]), ("Test-Staging", "scheduled", "auto"))
        self.b.tick(NOW - timedelta(minutes=1))
        self.assertEqual(self.b.rows[0]["state"], "scheduled")
        self.assertEqual(self.engine.machines.resolved, [])

    def test_reads_the_branch_once_at_its_time(self):
        self.add()
        self.b.tick(NOW)
        row = self.b.rows[0]
        self.assertEqual((row["sha"], row["state"], row["route"]), (SHA, "waiting", "pc"))
        self.assertEqual(self.engine.machines.resolved, ["Test-Staging"])
        self.b.tick(NOW + timedelta(minutes=5))
        self.assertEqual(self.engine.machines.resolved, ["Test-Staging"])  # not read again
        self.assertEqual(self.b.next_pc()["id"], row["id"])

    def test_daily_starts_a_copy_and_moves_on_a_day(self):
        self.add(repeat="daily")
        self.b.tick(NOW + timedelta(hours=3))
        sched, run = self.b.rows
        self.assertEqual(sched["state"], "scheduled")
        self.assertEqual(builds.utc(sched["at"]), NOW + timedelta(days=1))
        self.assertEqual((run["repeat"], run["parent"], run["state"]), ("", sched["id"], "waiting"))
        self.b.tick(NOW + timedelta(hours=4))
        self.assertEqual(len(self.b.rows), 2)

    def test_cancel(self):
        row = self.add(repeat="daily")
        self.assertEqual(self.b.cancel(row["id"])["message"], "Repeat stopped.")
        self.b.tick(NOW + timedelta(days=2))
        self.assertEqual(len(self.b.rows), 1)
        with self.assertRaises(ValueError):
            self.b.cancel(row["id"])


class RoutingTests(Base):
    def test_auto_goes_to_an_online_node_that_can_package(self):
        self.node()
        self.add(tests="full")
        self.b.tick(NOW)
        row = self.b.rows[0]
        self.assertEqual((row["route"], row["state"]), ("node", "queued"))
        job = self.m.job(row["job"])
        self.assertEqual((job["recipe"], job["sha"], job["params"]), ("package", SHA, {"version": "0.3.9", "tests": "full"}))
        self.assertIn("role:cook", machines.RECIPES["package"]["labels"])
        self.assertEqual(self.m.claim("SF-NODE-01")["id"], row["job"])

    def test_auto_without_a_cook_node_uses_this_pc(self):
        self.node(labels=("os:windows", "role:tests"))
        self.add()
        self.b.tick(NOW)
        self.assertEqual(self.b.rows[0]["route"], "pc")

    def test_pc_only_never_queues_a_node_job(self):
        self.node()
        self.add(where="pc")
        self.b.tick(NOW)
        self.assertEqual((self.b.rows[0]["route"], self.m.view()["queued"]), ("pc", 0))

    def test_follows_the_node_job_to_done(self):
        self.node()
        self.add()
        self.b.tick(NOW)
        row = self.b.rows[0]
        self.m.claim("SF-NODE-01")
        self.b.tick(NOW)
        self.assertEqual((row["state"], row["machine"]), ("running", "SF-NODE-01"))
        self.m.finish("SF-NODE-01", row["job"], 0, r"Packaged to E:\fletchery-builds\LifeShare-0.3.9 on SF-NODE-01")
        self.b.tick(NOW)
        self.assertEqual((row["state"], row["output"]), ("done", r"E:\fletchery-builds\LifeShare-0.3.9"))

    def test_a_node_without_the_recipe_hands_the_build_to_this_pc(self):
        self.node()
        self.add()
        self.b.tick(NOW)
        row = self.b.rows[0]
        self.m.claim("SF-NODE-01")
        self.m.finish("SF-NODE-01", row["job"], 2, "This node does not run 'package'")
        self.b.tick(NOW)
        self.assertEqual((row["state"], row["route"]), ("waiting", "pc"))

    def test_a_failed_node_build_fails(self):
        self.node()
        self.add()
        self.b.tick(NOW)
        row = self.b.rows[0]
        self.m.claim("SF-NODE-01")
        self.m.finish("SF-NODE-01", row["job"], 1, "Package 0.3.9 failed (exit 1)")
        self.b.tick(NOW)
        self.assertEqual((row["state"], row["reason"]), ("failed", "Package 0.3.9 failed (exit 1)"))

    def test_extra_node_labels(self):
        self.engine.cfg.builds = {"nodeLabels": ["unreal:5.8.3"]}
        self.node()  # no unreal:5.8.3
        self.add()
        self.b.tick(NOW)
        self.assertEqual(self.b.rows[0]["route"], "pc")


class PcRunTests(Base):
    def test_runs_the_package_steps_in_the_merge_checkout(self):
        self.engine.cfg.builds = {"outDir": str(Path(self.tmp.name) / "out")}
        self.add(where="pc")
        self.b.tick(NOW)
        row = self.b.rows[0]
        ws = Path(self.tmp.name) / "ws"
        ws.mkdir()
        repo = SimpleNamespace(remote="origin", git=mock.Mock(return_value=""))
        self.engine.ctx.repo = repo
        ran = []

        def fake_run(argv, **kw):
            ran.append(argv)
            return SimpleNamespace(returncode=0)
        with mock.patch("rounds.prepare_worktree", return_value=ws), mock.patch.object(builds.subprocess, "run", fake_run):
            self.b.run_pc(row)
        self.assertEqual(row["state"], "done", row["reason"])
        self.assertEqual(row["output"], str(Path(self.tmp.name) / "out" / "LifeShare-0.3.9"))
        repo.git.assert_any_call("checkout", "--no-overwrite-ignore", "-B", "fletchery/build-0.3.9", SHA, cwd=ws)
        joined = [" ".join(map(str, a)) for a in ran]
        self.assertTrue(any("Tools" in a and "Package.ps1" in a and "-Version 0.3.9" in a for a in joined), joined)
        self.assertTrue(any(a.startswith("git checkout -- Content/Sim") for a in joined))

    def test_a_failing_step_skips_the_rest_but_still_cleans_up(self):
        self.add(where="pc")
        self.b.tick(NOW)
        row = self.b.rows[0]
        ws = Path(self.tmp.name) / "ws"
        ws.mkdir()
        self.engine.ctx.repo = SimpleNamespace(remote="origin", git=mock.Mock(return_value=""))
        ran = []

        def fake_run(argv, **kw):
            ran.append(" ".join(map(str, argv)))
            return SimpleNamespace(returncode=3 if "Build.ps1" in ran[-1] else 0)
        with mock.patch("rounds.prepare_worktree", return_value=ws), mock.patch.object(builds.subprocess, "run", fake_run):
            self.b.run_pc(row)
        self.assertEqual(row["state"], "failed")
        self.assertIn("Build the editor failed (exit 3)", row["reason"])
        self.assertFalse(any("Package.ps1" in a for a in ran))
        self.assertTrue(any("Content/Sim" in a for a in ran))

    def test_busy_pc_keeps_it_waiting(self):
        import rounds
        self.add(where="pc")
        self.b.tick(NOW)
        row = self.b.rows[0]
        with mock.patch("rounds.prepare_worktree", side_effect=rounds.CheckoutBusy("heavy lease held")):
            self.b.run_pc(row)
        self.assertEqual(row["state"], "waiting")
        self.assertIn("heavy lease", row["reason"])

    def test_restart_marks_a_pc_build_that_was_running(self):
        self.engine.state.builds = [{"id": "x", "state": "running", "route": "pc", "version": "0.3.9"}]
        Builds(self.engine)
        self.assertEqual(self.engine.state.builds[0]["state"], "failed")


class RecipeTests(unittest.TestCase):
    def test_package_recipe_params_are_checked(self):
        self.assertEqual(machines.check_params("package", {"version": "0.3.9", "tests": "full", "cmd": "x"}),
                         {"version": "0.3.9", "tests": "full"})
        with self.assertRaises(ValueError):
            machines.check_params("package", {"version": "0.3.9;calc"})

    def test_node_steps(self):
        ws = Path("C:/w")
        steps = node_worker.recipe_steps("package", {"version": "0.3.9", "tests": "", "out_dir": r"E:\b\LifeShare-0.3.9"},
                                         ws, {"engine": r"E:\UE_5.8"}, Path("r.json"))
        names = [n for n, _, _ in steps]
        self.assertEqual(names[2:5], ["Build the editor", "BuildContent", "Package 0.3.9"])
        self.assertTrue(all(a for _, _, a in steps[-3:]))
        full = node_worker.recipe_steps("package", {"version": "0.3.9", "tests": "full", "out_dir": "o"}, ws, {}, Path("r"))
        self.assertIn("Build, BuildContent and Full tests", [n for n, _, _ in full])
        with self.assertRaises(ValueError):
            node_worker.recipe_steps("package", {"version": "x", "out_dir": "o"}, ws, {}, Path("r"))

    def test_package_out_never_overwrites(self):
        with tempfile.TemporaryDirectory() as t:
            first = node_worker.package_out(t, "LifeShare", "0.3.9", SHA)
            self.assertEqual(first.name, "LifeShare-0.3.9")
            first.mkdir()
            second = node_worker.package_out(t, "LifeShare", "0.3.9", SHA)
            self.assertEqual(second.name, "LifeShare-0.3.9-" + SHA[:10])
            second.mkdir()
            self.assertEqual(node_worker.package_out(t, "LifeShare", "0.3.9", SHA).name, f"LifeShare-0.3.9-{SHA[:10]}-2")


if __name__ == "__main__":
    unittest.main()
