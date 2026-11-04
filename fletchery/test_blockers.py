"""Automatic prerequisites, ten slots, provider-neutral mergers and crash reporting; no network or agents."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import blockers
from agents import AgentPool, AgentSpec
from clis import CodexRuntime, OpenCodeRuntime
from config import Config
import graph
from github import FakeGitHub, PullRequest
from loop import Engine
from pipeline import Context, Outcome, run_task
from repo import FakeRepo
from rounds import report_failure
from runtime import FakeRuntime
from server import Dashboard


class BlockerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.task = graph.Task(1, "Finish feature", "Keep original scope.", [graph.PREFIX, graph.label_for(graph.BLOCKED)])
        self.gh = FakeGitHub({1: self.task})
        self.gh.comment(1, "Fletchery blocked this task: prerequisite helper is missing")
        self.plan = {"issues": [{"title": "Implement helper", "body": "Implement the missing helper and add a focused regression proving its contract.", "depends_on": []}],
                     "reuse": [], "needs_human": ""}
        self.ctx = Context(Config(project_root=str(self.root), daily_budget_usd=100), self.gh, FakeRepo(self.root),
                           FakeRuntime({("planner", 1): self.plan}), log=lambda _: None)

    def resolve(self):
        return asyncio.run(blockers.resolve(self.task, self.ctx, Outcome(1, "blocked")))

    def test_create_link_wait_then_ready(self):
        out = self.resolve()
        self.assertEqual(out.result, "waiting_dependencies")
        self.assertEqual(self.task.depends_on, [2])
        self.assertEqual(self.task.status, graph.QUEUED)
        self.assertIn(1, graph.readiness(self.gh.open_tasks()).waiting)
        self.gh.tasks[2].closed = True
        self.assertEqual([t.number for t in graph.readiness(self.gh.open_tasks(), {2}).ready], [1])
        self.assertEqual(len(self.ctx.runtime.calls), 1)

    def test_crash_after_creation_reuses_plan_and_child(self):
        with patch.object(self.gh, "edit_body", side_effect=RuntimeError("network")):
            self.assertEqual(self.resolve().result, "blocked")
        self.assertEqual(len(self.gh.tasks), 2)
        self.resolve()
        self.assertEqual(len(self.gh.tasks), 2)
        self.assertEqual(len(self.ctx.runtime.calls), 1)
        self.assertEqual(self.task.depends_on, [2])

    def test_human_answer_cached_until_user_comments(self):
        self.plan.update(issues=[], needs_human="Choose the intended door layout.")
        self.resolve()
        self.assertFalse(blockers.pending(self.gh.issue_thread(1)))
        self.resolve()
        self.assertEqual(len(self.ctx.runtime.calls), 1)
        self.gh.notes[1] = [("alex", "now", "Use the left door.")]
        self.assertTrue(blockers.pending(self.gh.issue_thread(1)))
        self.assertEqual(self.task.status, graph.BLOCKED)

    def test_no_recursive_child_expansion(self):
        self.task.body += "\n" + blockers.CHILD + "9:key:0 -->"
        self.resolve()
        self.assertEqual(self.ctx.runtime.calls, [])

    def test_disabled_and_zero_workers_do_not_plan_backlog(self):
        self.ctx.cfg.auto_prerequisites = False
        self.resolve()
        self.assertEqual(self.ctx.runtime.calls, [])
        self.ctx.cfg.auto_prerequisites = True
        self.ctx.cfg.max_parallel = 0
        asyncio.run(Engine(self.ctx, self.root / "zero").once())
        self.assertEqual(self.ctx.runtime.calls, [])

    def test_dry_run_does_not_create_or_plan(self):
        before = list(self.gh.writes)
        asyncio.run(Engine(self.ctx, self.root / "dry", dry_run=True).once())
        self.assertEqual(self.gh.writes, before)
        self.assertEqual(self.ctx.runtime.calls, [])

    def test_reuse_and_existing_dependencies_preserved(self):
        self.gh.tasks[3] = graph.Task(3, "Existing helper", labels=[graph.PREFIX])
        self.plan.update(issues=[], reuse=[3])
        self.task.body += "\nDepends on: #9"
        self.resolve()
        self.assertEqual(self.task.depends_on, [3, 9])

    def test_reject_cycle_and_oversized_or_nested_plan(self):
        self.gh.tasks[3] = graph.Task(3, "Dependent", "Depends on: #1")
        for plan in ({"issues": [], "reuse": [3]}, {"issues": self.plan["issues"] * 4},
                     {"issues": [dict(self.plan["issues"][0], depends_on=[1])]},
                     {"issues": [], "reuse": [999]}):
            with self.assertRaises(ValueError):
                blockers.validate(plan, 1, self.gh.tasks)

    def test_closed_child_not_recreated_after_partial_failure(self):
        with patch.object(self.gh, "edit_body", side_effect=RuntimeError("network")):
            self.resolve()
        self.gh.tasks[2].closed = True
        self.resolve()
        self.assertEqual(len(self.gh.tasks), 2)
        self.assertEqual(self.task.depends_on, [2])

    def test_existing_blocked_backlog_runs_in_slot_and_respects_pause(self):
        engine = Engine(self.ctx, self.root / "state")
        engine.check_accounts = lambda **_: None
        engine.state.held = True
        asyncio.run(engine.once())
        self.assertEqual(self.ctx.runtime.calls, [])
        engine.state.held = False
        asyncio.run(engine.once())
        self.assertEqual(self.task.depends_on, [2])

    def test_new_worker_block_is_planned(self):
        self.task.labels = [graph.PREFIX]
        self.ctx.runtime.script[("worker", 1)] = {"gave_up": "missing helper"}
        out = asyncio.run(run_task(self.task, self.ctx))
        self.assertEqual(out.result, "waiting_dependencies")

    def test_ten_workers_dispatch_and_eleventh_waits(self):
        tasks = {n: graph.Task(n, "Task " + str(n), labels=[graph.PREFIX]) for n in range(1, 12)}
        self.ctx.gh = FakeGitHub(tasks)
        self.ctx.runtime = FakeRuntime()
        self.ctx.cfg.max_parallel = 10
        self.ctx.cfg.auto_prerequisites = False
        self.ctx.run_gates = lambda *a, **kw: []
        engine = Engine(self.ctx, self.root / "ten")
        engine.check_accounts = lambda **_: None
        results = asyncio.run(engine.once())
        self.assertEqual(len(results), 10)
        self.assertEqual(tasks[11].status, graph.QUEUED)

    def test_codex_merge_access_is_role_scoped(self):
        prompt = self.root / "prompt.txt"
        prompt.write_text("prompt", encoding="utf-8")
        with patch("clis.find_codex", return_value="codex"):
            runtime = CodexRuntime(AgentSpec("codex", kind="codex"))
            for role, sandbox in (("worker", "workspace-write"), ("reviewer", "read-only"), ("planner", "read-only"), ("merger", "danger-full-access")):
                argv, _, _ = runtime.command(role, prompt, self.root, self.root)
                self.assertEqual(argv[argv.index("-s") + 1], sandbox)
                self.assertEqual('approval_policy="never"' in argv, role == "merger")

    def test_provider_neutral_merger_and_role_limit(self):
        specs = [AgentSpec("codex", kind="codex", scores={"cpp": 95}), AgentSpec("claude", scores={"cpp": 80})]
        pool = AgentPool(specs, {}, lambda _: FakeRuntime())
        self.assertEqual(pool.merger(), "codex")
        specs[0].roles = ["worker"]
        self.assertEqual(pool.merger(), "claude")
        specs[0].roles = None
        specs[0].max_parallel = 1
        pool.acquire("codex")
        self.assertEqual(pool.merger(), "claude")

    def test_opencode_external_access_only_for_merger(self):
        prompt = self.root / "prompt.txt"
        prompt.write_text("prompt", encoding="utf-8")
        runtime = OpenCodeRuntime(AgentSpec("oc", kind="opencode"))
        with patch("clis.find_opencode", return_value="opencode"):
            for role in ("merger", "worker", "reviewer"):
                _, _, env = runtime.command(role, prompt, self.root, self.root)
                perms = json.loads(env["OPENCODE_CONFIG_CONTENT"])["permission"]
                self.assertEqual(perms["external_directory"], "allow" if role == "merger" else "deny")

    def test_crash_report_deduplicates_restarts_and_rounds(self):
        result = {"round": 12, "at": "now", "agent": "codex", "summary": "RuntimeError: git checkout fletchery/merge-round-12-11040537: unable to unlink asset"}
        n = report_failure(self.gh, self.ctx.cfg, result, str(self.root))
        self.assertEqual(report_failure(self.gh, self.ctx.cfg, result), n)
        self.assertNotIn(n, self.gh.comments)
        result.update(round=13, summary=result["summary"].replace("12-11040537", "13-11041000"))
        self.assertEqual(report_failure(self.gh, self.ctx.cfg, result), n)
        self.assertEqual(len(self.gh.comments[n]), 1)

    def test_resume_snapshot_does_not_wait_for_poll(self):
        engine = Engine(self.ctx, self.root / "status")
        engine.snapshot = {"merge_auto": {"held": True}}
        engine.hold_merging(False)
        self.assertFalse(Dashboard(engine, 0).status()["merge_auto"]["held"])
        self.assertEqual(Dashboard(engine, 0).status()["merge_auto"]["why"], "")


if __name__ == "__main__":
    unittest.main()
