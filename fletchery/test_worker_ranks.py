import asyncio
import unittest
from unittest.mock import patch

from agents import AgentPool, AgentSpec
from graph import Task
from loop import Engine
from pipeline import run_task
import test_dispatch_resilience as fixtures
from worker_ranks import configure, rank


class RankTests(unittest.IsolatedAsyncioTestCase):
    engine = fixtures.DispatchResilienceTests.engine

    def setup_pool(self, total=3):
        engine, gh, rt = self.engine(max_parallel=total)
        skills = lambda v: {k: v for k in ("cpp", "scripts", "ui", "docs", "review")}
        pool = AgentPool([
            AgentSpec("low", kind="fake", tier=1, scores=skills(50), roles=["worker"], levels=["easy"]),
            AgentSpec("medium", kind="fake", tier=3, scores=skills(65)),
            AgentSpec("high", kind="fake", tier=4, scores=skills(85)),
        ], {"mode": "scores"}, lambda _: rt)
        engine.ctx.agents = pool
        engine = Engine(engine.ctx, engine.state_dir)
        engine.check_accounts = lambda force=False: None
        return engine, pool

    def task(self, n, level):
        return Task(n, "A task", f"Difficulty: {level}\nSkill: docs")

    async def test_rank_comes_from_baseline_not_routing_score(self):
        engine, pool = self.setup_pool()
        pool.specs["high"].scores = {k: 48 for k in ("cpp", "scripts", "ui", "docs")}
        view = configure(engine, [self.task(1, "easy")])
        self.assertEqual(view["agent_ranks"], {"low": "low", "medium": "medium", "high": "high"})
        self.assertEqual([rank(v) for v in [59, 60, 74, 75]], ["low", "medium", "medium", "high"])

    async def test_all_workers_allocates_by_current_tasks(self):
        engine, pool = self.setup_pool(total=6)
        view = configure(engine, [self.task(1, "easy"), self.task(2, "medium"), self.task(3, "hard")])
        self.assertEqual(sum(view["limits"].values()), 6)
        self.assertTrue(all(view["limits"][r] > 0 for r in ("low", "medium", "high")))
        self.assertEqual(view["ready_levels"], {"easy": 1, "medium": 1, "hard": 1})
        # A harder-only backlog must not reserve easy-only model capacity.
        view = configure(engine, [self.task(3, "hard")])
        self.assertEqual(view["limits"]["low"], 0)
        self.assertEqual(sum(view["limits"].values()), 6)

    async def test_manual_rank_limits_and_high_model_can_take_easy_work(self):
        engine, pool = self.setup_pool()
        engine.cfg.worker_rank_limits = {"low": 0, "medium": 0, "high": 2}
        configure(engine, [self.task(1, "easy")])
        choice = pool.choose("Small task", "Difficulty: easy\nSkill: docs", [], stats=engine.stats())
        self.assertEqual(choice.agent, "high")
        self.assertTrue(pool.reserve_worker(1, "high"))
        self.assertTrue(pool.reserve_worker(2, "high"))
        self.assertFalse(pool.reserve_worker(3, "high"))
        self.assertIsNone(pool.choose("Small task", "Difficulty: easy", []))
        pool.release_worker(1)
        self.assertIsNotNone(pool.choose("Small task", "Difficulty: easy", []))

    async def test_active_rank_is_pinned_when_baseline_changes(self):
        engine, pool = self.setup_pool()
        configure(engine, [self.task(1, "easy")])
        pool.reserve_worker(1, "low")
        pool.specs["low"].tier = 5
        view = configure(engine, [self.task(2, "easy")])
        self.assertEqual(view["assignments"]["1"], "low")
        self.assertEqual(view["agent_ranks"]["low"], "high")

    async def test_lowering_capacity_does_not_cancel_active_slots(self):
        engine, pool = self.setup_pool()
        configure(engine, [self.task(1, "easy")])
        pool.reserve_worker(1, "low")
        engine.cfg.max_parallel = 0
        view = configure(engine, [self.task(2, "easy")])
        self.assertEqual(sum(view["limits"].values()), 0)
        self.assertEqual(view["active"]["low"], 1)
        self.assertFalse(pool.reserve_worker(2, "low"))

    async def test_forced_model_and_easy_only_restriction_are_preserved(self):
        engine, pool = self.setup_pool()
        engine.cfg.worker_rank_limits = {"low": 3, "medium": 0, "high": 0}
        configure(engine, [self.task(1, "hard")])
        self.assertIsNone(pool.choose("Hard", "Difficulty: hard\nAgent: low", []))
        self.assertIsNone(pool.choose("Hard", "Difficulty: hard", []))

    async def test_rank_reservation_releases_after_worker_failure(self):
        engine, pool = self.setup_pool()
        engine.cfg.worker_rank_limits = {"low": 1, "medium": 0, "high": 0}
        configure(engine, [self.task(1, "easy")])
        task = self.task(1, "easy")
        engine.ctx.gh.tasks[1] = task
        with patch("pipeline._attempt", side_effect=RuntimeError("fixture worker failure")):
            with self.assertRaises(RuntimeError):
                from pipeline import _run_task_body, Outcome
                await _run_task_body(task, engine.ctx, Outcome(1, "blocked"))
        self.assertEqual(engine.state.task_worker_rank["1"], "low")
        self.assertEqual(pool.worker_slots, {})
        self.assertEqual(pool.busy.get("low", 0), 0)


if __name__ == "__main__":
    unittest.main()
