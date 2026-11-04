"""Repair existing PRs from their real head and protect concurrent remote changes."""
from __future__ import annotations

import asyncio
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from config import Config
from github import FakeGitHub, Feedback, PullRequest
from graph import Task
from pipeline import Context, Outcome, _fix_pr
from repo import Repo
from runtime import FakeRuntime


class RepairCheckoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fletchery-pr-repair-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.origin = self.root / "origin.git"
        self.author = self.root / "author"
        self.host = self.root / "host"
        self.git("init", "-q", "--bare", "-b", "main", str(self.origin))
        self.git("clone", "-q", str(self.origin), str(self.author))
        self.identity(self.author)
        (self.author / "base.txt").write_text("base", encoding="utf-8")
        self.git("add", "base.txt", cwd=self.author)
        self.git("commit", "-qm", "base", cwd=self.author)
        self.git("push", "-q", "--no-verify", "origin", "HEAD:main", cwd=self.author)
        self.git("clone", "-q", str(self.origin), str(self.host))
        self.identity(self.host)
        self.git("switch", "-qc", "external-feature", cwd=self.author)
        (self.author / "feature.txt").write_text("the existing PR implementation", encoding="utf-8")
        self.git("add", "feature.txt", cwd=self.author)
        self.git("commit", "-qm", "feature", cwd=self.author)
        self.git("push", "-q", "--no-verify", "origin", "HEAD:external-feature", cwd=self.author)
        self.head = self.git("rev-parse", "HEAD", cwd=self.author).strip()
        self.repo = Repo(self.host, self.root / "worktrees", "main")
        self.probe = patch("rounds.worktree_busy", return_value=[])
        self.probe.start()
        self.addCleanup(self.probe.stop)

    def git(self, *args, cwd=None):
        return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=True).stdout

    def identity(self, where):
        for key, value in (("user.name", "Fixture"), ("user.email", "fixture@localhost")):
            self.git("config", key, value, cwd=where)

    def advance_remote(self):
        (self.author / "concurrent.txt").write_text("another person's work", encoding="utf-8")
        self.git("add", "concurrent.txt", cwd=self.author)
        self.git("commit", "-qm", "concurrent change", cwd=self.author)
        self.git("push", "-q", "--no-verify", "origin", "HEAD:external-feature", cwd=self.author)
        return self.git("rev-parse", "HEAD", cwd=self.author).strip()

    def prepared(self):
        return self.repo.repair_worktree(1, "repair", "external-feature", self.head)

    def commit_fix(self, where):
        (where / "fix.txt").write_text("fix", encoding="utf-8")
        self.repo.commit_all(where, "bounded fix")

    def test_absent_local_feature_branch_starts_at_actual_pr_head(self):
        self.assertEqual(self.git("branch", "--list", "external-feature", cwd=self.host).strip(), "")
        (self.author / "artist.blend").write_bytes(b"uncommitted source scene")
        wt = self.prepared()
        self.assertEqual(self.repo.head(wt), self.head)
        self.assertEqual((wt / "feature.txt").read_text(), "the existing PR implementation")
        self.assertEqual((self.author / "artist.blend").read_bytes(), b"uncommitted source scene")
        (wt / "local-draft.txt").write_text("preserve an interrupted fix", encoding="utf-8")
        self.assertEqual(self.prepared(), wt)
        self.assertEqual((wt / "local-draft.txt").read_text(), "preserve an interrupted fix")

    def test_changed_head_before_repair_does_not_create_a_checkout(self):
        self.advance_remote()
        with self.assertRaisesRegex(RuntimeError, "changed before repair"):
            self.prepared()
        self.assertFalse(self.repo.worktree_root.exists())

    def test_changed_head_during_repair_preserves_both_writers(self):
        wt = self.prepared()
        self.commit_fix(wt)
        concurrent = self.advance_remote()
        with self.assertRaisesRegex(RuntimeError, "changed during repair"):
            self.repo.push_repair(wt, "external-feature", self.head)
        self.assertEqual(self.git("rev-parse", "refs/heads/external-feature", cwd=self.origin).strip(), concurrent)
        self.assertTrue((wt / "fix.txt").exists())

    def test_normal_fast_forward_push_protects_a_race_after_the_head_check(self):
        wt = self.prepared()
        self.commit_fix(wt)
        real_git = self.repo.git
        concurrent = []
        commands = []
        def race(*args, **kwargs):
            commands.append(args)
            if args[0] == "push":
                concurrent.append(self.advance_remote())
            return real_git(*args, **kwargs)
        with patch.object(self.repo, "git", side_effect=race):
            with self.assertRaises(RuntimeError):
                self.repo.push_repair(wt, "external-feature", self.head)
        push = next(command for command in commands if command[0] == "push")
        self.assertFalse(any(argument.startswith("--force") for argument in push))
        self.assertEqual(self.git("rev-parse", "refs/heads/external-feature", cwd=self.origin).strip(), concurrent[0])
        self.assertTrue((wt / "fix.txt").exists())

    def test_fix_must_retain_original_pr_ancestry(self):
        wt = self.prepared()
        self.repo.git("checkout", "--detach", "origin/main", cwd=wt)
        with self.assertRaisesRegex(RuntimeError, "another local branch"):
            self.repo.push_repair(wt, "external-feature", self.head)
        self.assertEqual(self.git("rev-parse", "refs/heads/external-feature", cwd=self.origin).strip(), self.head)

    def test_fixer_pipeline_repairs_existing_head_not_generated_issue_branch(self):
        task = Task(1, "Repair the PR")
        pr = PullRequest(700, "External feature", "external-feature", "main", self.head, ["ready"])
        def fix(role, where):
            self.assertEqual(role, "fixer")
            self.assertEqual((where / "feature.txt").read_text(), "the existing PR implementation")
            (where / "fix.txt").write_text("pipeline fix", encoding="utf-8")
        runtime = FakeRuntime({("fixer", 1): {"summary": "Fixed", "pushed": False}}, on_run=fix)
        gh = FakeGitHub({1: task})
        cfg = Config(project_root=str(self.host), base_branch="main")
        ctx = Context(cfg, gh, self.repo, runtime, run_gates=lambda *_, **_kw: [], log=lambda _: None)
        result = asyncio.run(_fix_pr(task, pr, [Feedback(700, "merge", "failure", "2026-11-04T00:00:00Z")], 1, ctx, Outcome(1, "gave_up", pr_number=700)))
        self.assertEqual(result.result, "fixed")
        actual = self.git("rev-parse", "refs/heads/external-feature", cwd=self.origin).strip()
        self.assertNotEqual(actual, self.head)
        self.assertEqual(self.git("show", f"{actual}:feature.txt", cwd=self.origin), "the existing PR implementation")
        self.assertEqual(self.git("show", f"{actual}:fix.txt", cwd=self.origin), "pipeline fix")
        self.assertIn("local repair branch", runtime.calls[0][2])

    def test_repair_cannot_target_base_or_nonexistent_remote_branch(self):
        with self.assertRaisesRegex(RuntimeError, "base/default"):
            self.repo.repair_worktree(1, "repair", "main", self.head)
        with self.assertRaisesRegex(RuntimeError, "not on the configured remote"):
            self.repo.repair_worktree(1, "repair", "fork-only-branch", self.head)


if __name__ == "__main__":
    unittest.main()
