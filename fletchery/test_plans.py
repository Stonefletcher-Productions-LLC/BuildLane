"""Planned integration identity, restart, draining and release-controller proofs."""
import asyncio
import base64
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import Mock
import unittest

import test_dispatch_resilience as fixtures
from github import PullRequest
from graph import Task
from loop import Engine, State


class PlanTests(unittest.IsolatedAsyncioTestCase):
    engine = fixtures.DispatchResilienceTests.engine

    def setup_engine(self):
        engine, gh, rt = self.engine({1: Task(1, "Independent feature")}, merge_rounds=True)
        gh.prs = [PullRequest(12, "Chosen", "ready", "Test-Staging", "a" * 40, ["ready"])]
        return engine, gh, rt

    def package(self, engine, **changes):
        return engine.plans.enqueue({"kind": "package", "version": "0.3.7", "target": "a" * 40, **changes})

    def controller(self, gh, state):
        def api(path, method="GET", body=None):
            if "/git/ref/" in path:
                return {"object": {"sha": "b" * 40}}
            if "/contents/" in path:
                return {"encoding": "base64", "content": base64.b64encode(json.dumps(state).encode()).decode()}
            if method == "POST":
                return None
            raise AssertionError(path)
        gh.api = Mock(side_effect=api)

    async def test_future_window_persists_without_starving_feature_work(self):
        engine, gh, rt = self.setup_engine()
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        row = engine.plans.enqueue({"kind": "merge", "not_before": future, "prs": [12]})
        await engine.once()
        self.assertFalse(engine.merge_priority)
        self.assertIn("worker", [role for role, _, _ in rt.calls])
        self.assertNotIn("merger", [role for role, _, _ in rt.calls])
        restored = State.load(engine.state_dir / "state.json")
        self.assertTrue(restored.planned_integration)
        self.assertEqual(restored.integration_queue[0]["heads"], {"12": "a" * 40})
        self.assertEqual(restored.integration_queue[0]["id"], row["id"])

    async def test_due_window_drains_then_runs_only_frozen_selected_prs(self):
        engine, gh, rt = self.setup_engine()
        row = engine.plans.enqueue({"kind": "merge", "prs": [12]})
        gh.prs.append(PullRequest(13, "Later", "later", "Test-Staging", "b" * 40, ["ready"]))
        existing = asyncio.get_running_loop().create_future()
        engine.jobs[existing] = (88, None, None)
        engine.dispatch()
        self.assertEqual(engine.plans.rows[0]["state"], "draining")
        self.assertTrue(engine.merge_priority)
        self.assertIsNone(engine.round_job)
        self.assertFalse(existing.cancelled())
        engine.jobs.pop(existing); existing.set_result(None)
        engine.dispatch()
        self.assertEqual(engine.round_info["prs"], [12])
        self.assertEqual(engine.round_info["request_id"], row["id"])
        await engine.round_job
        self.assertEqual(engine.plans.rows[0]["state"], "blocked")  # fake merger lands nothing

    async def test_changed_head_blocks_request_but_dispatches_independent_worker(self):
        engine, gh, rt = self.setup_engine()
        engine.plans.enqueue({"kind": "merge", "prs": [12]})
        gh.prs[0].head_sha = "b" * 40
        await engine.once()
        self.assertEqual(engine.plans.rows[0]["state"], "blocked")
        self.assertFalse(engine.merge_priority)
        self.assertIn("worker", [role for role, _, _ in rt.calls])

    async def test_missing_dependency_cannot_start_partial_selected_batch(self):
        engine, gh, rt = self.setup_engine()
        gh.prs[0].body = "Depends on: #999"
        engine.plans.enqueue({"kind": "merge", "prs": [12]})
        await engine.once()
        self.assertFalse(engine.merge_priority)
        self.assertNotIn("merger", [role for role, _, _ in rt.calls])

    async def test_merge_pause_is_not_cleared_by_enqueue(self):
        engine, gh, rt = self.setup_engine()
        engine.state.merge_held = True
        engine.plans.enqueue({"kind": "merge", "prs": [12]})
        await engine.once()
        self.assertTrue(engine.state.merge_held)
        self.assertIn("worker", [role for role, _, _ in rt.calls])

    async def test_pausing_a_draining_window_releases_feature_reservation(self):
        engine, gh, rt = self.setup_engine()
        engine.plans.enqueue({"kind": "merge", "prs": [12]})
        existing = asyncio.get_running_loop().create_future()
        engine.jobs[existing] = (88, None, None)
        engine.dispatch()
        self.assertTrue(engine.merge_priority)
        engine.state.merge_held = True
        engine.dispatch()
        self.assertFalse(engine.merge_priority)
        self.assertIsNone(engine.plans.active_merge)
        self.assertFalse(existing.cancelled())
        engine.jobs.pop(existing); existing.set_result(None)
        await engine.once()
        self.assertIn("worker", [role for role, _, _ in rt.calls])

    async def test_draining_window_can_be_cancelled_without_stopping_sessions(self):
        engine, gh, rt = self.setup_engine()
        row = engine.plans.enqueue({"kind": "merge", "prs": [12]})
        existing = asyncio.get_running_loop().create_future()
        engine.jobs[existing] = (88, None, None)
        engine.dispatch()
        engine.plans.cancel(row["id"])
        engine.dispatch()
        self.assertFalse(engine.merge_priority)
        self.assertIsNone(engine.round_job)
        self.assertFalse(existing.cancelled())
        engine.jobs.pop(existing); existing.set_result(None)
        await engine.once()

    async def test_cancel_prevents_execution_and_retains_history(self):
        engine, gh, rt = self.setup_engine()
        row = engine.plans.enqueue({"kind": "merge", "prs": [12]})
        engine.plans.cancel(row["id"])
        await engine.once()
        self.assertEqual(engine.plans.rows[0]["state"], "cancelled")
        self.assertNotIn("merger", [role for role, _, _ in rt.calls])

    async def test_restart_blocks_unfinished_merge_without_restarting_it(self):
        engine, gh, rt = self.setup_engine()
        engine.plans.enqueue({"kind": "merge", "prs": [12]})
        engine.plans.update(engine.plans.rows[0], state="running")
        restored = Engine(engine.ctx, engine.state_dir)
        self.assertEqual(restored.plans.rows[0]["state"], "blocked")
        self.assertIsNone(restored.round_job)

    async def test_restart_recovers_completed_merge_result(self):
        engine, gh, rt = self.setup_engine()
        row = engine.plans.enqueue({"kind": "merge", "prs": [12]})
        engine.plans.rows[0]["state"] = "running"
        engine.state.merge_last = {"request_id": row["id"], "result": "landed", "pr": 91, "merged": [12]}
        engine.plans.save()
        restored = Engine(engine.ctx, engine.state_dir)
        self.assertEqual(restored.plans.rows[0]["state"], "complete")

    async def test_validation_rejects_ambiguous_times_and_sources(self):
        engine, gh, rt = self.setup_engine()
        for data in [{"kind": "merge", "not_before": "2026-11-05T10:00"},
                     {"kind": "merge", "prs": [999]},
                     {"kind": "package", "version": "bad", "target": "a" * 40},
                     {"kind": "package", "version": "0.3.7", "target": "main"},
                     {"kind": "package", "version": "0.3.7", "target": "a" * 40, "after": "unknown"}]:
            with self.subTest(data=data), self.assertRaises(ValueError):
                engine.plans.enqueue(data)
        self.assertEqual(engine.plans.rows, [])

    async def test_due_package_precedes_new_merge_without_reserving_workers(self):
        engine, gh, rt = self.setup_engine()
        engine.plans.enqueue({"kind": "merge", "prs": [12]})
        self.package(engine)
        engine._merge_round(False)
        self.assertIsNone(engine.round_job)
        self.assertFalse(engine.merge_priority)

    async def test_package_dispatch_is_idempotent_across_restart(self):
        engine, gh, rt = self.setup_engine()
        row = self.package(engine)
        state = {"schemaVersion": 1, "integrationMode": "MergeToStagingThenValidate", "work": [], "requests": []}
        self.controller(gh, state)
        engine.plans.package_step()
        post = [c for c in gh.api.call_args_list if len(c.args) > 1 and c.args[1] == "POST"]
        self.assertEqual(len(post), 1)
        self.assertEqual(post[0].args[2]["inputs"]["request_id"], row["id"])
        self.assertEqual(json.loads(post[0].args[2]["inputs"]["details"])["target"], "a" * 40)
        restored = Engine(engine.ctx, engine.state_dir)
        restored.plans.package_step()
        self.assertEqual(len([c for c in gh.api.call_args_list if len(c.args) > 1 and c.args[1] == "POST"]), 1)
        with self.assertRaises(ValueError):
            restored.plans.cancel(row["id"])

    async def test_dispatch_timeout_does_not_resend_or_mark_complete(self):
        engine, gh, rt = self.setup_engine()
        self.package(engine)
        state = {"schemaVersion": 1, "integrationMode": "MergeToStagingThenValidate", "work": [], "requests": []}
        self.controller(gh, state)
        underlying = gh.api.side_effect
        gh.api.side_effect = lambda path, method="GET", body=None: (_ for _ in ()).throw(RuntimeError("timeout")) if method == "POST" else underlying(path, method, body)
        engine.plans.package_step(); engine.plans.package_step()
        self.assertEqual(engine.plans.rows[0]["state"], "submitted")
        self.assertEqual(len([c for c in gh.api.call_args_list if len(c.args) > 1 and c.args[1] == "POST"]), 1)

    async def test_package_receipt_requires_full_verified_artifact_and_matching_identity(self):
        for variant in ("success", "unverified", "wrong-target", "wrong-version", "missing-tests"):
            engine, gh, rt = self.setup_engine()
            row = self.package(engine)
            package = {"result": "success", "artifactVerified": True, "source": "c" * 40, "version": "0.3.7", "path": "build/Windows"}
            receipt = {"requestId": row["id"], "target": "a" * 40, "version": "0.3.7", "output": "c" * 40,
                       "status": "verified", "testSet": "Full", "package": package}
            if variant == "unverified": package["artifactVerified"] = False
            if variant == "wrong-target": receipt["target"] = "d" * 40
            if variant == "wrong-version": package["version"] = "0.1.0"
            if variant == "missing-tests": receipt["testSet"] = "Fast"
            self.controller(gh, {"schemaVersion": 1, "requests": [receipt]})
            engine.plans.package_step()
            self.assertEqual(engine.plans.rows[0]["state"] == "complete", variant == "success")
            self.assertFalse(any(len(c.args) > 1 and c.args[1] == "POST" for c in gh.api.call_args_list))

    async def test_package_waits_for_dependency_and_uses_landed_commit_not_branch_tip(self):
        engine, gh, rt = self.setup_engine()
        parent = engine.plans.enqueue({"kind": "merge", "prs": [12]})
        self.package(engine, target="", after=parent["id"])
        gh.api = Mock(side_effect=AssertionError("No remote reads before dependency completes"))
        engine.plans.package_step(); gh.api.assert_not_called()
        engine.plans.finish_merge(parent["id"], {"result": "landed", "pr": 91, "merged": [12]})
        self.controller(gh, {"schemaVersion": 1, "integrationMode": "MergeToStagingThenValidate", "work": []})
        underlying = gh.api.side_effect
        gh.api.side_effect = lambda path, method="GET", body=None: {"merged": True, "base": {"ref": "Test-Staging"}, "merge_commit_sha": "e" * 40} if path.endswith("/pulls/91") else underlying(path, method, body)
        engine.plans.package_step()
        self.assertEqual(engine.plans.rows[1]["target"], "e" * 40)

    async def test_partial_merge_cannot_satisfy_package_dependency(self):
        engine, gh, rt = self.setup_engine()
        parent = engine.plans.enqueue({"kind": "merge", "prs": [12]})
        engine.plans.finish_merge(parent["id"], {"result": "landed", "pr": 91, "left_out": [{"pr": 12}]})
        self.assertEqual(engine.plans.rows[0]["state"], "blocked")

    async def test_controller_rejection_after_submission_is_blocked_without_resend(self):
        engine, gh, rt = self.setup_engine()
        row = self.package(engine)
        engine.plans.update(engine.plans.rows[0], dispatch_started="now", state="submitted")
        self.controller(gh, {"schemaVersion": 1, "controllerRequests": [{"requestId": row["id"], "status": "rejected", "message": "state revision changed"}]})
        engine.plans.package_step()
        self.assertEqual(engine.plans.rows[0]["state"], "blocked")
        self.assertFalse(any(len(c.args) > 1 and c.args[1] == "POST" for c in gh.api.call_args_list))

    async def test_live_http_controls_require_token_and_persist_modes_and_limits(self):
        from urllib.request import Request, urlopen
        from urllib.error import HTTPError
        from server import Dashboard
        from agents import AgentPool, AgentSpec
        engine, gh, rt = self.setup_engine()
        engine.ctx.agents = AgentPool([AgentSpec("fixture", kind="fake", tier=3)], {}, lambda _: rt)
        dash = Dashboard(engine, 0)
        dash.start(); dash.port = dash.server.server_address[1]
        self.addCleanup(dash.server.server_close); self.addCleanup(dash.stop)
        base = f"http://127.0.0.1:{dash.port}"

        def post(path, data, token=dash.token):
            request = Request(base + path, data=json.dumps(data).encode(), method="POST",
                              headers={"Content-Type": "application/json", "X-Fletchery-Token": token})
            with urlopen(request, timeout=3) as response:
                return json.load(response)

        with self.assertRaises(HTTPError) as denied:
            post("/api/queue/mode", {"planned": True}, "invalid")
        self.assertEqual(denied.exception.code, 403)
        post("/api/queue/mode", {"planned": True})
        row = post("/api/queue/add", {"kind": "merge", "prs": [12]})
        self.assertTrue(State.load(engine.state_dir / "state.json").planned_integration)
        post("/api/queue/cancel", {"id": row["id"]})
        post("/api/queue/mode", {"planned": False})
        post("/api/workers/ranks", {"limits": {"low": 7, "medium": 2, "high": 3}})
        self.assertEqual(engine.cfg.max_parallel, 12)
        self.assertEqual(engine.cfg.worker_rank_limits["low"], 7)
        with self.assertRaises(HTTPError) as bad:
            post("/api/workers/ranks", {"limits": {"low": 11, "medium": 0, "high": 0}})
        self.assertEqual(bad.exception.code, 400)
        post("/api/workers", {"max_parallel": 15})
        self.assertEqual(engine.cfg.max_parallel, 15)
        self.assertIsNone(engine.cfg.worker_rank_limits)


if __name__ == "__main__":
    unittest.main()
