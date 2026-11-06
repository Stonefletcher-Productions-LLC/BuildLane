"""Token accounting, plan-then-implement, skill routing (Blender & 3D to Codex), daily caps, cheap reviewers, and the
waste guards from the 2026-11-06 efficiency review; no agents, no network."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest

import agents
import blockers
import pipeline
import scores
import usage
from agents import AgentPool, AgentSpec, plan_of, strip_plan, with_plan
from clis import CodexRuntime, OpenCodeRuntime
from config import Config
from github import FakeGitHub
from graph import Task
from pipeline import Context, Outcome, environmental, run_task
from repo import FakeRepo
from runtime import FakeRuntime, RunResult


HARD_BODY = "Fix the save format migration for multiplayer RPC state.\n"


def pool_of(specs, routing=None, runtimes=None):
    runtimes = runtimes if runtimes is not None else {}
    return AgentPool(specs, routing or {"mode": "scores"}, lambda s: runtimes.setdefault(s.name, FakeRuntime()))


class TokenTests(unittest.TestCase):
    def test_codex_and_opencode_events_add_up_tokens(self):
        state = {"turns": 0, "errors": [], "last_text": "", "session": ""}
        codex = CodexRuntime(AgentSpec("codex", kind="codex"))
        for _ in range(2):
            codex.on_event({"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 40,
                                                                "output_tokens": 7}}, state, ".", None)
        self.assertEqual(state["tokens"], {"input": 200, "cache_read": 80, "output": 14})
        state = {"turns": 0, "errors": [], "last_text": "", "session": "", "cost": 0.0}
        oc = OpenCodeRuntime(AgentSpec("oc", kind="opencode"))
        oc.on_event({"type": "step_finish", "part": {"cost": 0.01, "tokens": {"input": 50, "output": 5, "reasoning": 3,
                                                                              "cache": {"read": 20, "write": 2}}}},
                    state, ".", None)
        self.assertEqual(state["tokens"], {"input": 50, "output": 8, "cache_read": 20, "cache_write": 2})

    def test_usage_log_summary_and_daily_counts(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "usage.jsonl"
            pool = pool_of([AgentSpec("codex", kind="codex"), AgentSpec("sonnet", kind="claude")])
            for agent, role, tokens in (("codex", "worker", {"input": 1000, "output": 100, "cache_read": 10000}),
                                        ("codex", "reviewer", {"input": 500}), ("sonnet", "architect", {"input": 50})):
                r = RunResult(role, True, tokens=tokens)
                r.agent, r.seconds = agent, 60
                usage.record(path, r, 7, pool)
            s = usage.summary(path)
            self.assertEqual([a["agent"] for a in s["agents"]], ["codex", "sonnet"])
            self.assertEqual(s["agents"][0]["weighted"], 1000 + 100 + 1000 + 500)
            self.assertEqual(s["agents"][0]["account"], "codex")
            self.assertEqual(usage.runs_today(path), {"codex": 2, "sonnet": 1})
            self.assertIn("codex (codex)", usage.text(s))


class RoutingTests(unittest.TestCase):
    def test_blender_work_is_art_and_prefers_codex_when_free(self):
        skill, _ = scores.classify_skill("Hair 2/4: in-house box braids", "Rebuild the mesh in Blender and re-export the FBX.", [])
        self.assertEqual(skill, "art")
        pool = pool_of([AgentSpec("codex", kind="codex", scores={"art": 50, "cost": 40}),
                        AgentSpec("sonnet", kind="claude", scores={"art": 80, "cost": 60})])
        self.assertEqual(pool.choose("Hair 2/4: in-house box braids", "Blender mesh work.", []).agent, "codex")
        pool.pause("codex", 60, "usage limit")
        self.assertEqual(pool.choose("Hair 2/4: in-house box braids", "Blender mesh work.", []).agent, "sonnet")

    def test_daily_cap_takes_an_agent_out_until_tomorrow(self):
        pool = pool_of([AgentSpec("fable", kind="claude", max_runs_per_day=2)])
        self.assertTrue(pool.available("fable", "architect"))
        pool.note_run("fable")
        pool.seed_runs({"fable": 2})
        self.assertFalse(pool.available("fable", "architect"))
        pool.runs_day = "2000-01-01"  # a new day resets the count
        self.assertTrue(pool.available("fable", "architect"))
        self.assertEqual(agents.specs_from([{"name": "f", "maxRunsPerDay": 3}])[0].max_runs_per_day, 3)

    def test_architect_role_follows_planner_rights(self):
        self.assertTrue(AgentSpec("a", roles=["planner"]).can("architect"))
        self.assertTrue(AgentSpec("a", roles=["architect"]).can("architect"))
        self.assertFalse(AgentSpec("a", roles=["worker"]).can("architect"))
        self.assertFalse(AgentSpec("fable", model="claude-fable-5-1", roles=["architect"]).can("worker"))

    def test_architect_choice_list_then_strongest(self):
        specs = [AgentSpec("opus", kind="claude", scores={"cpp": 85}), AgentSpec("flash", kind="opencode", scores={"cpp": 50}),
                 AgentSpec("pro", kind="opencode", scores={"cpp": 75})]
        pool = pool_of(specs, {"mode": "scores", "plan": {"architects": ["pro", "opus"]}})
        self.assertEqual(pool.architect("cpp"), "pro")
        pool.pause("pro", 60, "limit", shared=False)
        self.assertEqual(pool.architect("cpp"), "opus")
        pool = pool_of(specs)
        self.assertEqual(pool.architect("cpp"), "opus")
        pool.pause("opus", 60, "limit")
        self.assertEqual(pool.architect("cpp"), "pro")
        pool.pause("pro", 60, "limit", shared=False)
        self.assertIsNone(pool.architect("cpp"), "flash is under the architect minScore")

    def test_plan_round_trip_and_planned_hard_task_is_written_as_medium(self):
        body = with_plan(HARD_BODY, "1. Do it.\n2. Test it.", "opus")
        self.assertEqual(plan_of(body)["by"], "opus")
        self.assertIn("1. Do it.", plan_of(body)["text"])
        self.assertEqual(strip_plan(body), HARD_BODY.strip())
        self.assertEqual(strip_plan(with_plan(body, "new", "pro")).count("fletchery-plan"), 0)
        specs = [AgentSpec("opus", kind="claude", scores={"cpp": 85, "cost": 20}),
                 AgentSpec("flash", kind="opencode", scores={"cpp": 62, "cost": 90})]
        pool = pool_of(specs)
        self.assertEqual(pool.choose("Saves", HARD_BODY, []).agent, "opus")
        c = pool.choose("Saves", body, [])
        self.assertEqual((c.agent, c.difficulty), ("flash", "medium"))
        self.assertIn("planned by opus", c.why)

    def test_cheap_reviewer_for_medium_best_for_hard(self):
        specs = [AgentSpec("writer", kind="opencode", vendor="deepseek", scores={"review": 60, "cost": 90}),
                 AgentSpec("cheap", kind="codex", vendor="openai", scores={"review": 60, "cost": 80}),
                 AgentSpec("best", kind="claude", vendor="anthropic", scores={"review": 90, "cost": 20})]
        pool = pool_of(specs)
        self.assertEqual(pool.reviewer_for("writer", level="medium"), "cheap")
        self.assertEqual(pool.reviewer_for("writer", level="hard"), "best")
        self.assertEqual(pool.reviewer_for("writer"), "best")

    def test_setup_failures_are_no_verdict_on_the_agent(self):
        res = scores.results([{"agent": "codex", "role": "worker", "ok": False, "env": True, "skill": "art"},
                              {"agent": "codex", "role": "worker", "ok": False, "cant_run": True, "skill": "art"},
                              {"agent": "opus", "role": "architect", "ok": True, "skill": "cpp"}])
        self.assertEqual(res, {})
        self.assertTrue(environmental("worker gave up: This issue requires a branch and PR in Hearth, but only a "
                                      "LifeShare worktree is supplied"))
        self.assertFalse(environmental("worker gave up: the exported hair still tiles badly"))
        self.assertFalse(environmental("gates still fail after one fix pass: build"))


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    def ctx(self, tasks, specs, routing=None, scripts=None):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        runtimes = {n: FakeRuntime(s) for n, s in (scripts or {}).items()}
        pool = pool_of(specs, routing, runtimes)
        gh = FakeGitHub(tasks)
        ctx = Context(Config(project_root=str(root), base_branch="Test-Staging", auto_prerequisites=False), gh,
                      FakeRepo(root / "worktrees"), FakeRuntime(), run_gates=lambda *_, **__: [], log=lambda _: None,
                      agents=pool, usage_log=root / "usage.jsonl")
        return ctx, gh, runtimes

    async def test_hard_task_is_planned_once_then_written_by_the_cheap_agent(self):
        task = Task(5, "Saves: migrate the format", HARD_BODY)
        specs = [AgentSpec("opus", kind="claude", scores={"cpp": 85, "review": 85, "cost": 20}),
                 AgentSpec("flash", kind="opencode", vendor="deepseek", scores={"cpp": 62, "review": 40, "cost": 90})]
        plan = {"plan": "**Approach**: change LSSave.cpp only.", "files": ["Source/LSSave.cpp"], "difficulty": "medium"}
        ctx, gh, rts = self.ctx({5: task}, specs, scripts={"opus": {("architect", 0): plan}, "flash": {}})
        out = await run_task(task, ctx)
        self.assertEqual(out.result, "pr")
        self.assertEqual(out.agent, "flash")
        self.assertIn("fletchery-plan by=opus", gh.tasks[5].body)
        worker_prompt = next(p for role, _, p in rts["flash"].calls if role == "worker")
        self.assertIn("change LSSave.cpp only", worker_prompt)
        self.assertEqual([r for r, _, _ in rts["opus"].calls], ["architect", "reviewer"])
        rows = usage.read(ctx.usage_log)
        self.assertEqual([(r["agent"], r["role"]) for r in rows],
                         [("opus", "architect"), ("flash", "worker"), ("opus", "reviewer")])
        # a second run of the same issue reuses the saved plan: no new architect session
        again = Task(5, task.title, gh.tasks[5].body)
        await run_task(again, ctx)
        self.assertEqual([r for r, _, _ in rts["opus"].calls].count("architect"), 1)

    async def test_architect_failure_means_no_plan_not_no_work(self):
        task = Task(6, "Saves: migrate", HARD_BODY)
        specs = [AgentSpec("opus", kind="claude", scores={"cpp": 85, "cost": 20}),
                 AgentSpec("flash", kind="opencode", vendor="deepseek", scores={"cpp": 62, "cost": 90})]
        ctx, gh, rts = self.ctx({6: task}, specs, scripts={"opus": {("architect", 0): {"error": "crashed"}}})
        out = await run_task(task, ctx)
        self.assertEqual(out.result, "pr")
        self.assertEqual(out.agent, "opus", "unplanned, the hard task goes to the strong writer as before")
        self.assertNotIn("fletchery-plan", gh.tasks[6].body)

    async def test_setup_blocker_is_not_escalated(self):
        task = Task(7, "Hearth: woodland pieces", "Add oak and alder to Hearth.\nDifficulty: medium")
        gave_up = {"summary": "", "files": [], "build_steps": [], "checks": [], "unverified": [],
                   "gave_up": "This issue requires a branch and PR in Hearth, but only a LifeShare worktree is supplied."}
        specs = [AgentSpec("flash", kind="opencode", vendor="deepseek", scores={"cpp": 62, "art": 62, "cost": 90}),
                 AgentSpec("opus", kind="claude", scores={"cpp": 85, "art": 85, "cost": 20})]
        ctx, gh, rts = self.ctx({7: task}, specs, routing={"mode": "scores", "preferredKindsBySkill": {}},
                                scripts={"flash": {("worker", 0): gave_up}, "opus": {("worker", 0): gave_up}})
        out = await run_task(task, ctx)
        self.assertEqual(out.result, "blocked")
        self.assertEqual(len([a for a in out.attempts if a["role"] == "worker"]), 1)
        self.assertTrue(out.attempts[0].get("env"))
        self.assertEqual(rts["opus"].calls, [], "nobody else was paid to hit the same wall")

    async def test_escalation_stops_after_max_escalations(self):
        task = Task(8, "Plain change", "Change the thing.\nDifficulty: medium")
        bad = {"summary": "", "files": [], "build_steps": [], "checks": [], "unverified": [], "gave_up": "the code is tangled"}
        specs = [AgentSpec(n, kind="fake", vendor=n, scores={"cpp": s, "cost": 100 - s}) for n, s in
                 (("a", 55), ("b", 65), ("c", 75), ("d", 85))]
        ctx, gh, rts = self.ctx({8: task}, specs, routing={"mode": "scores", "maxEscalations": 1},
                                scripts={n: {("worker", 0): bad} for n in "abcd"})
        out = await run_task(task, ctx)
        self.assertEqual(out.result, "blocked")
        self.assertEqual([a["agent"] for a in out.attempts if a["role"] == "worker"], ["a", "b"])

    async def test_refused_model_is_paused_and_not_blamed(self):
        task = Task(9, "Plain change", "Change the thing.\nDifficulty: medium")
        specs = [AgentSpec("broken", kind="opencode", vendor="x", scores={"cpp": 70, "cost": 95}),
                 AgentSpec("good", kind="claude", scores={"cpp": 80, "review": 80, "cost": 20})]
        err = {"error": "opencode failed: {'type': 'provider.no-route', 'message': 'Model unavailable: opencode/x'}"}
        ctx, gh, rts = self.ctx({9: task}, specs, scripts={"broken": {("worker", 0): err}})
        out = await run_task(task, ctx)
        self.assertEqual(out.result, "pr")
        self.assertEqual(out.agent, "good")
        self.assertTrue(out.attempts[0].get("cant_run"))
        self.assertIn("broken", ctx.agents.paused)


class BlockerLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_waiting_on_finished_work_asks_a_person(self):
        import graph
        task = Task(944, "Willow Street kerb", "Fix the kerb z-fighting.\n\nDepends on: #42", [graph.PREFIX, graph.label_for(graph.BLOCKED)])
        gh = FakeGitHub({944: task}, {42})
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        rt = FakeRuntime({("planner", 0): {"issues": [], "reuse": [42], "retry": "", "needs_human": ""}})
        ctx = Context(Config(project_root=str(root), base_branch="Test-Staging", auto_prerequisites=True), gh,
                      FakeRepo(root), rt, log=lambda _: None)
        gh.comment(944, "Fletchery blocked this task: kerb geometry lives elsewhere")
        out = await blockers.resolve(task, ctx, Outcome(944, "blocked"))
        self.assertEqual(out.result, "blocked")
        self.assertIn("already finished", out.detail)
        thread = gh.issue_thread(944)
        self.assertFalse(blockers.pending(thread), "the saved verdict stops the planner from looping")


if __name__ == "__main__":
    unittest.main()
