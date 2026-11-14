"""Real Git fixtures for failed-attempt preservation and checkout ownership."""
import asyncio
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_repair_checkout as repair
from repo import Repo
from config import Config
from graph import Task
from github import FakeGitHub
from pipeline import Context, Outcome, _attempt
from runtime import FakeRuntime


class TaskPreservationTests(unittest.TestCase):
    setUp = repair.RepairCheckoutTests.setUp
    git = repair.RepairCheckoutTests.git
    identity = repair.RepairCheckoutTests.identity
    prepared = repair.RepairCheckoutTests.prepared

    def worker(self):
        return self.repo.worktree(42, "assets", "fletchery/42-assets")

    def test_escalation_preserves_every_kind_of_work_and_survives_restart(self):
        old = self.worker()
        (old / "committed.txt").write_text("old implementation")
        self.repo.commit_all(old, "first attempt")
        (old / "base.txt").write_text("staged")
        self.git("add", "base.txt", cwd=old)
        (old / "base.txt").write_text("unstaged")
        (old / "scene.blend").write_bytes(b"artist source")
        (old / ".gitignore").write_text("generated.uasset\n")
        (old / "generated.uasset").write_bytes(b"ignored generated asset")
        before = self.git("status", "--porcelain", "--ignored", cwd=old)
        head = self.repo.head(old)
        fresh = self.repo.restart_task_worktree(42, "assets", "fletchery/42-assets", old, "review failed")
        self.assertNotEqual(old, fresh)
        self.assertEqual(self.repo.head(old), head)
        self.assertEqual(self.git("status", "--porcelain", "--ignored", cwd=old), before)
        self.assertEqual(self.git("show", ":base.txt", cwd=old), "staged")
        self.assertEqual((old / "base.txt").read_text(), "unstaged")
        self.assertEqual((old / "scene.blend").read_bytes(), b"artist source")
        self.assertEqual((old / "generated.uasset").read_bytes(), b"ignored generated asset")
        self.assertFalse((fresh / "committed.txt").exists())
        restarted = Repo(self.host, self.repo.worktree_root, "main")
        self.assertEqual(restarted.worktree(42, "assets", "fletchery/42-assets"), fresh)
        (fresh / "healthy.txt").write_text("replacement")
        restarted.commit_all(fresh, "healthy attempt")
        restarted.push(fresh, "fletchery/42-assets")
        self.assertEqual(self.git("show", "refs/heads/fletchery/42-assets:healthy.txt", cwd=self.origin), "replacement")
        second = restarted.restart_task_worktree(42, "assets", "fletchery/42-assets", fresh, "again")
        self.assertNotEqual(second, fresh)
        state = json.loads((self.repo.worktree_root / ".task-checkouts.json").read_text())
        self.assertEqual(len(state["preserved"]), 2)
        with self.assertRaisesRegex(RuntimeError, "no longer its active"):
            restarted.ensure_task_checkout(42, "assets", "fletchery/42-assets", old)

    def test_worker_pipeline_uses_fresh_local_branch_and_stable_remote_target(self):
        task = Task(42, "Assets")
        old = self.repo.worktree(task.number, task.slug, task.branch)
        (old / "scene.blend").write_bytes(b"failed attempt source")
        fresh = self.repo.restart_task_worktree(task.number, task.slug, task.branch, old, "review failed")
        local_branch = self.git("branch", "--show-current", cwd=fresh).strip()
        def work(role, where):
            self.assertEqual(where, fresh)
            if role == "worker":
                (where / "healthy.txt").write_text("new implementation")
        runtime = FakeRuntime(on_run=work)
        gh = FakeGitHub({42: task})
        cfg = Config(project_root=str(self.host), base_branch="main")
        ctx = Context(cfg, gh, self.repo, runtime, run_gates=lambda *_, **_kw: [], log=lambda _: None)
        out = asyncio.run(_attempt(task, ctx, Outcome(42, "gave_up"), None, ["Preserved attempt: " + str(old)], False))
        self.assertEqual(out.result, "pr", out.detail)
        self.assertIn(local_branch, runtime.calls[0][2])
        self.assertIn(str(old), runtime.calls[0][2])
        self.assertEqual(self.git("show", f"refs/heads/{task.branch}:healthy.txt", cwd=self.origin), "new implementation")
        self.assertEqual((old / "scene.blend").read_bytes(), b"failed attempt source")

    def test_lease_and_process_ownership_refuse_without_modifying_assets(self):
        old = self.worker()
        (old / "scene.blend").write_bytes(b"keep")
        lease = self.root / "lease.json"
        lease.write_text(json.dumps({"token": "active", "workspaces": [str(old)]}))
        with self.assertRaisesRegex(RuntimeError, "heavy lease"):
            self.repo.restart_task_worktree(42, "assets", "fletchery/42-assets", old, "bad", str(lease))
        with patch("rounds.worktree_busy", return_value=["editor is running"]):
            with self.assertRaises(RuntimeError):
                self.repo.restart_task_worktree(42, "assets", "fletchery/42-assets", old, "bad")
        self.assertEqual((old / "scene.blend").read_bytes(), b"keep")
        lease.write_text(json.dumps({"token": "active", "workspaces": [str(self.root / "merge-round")]}))
        self.assertNotEqual(self.repo.restart_task_worktree(42, "assets", "fletchery/42-assets", old, "ok", str(lease)), old)

    def test_corrupt_registry_and_foreign_branch_fail_closed(self):
        old = self.worker()
        marker = old / "scene.blend"
        marker.write_bytes(b"keep")
        self.git("switch", "-qc", "other-owner", cwd=old)
        with self.assertRaisesRegex(RuntimeError, "another local branch"):
            self.worker()
        registry = self.repo.worktree_root / ".task-checkouts.json"
        registry.write_text("{broken")
        with self.assertRaisesRegex(RuntimeError, "Cannot read task checkout state"):
            self.worker()
        registry.write_text(json.dumps({"active": {"fletchery/42-assets": {"path": str(self.host), "local_branch": "main"}}, "preserved": []}))
        with self.assertRaisesRegex(RuntimeError, "Invalid active task checkout path"):
            self.worker()
        self.assertEqual(marker.read_bytes(), b"keep")

    def test_read_only_planner_resolution_uses_active_attempt_without_mutation(self):
        old = self.worker()
        (old / "draft.txt").write_text("old saved evidence")
        current = self.repo.restart_task_worktree(42, "assets", "fletchery/42-assets", old, "retry")
        (current / "draft.txt").write_text("current saved evidence")
        registry = self.repo.worktree_root / ".task-checkouts.json"
        before = registry.read_bytes()
        resumed = Repo(self.host, self.repo.worktree_root, "main")
        self.assertEqual(resumed.existing_task_worktree(42, "assets", "fletchery/42-assets"), current.resolve())
        self.assertEqual(registry.read_bytes(), before)
        self.assertEqual((old / "draft.txt").read_text(), "old saved evidence")
        self.assertEqual((current / "draft.txt").read_text(), "current saved evidence")
        self.assertIsNone(resumed.existing_task_worktree(99, "missing", "fletchery/99-missing"))
        self.assertFalse((self.repo.worktree_root / "99-missing").exists())

    def delete_folder(self, path):
        import shutil, stat, os
        shutil.rmtree(path, onerror=lambda f, p, _e: (os.chmod(p, stat.S_IWRITE), f(p)))
        self.assertFalse(path.exists())

    def test_deleted_active_attempt_is_re_added_at_its_branch(self):
        """LifeShare 2026-11-13: a disk cleanup deleted the folders; tasks went to Needs you instead of resuming."""
        old = self.worker()
        current = self.repo.restart_task_worktree(42, "assets", "fletchery/42-assets", old, "retry")
        (current / "kept.txt").write_text("committed attempt work")
        self.repo.commit_all(current, "attempt work")
        head = self.repo.head(current)
        self.delete_folder(current)
        resumed = Repo(self.host, self.repo.worktree_root, "main")
        self.assertIsNone(resumed.existing_task_worktree(42, "assets", "fletchery/42-assets"))
        again = resumed.worktree(42, "assets", "fletchery/42-assets")
        self.assertEqual(again.resolve(), current.resolve())
        self.assertEqual(resumed.head(again), head)
        self.assertEqual((again / "kept.txt").read_text(), "committed attempt work")
        self.assertTrue(old.exists())

    def test_deleted_checkout_with_lost_local_branch_starts_from_pushed_branch(self):
        wt = self.worker()
        (wt / "pushed.txt").write_text("pushed work")
        self.repo.commit_all(wt, "pushed work")
        self.repo.push(wt, "fletchery/42-assets")
        self.git("fetch", "-q", "origin", cwd=self.host)
        self.delete_folder(wt)
        self.git("worktree", "add", "-q", "-f", "--detach", str(wt), "main", cwd=self.host)
        self.delete_folder(wt)  # registration now holds no branch
        self.git("branch", "-D", "fletchery/42-assets", cwd=self.host)
        again = self.repo.worktree(42, "assets", "fletchery/42-assets")
        self.assertEqual((again / "pushed.txt").read_text(), "pushed work")

    def test_deleted_checkout_refuses_when_branch_is_live_elsewhere(self):
        wt = self.worker()
        self.delete_folder(wt)
        other = self.root / "someone-else"
        self.git("worktree", "add", "-q", "-f", str(other), "fletchery/42-assets", cwd=self.host)
        with self.assertRaisesRegex(RuntimeError, "checked out in another worktree"):
            self.worker()
        self.assertTrue(other.exists())

    def test_planner_registry_cannot_redirect_to_foreign_checkout(self):
        self.repo.worktree_root.mkdir(parents=True, exist_ok=True)
        registry = self.repo.worktree_root / ".task-checkouts.json"
        registry.write_text(json.dumps({"active": {"fletchery/42-assets":
            {"path": str(self.host), "local_branch": "main"}}, "preserved": []}))
        with self.assertRaisesRegex(RuntimeError, "Invalid active task checkout path"):
            self.repo.existing_task_worktree(42, "assets", "fletchery/42-assets")

    def test_repair_dirty_wrong_branch_and_lease_are_never_pushed(self):
        wt = self.prepared()
        (wt / "scene.blend").write_bytes(b"keep")
        with self.assertRaisesRegex(RuntimeError, "uncommitted"):
            self.repo.push_repair(wt, "external-feature", self.head)
        self.repo.commit_all(wt, "source")
        lease = self.root / "lease.json"
        lease.write_text(json.dumps({"token": "active", "workspaces": str(wt)}))
        with self.assertRaisesRegex(RuntimeError, "heavy lease"):
            self.repo.push_repair(wt, "external-feature", self.head, str(lease))
        self.git("switch", "-qc", "other-owner", cwd=wt)
        with self.assertRaisesRegex(RuntimeError, "another local branch"):
            self.prepared()
        self.assertEqual(self.git("rev-parse", "refs/heads/external-feature", cwd=self.origin).strip(), self.head)
        self.assertEqual((wt / "scene.blend").read_bytes(), b"keep")


if __name__ == "__main__":
    unittest.main()
