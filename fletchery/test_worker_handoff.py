"""Pause at safe worker boundaries and preserve a verifiable Git checkpoint."""
import asyncio
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from github import Feedback, PullRequest
from graph import Task, PR, label_for
from loop import Engine, State
from pipeline import Outcome, fix_pr
from repo import Repo
from runtime import RunResult
import test_dispatch_resilience as fixtures


class HandoffTests(unittest.IsolatedAsyncioTestCase):
    engine = fixtures.DispatchResilienceTests.engine

    async def test_git_checkpoint_stages_task_files_without_commit_push_or_cleanup(self):
        folder = tempfile.TemporaryDirectory(prefix="fletchery-handoff-")
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        source = root / "source"; source.mkdir()
        def git(*args, cwd=source):
            return subprocess.check_output(["git", *args], cwd=cwd, text=True, encoding="utf-8").strip()
        git("init", "-q", "-b", "main")
        (source / "tracked.txt").write_text("base", encoding="utf-8")
        git("add", "tracked.txt")
        git("-c", "user.name=Fixture", "-c", "user.email=test@localhost", "commit", "-qm", "base")
        git("update-ref", "refs/remotes/origin/main", "HEAD")
        task = Task(1, "Preserved feature")
        engine, gh, rt = self.engine({1: task})
        repo = Repo(source, root / "worktrees", "main", thin_text_workers=False)
        engine.ctx.repo = repo
        wt = repo.worktree(task.number, task.slug, task.branch)
        head = git("rev-parse", "HEAD", cwd=wt)
        (wt / "tracked.txt").write_text("feature", encoding="utf-8")
        (wt / "new.txt").write_text("new feature", encoding="utf-8")
        (wt / "artist-original.blend").write_bytes(b"preserved but not staged")
        (root / "outside.txt").write_text("outside", encoding="utf-8")
        engine.active.add(1)
        engine.handoffs.request()
        out = Outcome(1, "requeue", agent="previous")
        self.assertTrue(engine.handoffs.checkpoint(task, out, wt, "writing", {"summary": "Draft", "files": ["new.txt", str(root / "outside.txt")]}))
        self.assertEqual(out.result, "handed_off")
        self.assertEqual(git("rev-parse", "HEAD", cwd=wt), head)
        self.assertEqual(set(git("diff", "--cached", "--name-only", cwd=wt).splitlines()), {"tracked.txt", "new.txt"})
        self.assertEqual((wt / "artist-original.blend").read_bytes(), b"preserved but not staged")
        self.assertEqual((source / "tracked.txt").read_text(), "base")
        self.assertEqual(git("status", "--porcelain"), "")
        saved = State.load(engine.state_dir / "state.json").worker_handoffs["1"]
        self.assertEqual(saved["head"], head)
        self.assertEqual(saved["state"], "checkpointed")
        self.assertIn("Draft", engine.handoffs.note(1, wt))

    async def test_active_writer_finishes_phase_then_resumes_with_checkpoint_note(self):
        task = Task(1, "Feature")
        engine, gh, rt = self.engine({1: task})
        entered, finish = asyncio.Event(), asyncio.Event()
        original = rt.run
        async def slow(role, *args, **kwargs):
            if role == "worker" and not finish.is_set():
                entered.set()
                await finish.wait()
            return await original(role, *args, **kwargs)
        with patch.object(rt, "run", side_effect=slow):
            engine.dispatch()
            await asyncio.wait_for(entered.wait(), 3)
            self.assertEqual(engine.handoffs.request()["tasks"], [1])
            self.assertTrue(engine.state.held)
            self.assertTrue(engine.jobs)
            finish.set()
            first = await engine._wait_for_jobs(None)
            self.assertEqual(first[0].result, "handed_off")
            self.assertEqual([role for role, _, _ in rt.calls], ["worker"])
            await engine.once()
            self.assertEqual(len(rt.calls), 1)
            engine.hold(False)
            await engine.once()
        self.assertIn("Resume saved worker handoff", rt.calls[1][2])

    async def test_fixer_handoff_does_not_consume_attempt_or_new_feedback(self):
        task = Task(1, "Repair", labels=[label_for(PR)])
        engine, gh, rt = self.engine({1: task})
        pr = PullRequest(12, "Repair", task.branch, "Test-Staging", "a" * 40, [])
        feedback = [Feedback(12, "comment", "Fix concrete defect", "2026-11-05T18:00:00Z")]
        entered, finish = asyncio.Event(), asyncio.Event()
        async def pause_at_fix(*args, **kwargs):
            entered.set(); await finish.wait()
            return RunResult(role="fixer", ok=True, output={"summary": "Partial repair", "files": []})
        engine.state.fix_attempts["12"] = 1
        engine.state.seen["12"] = feedback[0].at
        with patch.object(rt, "run", side_effect=pause_at_fix):
            engine._start(fix_pr(task, pr, feedback, 1, engine.ctx), 1, 12, None)
            await asyncio.wait_for(entered.wait(), 3)
            engine.handoffs.request(); finish.set()
            result = await engine._wait_for_jobs(None)
        self.assertEqual(result[0].result, "handed_off")
        self.assertEqual(engine.state.fix_attempts["12"], 0)
        self.assertNotIn("12", engine.state.seen)
        self.assertEqual(engine.state.worker_handoffs["1"]["phase"], "repair writing")

    async def test_restart_distinguishes_requested_from_staged_handoff(self):
        engine, gh, rt = self.engine()
        engine.active.add(1); engine.handoffs.request()
        restored = Engine(engine.ctx, engine.state_dir)
        self.assertEqual(restored.state.worker_handoffs["1"]["state"], "interrupted")
        self.assertTrue(restored.state.held)

    async def test_storage_cleanup_protects_paused_worker_checkout(self):
        engine, gh, rt = self.engine()
        engine.ctx.repo.git = lambda *a, **k: ""
        owned = str(engine.state_dir / "worker-checkout")
        engine.state.worker_handoffs["1"] = {"state": "checkpointed", "worktree": owned}
        with patch("storage.maintain", return_value={"checked_at": "now", "retired": [], "eligible": [], "skipped": []}) as clean:
            engine._maybe_storage({})
            await engine._storage_job
        self.assertIn(owned, clean.call_args.kwargs["protected_paths"])


if __name__ == "__main__":
    unittest.main()
