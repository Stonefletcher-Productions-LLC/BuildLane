"""Hot prompt edits and deferred reloads must not strand workers behind a long merge."""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cli
from config import Config
from github import FakeGitHub
from loop import Engine
from pipeline import Context
from repo import FakeRepo
from runtime import FakeRuntime


class FingerprintTests(unittest.TestCase):
    def test_role_and_ui_edits_are_hot_but_python_changes_require_reload(self):
        with tempfile.TemporaryDirectory(prefix="fletchery-code-fingerprint-") as temp:
            root = Path(temp)
            package = root / "fletchery"
            (package / "roles").mkdir(parents=True)
            (root / "ui").mkdir()
            source = package / "cli.py"
            source.write_text("print('v1')", encoding="utf-8")
            prompt = package / "roles/worker.md"
            prompt.write_text("first prompt", encoding="utf-8")
            page = root / "ui/index.html"
            page.write_text("first UI", encoding="utf-8")
            with patch.object(cli, "__file__", str(source)):
                original = cli.code_fingerprint()
                prompt.write_text("new MCP instructions", encoding="utf-8")
                page.write_text("new Control room", encoding="utf-8")
                self.assertEqual(cli.code_fingerprint(), original)
                source.touch()
                self.assertEqual(cli.code_fingerprint(), original)
                source.write_text("print('v2')", encoding="utf-8")
                self.assertNotEqual(cli.code_fingerprint(), original)


class ReloadDispatchTests(unittest.IsolatedAsyncioTestCase):
    def engine(self):
        folder = tempfile.TemporaryDirectory(prefix="fletchery-reload-")
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        cfg = Config(max_parallel=6, poll_minutes=0.001)
        logs = []
        ctx = Context(cfg, FakeGitHub({}), FakeRepo(root / "worktrees"), FakeRuntime(), log=logs.append)
        engine = Engine(ctx, root / "state")
        return engine, logs

    async def test_pending_code_keeps_refilling_workers_until_round_finishes(self):
        engine, logs = self.engine()
        round_job = asyncio.get_running_loop().create_future()
        engine.round_job = round_job
        starts, snapshots = [], []

        def dispatch(*, draining):
            if not draining and len(starts) < 3:
                starts.append(len(starts) + 1)
                job = asyncio.create_task(asyncio.sleep(0.005))
                engine.jobs[job] = (starts[-1], None, None)
                if len(starts) == 3:
                    round_job.set_result({"result": "landed"})

        def settle(done):
            for job in done:
                engine.jobs.pop(job)
            return []

        def publish(_):
            snapshots.append(engine.make_snapshot({}, {}, {}, []))

        engine.dispatch, engine._settle, engine.publish = dispatch, settle, publish
        await asyncio.wait_for(engine.run_forever(code_changed=lambda: True), timeout=2)
        self.assertEqual(starts, [1, 2, 3], "pending update must keep dispatching during the merge")
        self.assertTrue(any(s["update_pending"] and not s["restarting"] and "Workers continue" in s["update_note"] for s in snapshots))
        self.assertTrue(snapshots[-1]["restarting"], "drain state must be visible after the merge")
        self.assertTrue(any("workers continue" in log for log in logs))
        self.assertFalse(engine.jobs)

    async def test_explicit_project_switch_drains_immediately_during_round(self):
        engine, _ = self.engine()
        round_job = asyncio.get_running_loop().create_future()
        engine.round_job = round_job
        engine.restart_reason = "switching project"
        passes = []
        engine.dispatch = lambda *, draining: passes.append(draining)
        engine.publish = lambda _: None
        asyncio.get_running_loop().call_later(0.01, round_job.set_result, {})
        await asyncio.wait_for(engine.run_forever(code_changed=lambda: True), timeout=2)
        self.assertTrue(passes)
        self.assertTrue(all(passes))
        self.assertEqual(engine.restart_status(), "switching project")

    async def test_code_update_without_round_drains_without_starting_workers(self):
        engine, _ = self.engine()
        passes = []
        engine.dispatch = lambda *, draining: passes.append(draining)
        engine.publish = lambda _: None
        await asyncio.wait_for(engine.run_forever(code_changed=lambda: True), timeout=2)
        self.assertEqual(passes, [True])
        self.assertIn("reloading", engine.restart_status())

    async def test_update_reverted_before_round_end_does_not_latch_drain(self):
        engine, _ = self.engine()
        round_job = asyncio.get_running_loop().create_future()
        engine.round_job = round_job
        code_changed = True
        passes = []

        def dispatch(*, draining):
            nonlocal code_changed
            passes.append((draining, engine.update_pending))
            if len(passes) == 1:
                code_changed = False
                engine._wake.set()
            elif not draining:
                # End the test using the supported explicit restart path.
                engine.request_restart("test complete")
                round_job.set_result({})

        engine.dispatch = dispatch
        engine.publish = lambda _: None
        await asyncio.wait_for(engine.run_forever(code_changed=lambda: code_changed), timeout=2)
        self.assertEqual(passes[:2], [(False, True), (False, False)])


if __name__ == "__main__":
    unittest.main()
