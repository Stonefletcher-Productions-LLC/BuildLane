"""Machines: the node registry, the job queue, the worker API and the node worker, end to end on this machine."""
import json
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import machines  # noqa: E402
import node_worker  # noqa: E402
from machines import Machines, WorkerApi, check_listen  # noqa: E402

SHA = "a" * 40
NODE = ["os:windows", "ram:24", "role:tests", "role:cook", "unreal:5.8.4"]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.m = Machines(self.tmp.name, "owner/repo")

    def tearDown(self):
        self.tmp.cleanup()

    def node(self, name="SF-NODE-01", labels=NODE, ram=24):
        key = self.m.add_machine(name)
        self.m.seen(name, "100.77.158.113", labels, ram, "1", {})
        return key


class RegistryTests(Base):
    def test_key_is_shown_once_and_only_its_hash_is_kept(self):
        key = self.node()
        self.assertTrue(self.m.authenticate("SF-NODE-01", key))
        self.assertFalse(self.m.authenticate("SF-NODE-01", key + "x"))
        self.assertFalse(self.m.authenticate("other", key))
        self.assertNotIn(key.encode(), Path(self.m.path).read_bytes())

    def test_round_jobs_go_first_then_builds_then_pr_checks(self):
        self.node()
        check = self.m.enqueue("checkout-only", SHA, requested_by="PR check")
        build = self.m.enqueue("checkout-only", SHA, requested_by="build 0.4.0")
        round_job = self.m.enqueue("node-checks", SHA, requested_by="round 133")
        order = []
        for _ in range(3):
            job = self.m.claim("SF-NODE-01")
            order.append(job["id"])
            self.m.finish("SF-NODE-01", job["id"], 0, "")
        self.assertEqual(order, [round_job, build, check])

    def test_waiting_round_job_stops_the_running_pr_check(self):
        self.node()
        check = self.m.enqueue("checkout-only", SHA, requested_by="PR check")
        self.assertEqual(self.m.claim("SF-NODE-01")["id"], check)
        self.assertEqual(self.m.preempt_for_rounds(), [], "nothing waits for the node yet")
        round_job = self.m.enqueue("node-checks", SHA, requested_by="round 141")
        self.assertEqual(self.m.preempt_for_rounds(), [(check, "SF-NODE-01", round_job)])
        self.assertTrue(self.m.heartbeat("SF-NODE-01", check)["cancel"])
        self.assertEqual(self.m.preempt_for_rounds(), [], "one stop per round job; the node is already stopping")

    def test_preempt_leaves_builds_idle_nodes_and_other_round_jobs_alone(self):
        self.node()
        self.node("GAMING-PC")
        build = self.m.enqueue("checkout-only", SHA, requested_by="build 0.4.0")
        self.assertEqual(self.m.claim("SF-NODE-01")["id"], build)
        self.m.enqueue("node-checks", SHA, requested_by="round 141")
        self.assertEqual(self.m.preempt_for_rounds(), [], "GAMING-PC is idle and takes it")
        first = self.m.claim("GAMING-PC")["id"]
        self.m.enqueue("node-checks", SHA, requested_by="round 141 split")
        self.assertEqual(self.m.preempt_for_rounds(), [], "a build or another round job is never stopped")
        self.assertFalse(self.m.heartbeat("SF-NODE-01", build)["cancel"])
        self.assertFalse(self.m.heartbeat("GAMING-PC", first)["cancel"])

    def test_preempt_only_where_the_round_job_could_run(self):
        self.node(labels=["os:windows", "role:checks"])
        check = self.m.enqueue("checkout-only", SHA, requested_by="PR check", labels=[])
        self.assertEqual(self.m.claim("SF-NODE-01")["id"], check)
        self.m.enqueue("node-checks", SHA, requested_by="round 141")
        self.assertEqual(self.m.preempt_for_rounds(), [], "this node lacks the round job's labels")

    def test_revoke_stops_the_key_and_requeues_its_job(self):
        key = self.node()
        job = self.m.enqueue("node-checks", SHA)
        self.assertEqual(self.m.claim("SF-NODE-01")["id"], job)
        self.assertTrue(self.m.revoke("SF-NODE-01"))
        self.assertFalse(self.m.authenticate("SF-NODE-01", key))
        self.assertEqual(self.m.view()["jobs"][0]["state"], "queued")
        new = self.m.add_machine("SF-NODE-01")  # re-adding gives a new key; the old one stays dead
        self.assertTrue(self.m.authenticate("SF-NODE-01", new))
        self.assertFalse(self.m.authenticate("SF-NODE-01", key))

    def test_bad_names_are_refused(self):
        for name in ("", "../x", "a b", "x" * 80):
            with self.assertRaises(ValueError):
                self.m.add_machine(name)

    def test_listen_must_be_one_private_address(self):
        self.assertEqual(check_listen("100.83.43.111:8771"), ("100.83.43.111", 8771))
        self.assertEqual(check_listen("127.0.0.1:8771"), ("127.0.0.1", 8771))
        for bad in ("0.0.0.0:8771", "[::]:8771", ":8771", "8.8.8.8:8771", "example.com:8771", "100.83.43.111"):
            with self.assertRaises(ValueError, msg=bad):
                check_listen(bad)


class QueueTests(Base):
    def test_jobs_go_to_a_node_with_the_labels_and_ram(self):
        self.node("small", ["os:windows", "role:tests"], 8)
        self.node("cook", ["os:windows"], 64)
        self.node("SF-NODE-01")
        job = self.m.enqueue("node-checks", SHA, {"only": "RunFlowTest, RunM5aTest", "strict_slots": True}, pr=1666)
        self.assertEqual(self.m.view()["jobs"][0]["pr"], 1666)
        self.assertIsNone(self.m.claim("small"))   # too little RAM
        self.assertIsNone(self.m.claim("cook"))    # no role:tests
        got = self.m.claim("SF-NODE-01")
        self.assertEqual((got["id"], got["params"]), (job, {"only": "RunFlowTest,RunM5aTest", "strict_slots": True}))

    def test_extra_labels_narrow_the_choice(self):
        self.node()
        self.m.enqueue("node-checks", SHA, labels=["unreal:5.8.3"])
        self.assertIsNone(self.m.claim("SF-NODE-01"))
        self.m.enqueue("node-checks", SHA, labels=["unreal:5.8.4"])
        self.assertEqual(self.m.claim("SF-NODE-01")["id"], 2)

    def test_a_node_runs_one_job_at_a_time_oldest_first(self):
        self.node()
        a, b = self.m.enqueue("node-checks", SHA), self.m.enqueue("nightly-full", SHA)
        self.assertEqual(self.m.claim("SF-NODE-01")["id"], a)
        self.assertIsNone(self.m.claim("SF-NODE-01"))
        self.m.finish("SF-NODE-01", a, 0, "ok")
        self.assertEqual(self.m.claim("SF-NODE-01")["id"], b)

    def test_parameters_can_choose_tests_never_a_command(self):
        for only in ("RunFlowTest; Remove-Item", "a b", "$(x)", "x,,y", "../x"):
            with self.assertRaises(ValueError, msg=only):
                self.m.enqueue("node-checks", SHA, {"only": only})
        with self.assertRaises(ValueError):
            self.m.enqueue("powershell", SHA)
        with self.assertRaises(ValueError):
            self.m.enqueue("node-checks", "main")
        job = self.m.enqueue("nightly-full", SHA, {"only": "X", "cmd": "calc"})
        self.assertEqual(self.m.view()["jobs"][0]["params"], {})
        self.assertEqual(job, 1)

    def test_only_the_node_running_a_job_can_report_on_it(self):
        self.node(); self.node("other")
        job = self.m.enqueue("node-checks", SHA)
        self.m.claim("SF-NODE-01")
        for call in (lambda: self.m.heartbeat("other", job), lambda: self.m.append_log("other", job, "x"),
                     lambda: self.m.finish("other", job, 0)):
            with self.assertRaises(PermissionError):
                call()

    def test_cancel_reaches_the_node_at_its_heartbeat(self):
        self.node()
        queued = self.m.enqueue("node-checks", SHA)
        running = self.m.enqueue("node-checks", SHA)
        self.assertEqual(self.m.cancel(queued), "cancelled")
        self.assertEqual(self.m.claim("SF-NODE-01")["id"], running)
        self.assertFalse(self.m.heartbeat("SF-NODE-01", running)["cancel"])
        self.assertEqual(self.m.cancel(running), "stopping")
        self.assertTrue(self.m.heartbeat("SF-NODE-01", running)["cancel"])
        self.m.finish("SF-NODE-01", running, 130, "Cancelled from Fletchery")
        self.assertEqual(self.m.view()["jobs"][0]["state"], "cancelled")

    def test_a_silent_node_loses_its_job_once_then_it_fails(self):
        self.node()
        job = self.m.enqueue("node-checks", SHA)
        self.m.claim("SF-NODE-01")
        later = datetime.now(timezone.utc) + timedelta(minutes=10)
        self.assertEqual(self.m.reap(later), [job])
        self.assertEqual(self.m.view()["jobs"][0]["state"], "queued")
        self.m.claim("SF-NODE-01")
        self.m.reap(later)
        row = self.m.view()["jobs"][0]
        self.assertEqual((row["state"], row["attempts"]), ("lost", 2))

    def test_logs_append_and_are_capped(self):
        self.node()
        job = self.m.enqueue("node-checks", SHA)
        self.m.claim("SF-NODE-01")
        self.m.append_log("SF-NODE-01", job, "line 1\n")
        self.m.append_log("SF-NODE-01", job, "line 2\n")
        self.assertEqual(self.m.log_text(job), "line 1\nline 2\n")
        old = machines.LOG_MAX_BYTES
        machines.LOG_MAX_BYTES = 20
        try:
            self.m.append_log("SF-NODE-01", job, "x" * 100)
            self.m.append_log("SF-NODE-01", job, "more")
        finally:
            machines.LOG_MAX_BYTES = old
        self.assertIn("[log cut at 50 MB]", self.m.log_text(job))
        self.assertNotIn("more", self.m.log_text(job))


class StatusTests(Base):
    def test_statuses_use_the_old_names_once_per_change_with_a_shadow_prefix(self):
        self.node()
        sent = []
        post = lambda sha, state, context, desc: sent.append((state, context, desc))  # noqa: E731
        job = self.m.enqueue("nightly-full", SHA)
        self.m.post_statuses(post)
        self.m.post_statuses(post)
        self.assertEqual(sent, [("pending", "shadow/Nightly full tests (node)", "Queued in Fletchery")])
        self.m.claim("SF-NODE-01")
        self.m.finish("SF-NODE-01", job, 1, "2 of 40 failed on SF-NODE-01: RunFlowTest, RunM5aTest")
        self.m.post_statuses(post)
        self.assertEqual(sent[-1], ("failure", "shadow/Nightly full tests (node)",
                                    "2 of 40 failed on SF-NODE-01: RunFlowTest, RunM5aTest"))
        live = Machines(self.tmp.name, "owner/repo", shadow=False)
        live.enqueue("node-checks", SHA)
        live.post_statuses(post)
        self.assertEqual(sent[-1][1], "Node checks")

    def test_a_failed_post_is_sent_again(self):
        self.m.enqueue("node-checks", SHA)
        calls = []

        def post(*a):
            calls.append(a)
            if len(calls) == 1:
                raise RuntimeError("GitHub down")
        self.m.post_statuses(post)
        self.m.post_statuses(post)
        self.m.post_statuses(post)
        self.assertEqual(len(calls), 2)

    def test_checkout_only_reports_nothing(self):
        self.m.enqueue("checkout-only", SHA)
        sent = []
        self.m.post_statuses(lambda *a: sent.append(a))
        self.assertEqual(sent, [])

    def test_nightly_once_a_day_inside_its_window(self):
        at = datetime(2026, 11, 12, 7, 5, tzinfo=timezone.utc)
        self.assertEqual(self.m.nightly_due("07:00", at), "2026-11-12")
        self.m.nightly_done("2026-11-12")
        self.assertIsNone(self.m.nightly_due("07:00", at))
        self.assertIsNone(self.m.nightly_due("07:00", at.replace(hour=6)))
        self.assertIsNone(self.m.nightly_due("07:00", at.replace(hour=11)))
        self.assertEqual(self.m.nightly_due("07:00", at + timedelta(days=1)), "2026-11-13")


class FakeGh:
    def __init__(self):
        self.posts = []

    def api(self, path, method="GET", body=None, paginate=False, fresh=False):
        if method == "POST":
            self.posts.append((path, body))
            return {}
        return {"sha": "b" * 40}


class CoordinatorTests(unittest.TestCase):
    def test_tick_queues_the_nightly_on_main_and_posts_its_status(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            cfg = type("C", (), {"state_dir": d, "repo": "o/r", "machines": {"nightly": {"at": "07:00"}, "shadow": False}})()
            gh = FakeGh()
            c = machines.Coordinator(cfg, gh, log=lambda *_: None)
            at = datetime(2026, 11, 12, 7, 1, tzinfo=timezone.utc)
            c.tick(at)
            c.tick(at)
            jobs = c.machines.view()["jobs"]
            self.assertEqual([(j["recipe"], j["sha"], j["ref"]) for j in jobs], [("nightly-full", "b" * 40, "main")])
            self.assertEqual(gh.posts, [("repos/o/r/statuses/" + "b" * 40,
                                         {"state": "pending", "context": "Nightly full tests (node)", "description": "Queued in Fletchery"})])

    def test_resolve_refuses_odd_refs(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            cfg = type("C", (), {"state_dir": d, "repo": "o/r", "machines": {}})()
            c = machines.Coordinator(cfg, FakeGh(), log=lambda *_: None)
            self.assertEqual(c.resolve("A" * 40), "a" * 40)
            self.assertEqual(c.resolve("main"), "b" * 40)
            for bad in ("", "../x", "a b", "x?y"):
                with self.assertRaises(ValueError):
                    c.resolve(bad)


class ApiTests(Base):
    def test_unknown_keys_are_refused_and_throttled(self):
        api = WorkerApi(self.m, "127.0.0.1:0")
        key = self.node()
        self.assertEqual(api.handle("/v1/claim", "SF-NODE-01", "nope", "1.2.3.4", {})[0], 401)
        for _ in range(10):
            api.handle("/v1/claim", "SF-NODE-01", "nope", "1.2.3.4", {})
        self.assertEqual(api.handle("/v1/claim", "SF-NODE-01", key, "1.2.3.4", {})[0], 429)
        self.assertEqual(api.handle("/v1/claim", "SF-NODE-01", key, "5.6.7.8", {})[0], 200)

    def test_a_paused_node_checks_in_but_takes_nothing(self):
        api = WorkerApi(self.m, "127.0.0.1:0")
        key = self.node()
        self.m.enqueue("node-checks", SHA)
        code, body = api.handle("/v1/claim", "SF-NODE-01", key, "ip", {"paused": True, "stats": {"paused": True}})
        self.assertEqual((code, body["job"]), (200, None))
        self.assertTrue(self.m.view()["machines"][0]["stats"]["paused"])


class DashboardTests(Base):
    def test_dashboard_adds_a_machine_queues_and_cancels_a_job(self):
        from server import Dashboard
        logs = []
        cfg = type("C", (), {"state_dir": self.tmp.name, "repo": "o/r", "machines": {}})()
        fm = type("F", (), {})()
        fm.ctx = type("X", (), {"log": staticmethod(logs.append), "gh": FakeGh()})()
        fm.machines = machines.Coordinator(cfg, fm.ctx.gh, log=logs.append)
        dash = Dashboard.__new__(Dashboard)
        dash.fm = fm
        added = dash.machine_add({"name": "SF-NODE-01"})
        self.assertTrue(fm.machines.machines.authenticate("SF-NODE-01", added["key"]))
        job = dash.job_add({"recipe": "node-checks", "ref": "main", "only": "RunFlowTest", "pr": "1666"})
        self.assertEqual(job["sha"], "b" * 40)
        row = fm.machines.machines.view()["jobs"][0]
        self.assertEqual((row["ref"], row["pr"], row["params"]["only"], row["requested_by"]), ("main", 1666, "RunFlowTest", "dashboard"))
        self.assertEqual(dash.job_cancel({"id": job["id"]}), {"state": "cancelled"})
        self.assertEqual(dash.machine_revoke({"name": "SF-NODE-01"}), {"revoked": "SF-NODE-01"})
        with self.assertRaises(ValueError):
            dash.machine_revoke({"name": "SF-NODE-01"})
        fm.machines = None
        with self.assertRaises(ValueError):
            dash.job_add({"recipe": "node-checks", "ref": "main"})


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


class EndToEndTests(Base):
    """A real worker against a real API, fetching a real commit from a local repository."""

    def test_the_worker_takes_a_job_fetches_the_commit_and_reports(self):
        root = Path(self.tmp.name)
        src = root / "src"
        src.mkdir()
        git(src, "init", "-q")
        git(src, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "the commit")
        sha = git(src, "rev-parse", "HEAD")
        key = self.m.add_machine("node")
        (root / "key").write_text(key)
        api = WorkerApi(self.m, "127.0.0.1:0", log=lambda *_: None)
        api.start()
        try:
            port = api.server.server_address[1]
            cfg = {"coordinator": f"http://127.0.0.1:{port}", "machine": "node", "keyFile": str(root / "key"),
                   "labels": ["os:linux"], "workRoot": str(root / "work"), "repoUrl": str(src),
                   "recipes": ["checkout-only"]}
            (root / "w.json").write_text(json.dumps(cfg))
            job = self.m.enqueue("checkout-only", sha, ref="main")
            self.assertEqual(node_worker.main(["once", "--config", str(root / "w.json")]), 0)
        finally:
            api.stop()
        row = self.m.view()["jobs"][0]
        self.assertEqual((row["id"], row["state"], row["machine"]), (job, "passed", "node"))
        log = self.m.log_text(job)
        self.assertIn(f"{sha} the commit", log)
        self.assertIn("##[step] Show the commit: exit 0", log)
        node = self.m.view()["machines"][0]
        self.assertTrue(node["online"])
        self.assertEqual(node["labels"], ["cap:asset-sync", "os:linux"])

    def test_the_worker_refuses_a_recipe_it_does_not_run(self):
        root = Path(self.tmp.name)
        (root / "key").write_text("k")
        cfg = {"coordinator": "http://127.0.0.1:1", "machine": "n", "keyFile": str(root / "key"),
               "workRoot": str(root / "w"), "repoUrl": "x", "recipes": ["checkout-only"]}
        job = node_worker.Job(node_worker.Coordinator(cfg), cfg, {"id": 1, "recipe": "nightly-full", "sha": SHA})
        code, summary, _ = job.run()
        self.assertEqual(code, 2)
        self.assertIn("does not run", summary)


class RecipeTests(unittest.TestCase):
    def test_node_checks_mirror_the_workflow(self):
        ws = Path("E:/w/LifeShare")
        steps = node_worker.recipe_steps("node-checks", {"only": "RunFlowTest,RunM5aTest", "strict_slots": True}, ws,
                                         {"engine": r"E:\UE_5.8"}, Path("T:/r.json"))
        names = [s[0] for s in steps]
        self.assertEqual(names[2], "Build, BuildContent and tests")
        tests = steps[2][1]
        self.assertEqual(tests[-7:], ["-Engine", r"E:\UE_5.8", "-ResultJson", str(Path("T:/r.json")), "-Only",
                                      "RunFlowTest,RunM5aTest", "-StrictSlots"][-7:])
        self.assertEqual([s[2] for s in steps], [False, False, False, True, True, True])
        with self.assertRaises(ValueError):
            node_worker.recipe_steps("node-checks", {"only": "a;b"}, ws, {}, Path("r"))
        with self.assertRaises(ValueError):
            node_worker.recipe_steps("anything", {}, ws, {}, Path("r"))

    def test_stop_processes_quotes_every_path(self):
        argv = node_worker._stop_processes(Path("E:/it's/LifeShare"))
        self.assertIn("it''s", argv[-1])
        self.assertIn("-ProbePath @(", argv[-1])

    def test_summary_from_the_result_json(self):
        r = {"machine": "SF-NODE-01", "results": [{"name": "A", "pass": True}, {"name": "B", "pass": False}]}
        self.assertEqual(node_worker._summary(r), "1 of 2 failed on SF-NODE-01: B")
        self.assertEqual(node_worker._summary({"results": [{"name": "A", "pass": True}]}), "1 checks passed")


if __name__ == "__main__":
    unittest.main()
