"""Reservation contention is scheduling, not a failed repair; no real services."""
import json
import unittest
from pathlib import Path
from unittest.mock import patch
import test_dispatch_resilience as fixtures
from graph import Task, PR, BLOCKED, label_for
from github import PullRequest, Feedback
from loop import Engine, State
from runtime import DEFAULT_ANSWERS


class ResourceWaitTests(unittest.IsolatedAsyncioTestCase):
    engine = fixtures.DispatchResilienceTests.engine
    def test_wait_guidance_is_loaded_with_schema_not_hot_read_roles(self):
        from runtime import role_prompt, SCHEMAS, ROLES_DIR
        for role in ("worker", "fixer"):
            self.assertIn("heavy", SCHEMAS[role]["properties"]["waiting_resource"]["enum"])
            self.assertIn('waiting_resource: "heavy"', role_prompt(role))
            self.assertNotIn("waiting_resource", (ROLES_DIR / (role + ".md")).read_text(encoding="utf-8"))

    def waiting_engine(self, fixer=False):
        task = Task(1, "Hair", labels=[label_for(PR)] if fixer else [])
        engine, gh, rt = self.engine({1: task})
        engine.ctx.run_gates = lambda *args, **kwargs: []
        lease = Path(engine.cfg.project_root) / "heavy.json"
        lease.write_text(json.dumps({"pid": 1234, "token": "fixture"}))
        engine.cfg.heavy_lease_file = str(lease)
        role = "fixer" if fixer else "worker"
        rt.script[(role, 1)] = {**DEFAULT_ANSWERS[role], "waiting_resource": "heavy"}
        if fixer:
            gh.prs = [PullRequest(12, "Hair", task.branch, "Test-Staging", "a" * 40, ["ready"], "Closes #1")]
            gh.feedback[12] = [Feedback(12, "comment", "Repair roots", "2026-11-04T00:00:00Z")]
        return engine, gh, rt, lease

    async def test_worker_wait_preserves_branch_and_resumes_after_release_and_restart(self):
        engine, gh, rt, lease = self.waiting_engine()
        original_branch = gh.tasks[1].branch
        with patch.object(engine.ctx.repo, "restart_task_worktree", wraps=engine.ctx.repo.restart_task_worktree) as reset:
            outcomes = await engine.once()
            self.assertEqual(outcomes[0].result, "waiting_resource")
            self.assertNotEqual(gh.tasks[1].status, BLOCKED)
            self.assertFalse(gh.comments)
            self.assertEqual(len(rt.calls), 1)
            engine = Engine(engine.ctx, engine.state_dir)
            self.assertIn("1", engine.state.resource_waits)
            for _ in range(4):
                await engine.once()
            self.assertEqual(len(rt.calls), 1)
            engine.state.resource_waits["1"] = "2000-01-01T00:00:00+00:00"
            await engine.once()
            self.assertEqual(len(rt.calls), 1)
            lease.unlink()
            rt.script.clear()
            results = await engine.once()
            self.assertEqual(results[0].result, "pr", results[0].detail)
            self.assertEqual(gh.tasks[1].branch, original_branch)
            reset.assert_not_called()

    async def test_fixer_wait_restores_feedback_and_attempt_budget(self):
        engine, gh, rt, lease = self.waiting_engine(True)
        for _ in range(4):
            await engine.once()
        self.assertEqual(engine.state.fix_attempts.get("12"), 0)
        self.assertNotIn("12", engine.state.seen)
        self.assertEqual(len(rt.calls), 1)
        engine.state.resource_waits["1"] = "2000-01-01T00:00:00+00:00"
        lease.unlink()
        rt.script.clear()
        outcomes = await engine.once()
        self.assertEqual(outcomes[0].result, "fixed", outcomes[0].detail)
        self.assertEqual(engine.state.fix_attempts["12"], 1)

    async def test_busy_lease_at_checkout_waits_instead_of_blocking(self):
        # LifeShare 2026-11-08: a round held the lease without naming workspaces; nine tasks went to Needs you.
        from repo import LeaseBusy
        engine, gh, rt, lease = self.waiting_engine()
        rt.script.clear()
        busy = LeaseBusy("PC lease ownership is unknown; task checkout is preserved")
        with patch.object(engine.ctx.repo, "ensure_task_checkout", side_effect=busy):
            outcomes = await engine.once()
        self.assertEqual(outcomes[0].result, "waiting_resource", outcomes[0].detail)
        self.assertNotEqual(gh.tasks[1].status, BLOCKED)
        self.assertFalse(gh.comments)
        self.assertEqual(len(rt.calls), 0)
        self.assertIn("1", engine.state.resource_waits)
        engine.state.resource_waits["1"] = "2000-01-01T00:00:00+00:00"
        lease.unlink()
        self.assertEqual((await engine.once())[0].result, "pr")

    async def test_busy_lease_at_repair_checkout_gives_the_attempt_back(self):
        from repo import LeaseBusy
        engine, gh, rt, lease = self.waiting_engine(True)
        rt.script.clear()
        busy = LeaseBusy("PC lease ownership is unknown; task checkout is preserved")
        with patch.object(engine.ctx.repo, "ensure_repair_checkout", side_effect=busy):
            outcomes = await engine.once()
        self.assertEqual(outcomes[0].result, "waiting_resource", outcomes[0].detail)
        self.assertEqual(engine.state.fix_attempts.get("12"), 0)
        self.assertNotIn("12", engine.state.seen)
        engine.state.resource_waits["1"] = "2000-01-01T00:00:00+00:00"
        lease.unlink()
        self.assertEqual((await engine.once())[0].result, "fixed")

    async def test_worker_without_commits_on_a_merged_branch_closes_out(self):
        # LifeShare #1284, 2026-11-08: the call passed (ctx, task) the wrong way round and crashed every such task.
        engine, gh, rt, lease = self.waiting_engine()
        lease.unlink()
        rt.script.clear()
        engine.ctx.repo.changed[1] = []
        gh.merged = {gh.tasks[1].branch: (77, "Test-Staging", "2026-11-08T00:00:00Z")}
        outcomes = await engine.once()
        self.assertEqual(outcomes[0].result, "pr", outcomes[0].detail)
        self.assertEqual(outcomes[0].pr_number, 77)

    async def test_permission_denial_has_priority_over_wait(self):
        engine, gh, rt, lease = self.waiting_engine()
        rt.script[("worker", 1)]["gave_up"] = "MCP execution permission denied"
        outcomes = await engine.once()
        self.assertEqual(outcomes[0].result, "blocked")
        self.assertFalse(engine.state.resource_waits)
        self.assertIn("permission denied", outcomes[0].detail)

    async def test_unconfigured_or_unknown_resource_blocks(self):
        for resource in ("heavy", "unknown"):
            engine, gh, rt, lease = self.waiting_engine()
            rt.script[("worker", 1)]["waiting_resource"] = resource
            engine.cfg.heavy_lease_file = ""
            outcomes = await engine.once()
            self.assertEqual(outcomes[0].result, "blocked")
            self.assertFalse(engine.state.resource_waits)

    async def test_ci_lease_and_release_before_result_defer_without_repair(self):
        for content in (None, '{"runId":"123", "attempt":"1", "schemaVersion":1}', "partial"):
            engine, gh, rt, lease = self.waiting_engine()
            if content is None:
                lease.unlink()
            else:
                lease.write_text(content)
            outcomes = await engine.once()
            self.assertEqual(outcomes[0].result, "waiting_resource")
            await engine.once()
            self.assertEqual(len(rt.calls), 1)
            engine.state.resource_waits["1"] = "2000-01-01T00:00:00+00:00"
            if content is not None:
                await engine.once()
                self.assertEqual(len(rt.calls), 1)
                lease.unlink()
            rt.script.clear()
            self.assertEqual((await engine.once())[0].result, "pr")

    async def test_changed_or_malformed_lease_never_releases_wait(self):
        engine, gh, rt, lease = self.waiting_engine()
        await engine.once()
        engine.state.resource_waits["1"] = "2000-01-01T00:00:00+00:00"
        lease.write_text("partial write")
        await engine.once()
        self.assertEqual(len(rt.calls), 1)
        self.assertEqual(lease.read_text(), "partial write")


if __name__ == "__main__":
    unittest.main()
