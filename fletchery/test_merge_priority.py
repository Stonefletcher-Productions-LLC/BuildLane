"""Merge capacity takes precedence over new work; existing sessions are preserved."""
import asyncio
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import test_dispatch_resilience as fixtures
from agents import AgentPool, AgentSpec
from github import PullRequest, Feedback
from graph import Task, PR, BLOCKED, RUNNING, label_for
from loop import Engine, now
from pipeline import Outcome
import rounds


class MergePriorityTests(unittest.IsolatedAsyncioTestCase):
    engine = fixtures.DispatchResilienceTests.engine

    def ready_pr(self, number=12, body=""):
        return PullRequest(number, "Ready change", "ready-branch", "Test-Staging", "a" * 40, ["ready"], body)

    async def test_reservation_blocks_new_workers_fixers_and_planners_without_stopping_existing_job(self):
        queued = Task(1, "Queued work")
        blocked = Task(2, "Needs a plan", labels=[label_for(BLOCKED)])
        repair = Task(3, "Needs a fix", labels=[label_for(PR)])
        active = Task(992, "Current hair validation", labels=[label_for(RUNNING)])
        engine, gh, rt = self.engine({1: queued, 2: blocked, 3: repair, 992: active}, merge_rounds=True)
        engine.cfg.auto_prerequisites = True
        gh.prs = [self.ready_pr(), PullRequest(13, "Repair", repair.branch, "Test-Staging", "b" * 40, [], "Closes #3")]
        gh.feedback[13] = [Feedback(13, "comment", "Fix a concrete defect", "2026-11-04T00:00:00Z")]
        existing = asyncio.get_running_loop().create_future()
        engine.jobs[existing] = (992, None, None)
        engine.active.add(992)
        lease = Path(engine.cfg.project_root) / "heavy.json"
        lease.write_text(json.dumps({"token": "existing-job", "pid": 992}))
        engine.cfg.heavy_lease_file = str(lease)
        engine.dispatch()
        self.assertTrue(engine.merge_priority)
        self.assertEqual(list(engine.jobs), [existing])
        self.assertFalse(existing.cancelled())
        self.assertEqual(engine.state.fix_attempts, {})
        self.assertEqual(engine.state.blocker_checked, {})
        self.assertIn("merge priority", engine._waiting[1])
        self.assertIn("lease is held", engine.merge_why)
        self.assertEqual(rt.calls, [])
        lease.unlink()
        engine.dispatch()
        self.assertIsNotNone(engine.round_job)
        self.assertEqual(list(engine.jobs), [existing])
        await engine.round_job
        self.assertEqual([role for role, _, _ in rt.calls], ["merger"])
        engine.round_job, engine.round_info = None, {}
        existing.set_result(Outcome(992, "blocked"))
        engine._settle([existing])
        gh.close_issue(992)
        engine.cfg.merge_rounds = False
        engine.cfg.auto_prerequisites = False
        await engine.once()
        self.assertIn(("worker", 1), [(role, task) for role, task, _ in rt.calls])
        self.assertIn(("fixer", 3), [(role, task) for role, task, _ in rt.calls])

    async def test_merger_gets_first_opportunity_with_one_agent_slot(self):
        engine, gh, rt = self.engine({1: Task(1, "Worker")}, merge_rounds=True)
        pool = AgentPool([AgentSpec("native", kind="fake", tier=3, max_parallel=1)], {}, lambda _: rt)
        engine.ctx.agents, engine.ctx.gh = pool, gh
        engine = Engine(engine.ctx, engine.state_dir)
        engine.check_accounts = lambda force=False: None
        gh.prs = [self.ready_pr()]
        await engine.once()
        self.assertEqual([role for role, _, _ in rt.calls], ["merger"])
        self.assertEqual(pool.busy.get("native"), 0)
        engine.cfg.merge_rounds = False
        await engine.once()
        self.assertIn("worker", [role for role, _, _ in rt.calls])

    async def test_ineligible_merge_does_not_starve_workers(self):
        for state in ("off", "paused", "empty", "cooldown", "open_dependency"):
            with self.subTest(state=state):
                engine, gh, rt = self.engine({1: Task(1, "Independent worker")}, merge_rounds=True)
                pr = self.ready_pr()
                gh.prs = [pr]
                if state == "off": engine.cfg.merge_rounds = False
                if state == "paused": engine.state.merge_held = True
                if state == "empty": gh.prs = []
                if state == "cooldown":
                    engine.state.merge_last = {"result": "failed", "key": rounds.key_of([pr]), "at": now()}
                if state == "open_dependency":
                    pr.body = "Depends on: #90"
                    gh.closed.clear()
                await engine.once()
                self.assertFalse(engine.merge_priority)
                self.assertIn("worker", [role for role, _, _ in rt.calls])
                self.assertNotIn("merger", [role for role, _, _ in rt.calls])

    async def test_busy_merger_reserves_next_capacity_over_other_worker_provider(self):
        engine, gh, rt = self.engine({1: Task(1, "Docs task", labels=["difficulty:easy"])}, merge_rounds=True)
        pool = AgentPool([AgentSpec("native", kind="fake", tier=3, max_parallel=1),
                          AgentSpec("writer", kind="fake", tier=1, roles=["worker", "reviewer"])],
                         {"mode": "lists", "easy": ["writer"]}, lambda _: rt)
        pool.acquire("native")
        engine.ctx.agents, engine.ctx.gh = pool, gh
        engine = Engine(engine.ctx, engine.state_dir)
        engine.check_accounts = lambda force=False: None
        gh.prs = [self.ready_pr()]
        engine.dispatch()
        self.assertTrue(engine.merge_priority)
        self.assertFalse(engine.jobs)
        pool.release("native")
        await engine.once()
        self.assertEqual([role for role, _, _ in rt.calls], ["merger"])

    async def test_unavailable_merger_does_not_pause_other_providers(self):
        for state in ("off", "signed_out", "rate_limited"):
            with self.subTest(state=state):
                engine, gh, rt = self.engine({1: Task(1, "Docs task", labels=["difficulty:easy"])}, merge_rounds=True)
                pool = AgentPool([AgentSpec("native", kind="fake", tier=3),
                                  AgentSpec("writer", kind="fake", tier=1, roles=["worker", "reviewer"])],
                                 {"mode": "lists", "easy": ["writer"]}, lambda _: rt)
                engine.ctx.agents, engine.ctx.gh = pool, gh
                engine = Engine(engine.ctx, engine.state_dir)
                engine.check_accounts = lambda force=False: None
                if state == "off": pool.set_enabled("native", False)
                if state == "signed_out": pool.accounts["native"] = {"ok": False}
                if state == "rate_limited": pool.pause("native", 60, "fixture", shared=False)
                gh.prs = [self.ready_pr()]
                await engine.once()
                self.assertFalse(engine.merge_priority)
                self.assertIn("worker", [role for role, _, _ in rt.calls])
                self.assertNotIn("merger", [role for role, _, _ in rt.calls])

    async def test_existing_round_keeps_priority_even_if_future_rounds_disabled(self):
        engine, gh, rt = self.engine({1: Task(1, "Queued worker")}, merge_rounds=False)
        current = asyncio.get_running_loop().create_future()
        engine.round_job = current
        try:
            engine.dispatch()
            self.assertTrue(engine.merge_priority)
            self.assertFalse(engine.jobs)
            self.assertFalse(current.cancelled())
        finally:
            current.set_result({})
            engine.round_job = None

    async def test_completed_round_timestamp_starts_cooldown_at_end(self):
        engine, gh, rt = self.engine(merge_rounds=True)
        pr = self.ready_pr()
        rt.script[("merger", 0)] = {"state": "failed", "summary": "fixture failure"}
        start, end = "2026-11-04T19:00:00+00:00", "2026-11-04T22:30:00+00:00"
        with patch("loop.now", side_effect=[start, end]):
            result = await engine._run_round(1, [pr], "", rounds.key_of([pr]), str(engine.state_dir / "round.json"))
        self.assertEqual(result["started"], start)
        self.assertEqual(result["ended"], end)
        self.assertEqual(result["at"], end)
        from datetime import datetime
        allowed, reason = rounds.should_start(engine.cfg, result, False, False, False, [pr], datetime.fromisoformat(end))
        self.assertFalse(allowed)
        self.assertIn("last round didn't land", reason)
        saved = rounds.read_file(str(engine.state_dir / "round.json"))
        self.assertEqual(saved["ended"], saved["updated"])
        self.assertEqual(saved["result"], "failed")

    async def test_unbatchable_dependency_cycle_does_not_hold_unrelated_workers(self):
        engine, gh, rt = self.engine({1: Task(1, "Independent worker")}, merge_rounds=True, merge_round_max=1)
        gh.prs = [self.ready_pr(12, "Depends on: #13"), self.ready_pr(13, "Depends on: #12")]
        await engine.once()
        self.assertFalse(engine.merge_priority)
        self.assertIn("worker", [role for role, _, _ in rt.calls])
        self.assertNotIn("merger", [role for role, _, _ in rt.calls])


if __name__ == "__main__":
    unittest.main()
