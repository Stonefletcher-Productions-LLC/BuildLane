"""Authorized repair delivery preserves head/ownership guards and never blocks worker scheduling."""
import asyncio
from pathlib import Path
import tempfile
import threading
import subprocess
import unittest
from unittest.mock import Mock, patch

from config import Config
from github import FakeGitHub
from graph import Task
from pipeline import Context, run_task
from repo import FakeRepo, Repo
from runtime import FakeRuntime


class AssetRepairDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.wt = self.root / "tasks" / ("991-hair-repair-" + "a" * 12)
        self.wt.mkdir(parents=True)
        self.helper = self.root / "Tools" / "CI" / "PushWithLfs.ps1"
        self.helper.parent.mkdir(parents=True)
        self.helper.write_text("trusted helper", encoding="utf-8")
        self.repo = Repo(self.root, self.root / "tasks", "main", asset_tasks={"991": {"paths": ["Art/Hair/"]}})
        self.repo.ensure_repair_checkout = Mock()
        self.repo._repair_remote_head = Mock(return_value="a" * 40)
        self.repo.git = Mock(return_value="Art/Hair/Waves.png")
        self.repo.lfs_tracked = Mock(side_effect=lambda _, names: names)

    def push(self):
        self.repo.push_repair(self.wt, "feature-hair", "a" * 40, "lease.json")

    def test_granted_repair_uses_trusted_delivery_helper_after_ownership_recheck(self):
        def deliver(*args, **kwargs):
            self.assertEqual(self.repo.ensure_repair_checkout.call_count, 2)
            return Mock(returncode=0, stdout="", stderr="")
        with patch("repo.subprocess.run", side_effect=deliver) as run:
            self.push()
        self.assertEqual(run.call_args.args[0], ["pwsh", "-NoProfile", "-File", str(self.helper),
                                                "-Branch", "feature-hair", "-Remote", "origin"])
        self.assertEqual(run.call_args.kwargs["cwd"], self.wt)
        self.repo.git.assert_any_call("merge-base", "--is-ancestor", "a" * 40, "HEAD", cwd=self.wt)
        self.assertFalse(any(call.args[0] == "push" for call in self.repo.git.call_args_list))

    def test_remote_head_change_and_ownership_loss_stop_before_delivery(self):
        with patch("repo.subprocess.run") as run:
            self.repo._repair_remote_head.return_value = "b" * 40
            with self.assertRaisesRegex(RuntimeError, "remote PR head changed"):
                self.push()
            self.repo._repair_remote_head.return_value = "a" * 40
            self.repo.ensure_repair_checkout.side_effect = [None, RuntimeError("heavy lease")]
            with self.assertRaisesRegex(RuntimeError, "heavy lease"):
                self.push()
            run.assert_not_called()

    def test_every_changed_lfs_path_requires_grant(self):
        with patch("repo.subprocess.run") as run:
            for grants in ({}, {"991": {"paths": ["Review991/"]}}):
                self.repo.asset_tasks = grants
                with self.assertRaisesRegex(RuntimeError, "operator grant"):
                    self.push()
            self.repo.asset_tasks = {"991": {"paths": ["Art/Hair/"]}}
            self.repo.git.return_value = "Art/Hair/Waves.png\nContent/Other.uasset"
            with self.assertRaisesRegex(RuntimeError, "operator grant"):
                self.push()
            run.assert_not_called()

    def test_delete_only_checks_repair_delta_and_uses_normal_hook(self):
        self.repo.asset_tasks["991"]["mode"] = "delete"
        with patch("repo.subprocess.run") as run:
            with self.assertRaisesRegex(RuntimeError, "Deletion-only"):
                self.push()
            self.repo.git.side_effect = lambda *args, **_: "Art/Hair/Waves.png" if args[:3] == ("diff", "--no-renames", "--name-only") and "--diff-filter=ACMRTUXB" not in args else ""
            self.push()
            self.repo.git.assert_any_call("diff", "--no-renames", "--name-only", "--diff-filter=ACMRTUXB",
                                          "a" * 40 + "..HEAD", cwd=self.wt)
            self.assertEqual(self.repo.git.call_args.args, ("push", "-u", "origin", "HEAD:feature-hair"))
            run.assert_not_called()

    def test_missing_helper_and_upload_failure_do_not_fall_back_to_git_push(self):
        self.helper.unlink()
        with self.assertRaisesRegex(RuntimeError, "delivery helper"):
            self.push()
        self.helper.write_text("trusted helper", encoding="utf-8")
        with patch("repo.subprocess.run", return_value=Mock(returncode=1, stdout="missing object", stderr="")):
            with self.assertRaisesRegex(RuntimeError, "missing object"):
                self.push()
        self.assertFalse(any(call.args[0] == "push" for call in self.repo.git.call_args_list))

    def test_real_git_rename_cannot_hide_ungranted_source_from_worker_or_repair(self):
        def git(*args):
            return subprocess.run(["git", *args], cwd=self.wt, check=True, capture_output=True, text=True).stdout.strip()

        git("init", "-q", "-b", "main")
        git("config", "user.name", "Fixture")
        git("config", "user.email", "fixture@localhost")
        source = self.wt / "Art" / "Unowned" / "Waves.png"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"fake asset data for rename detection")
        git("add", "Art/Unowned/Waves.png")
        git("commit", "-qm", "base asset")
        head = git("rev-parse", "HEAD")
        git("update-ref", "refs/remotes/origin/main", head)
        (self.wt / "Art" / "Hair").mkdir()
        git("mv", "Art/Unowned/Waves.png", "Art/Hair/Waves.png")
        git("commit", "-qm", "move into authorized directory")
        repo = Repo(self.root, self.root / "tasks", "main", asset_tasks=self.repo.asset_tasks)
        # Fake content needs no actual LFS setup/upload; classify both asset paths
        # while retaining Git's real rename detection and diff enumeration.
        repo.lfs_tracked = lambda _, names: names
        repo.ensure_repair_checkout = Mock()
        repo._repair_remote_head = Mock(return_value=head)
        with patch.object(repo, "_publish_assets") as publish:
            for delivery in (lambda: repo.push(self.wt, "hair"), lambda: repo.push_repair(self.wt, "hair", head)):
                with self.subTest(delivery=delivery), self.assertRaisesRegex(RuntimeError, "Art/Unowned/Waves.png"):
                    delivery()
            publish.assert_not_called()


class AsyncWorkerDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_worker_delivery_keeps_event_loop_responsive(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            task = Task(991, "Author hair")
            gh = FakeGitHub({991: task})
            repo = FakeRepo(root / "tasks")
            rt = FakeRuntime({("worker", 991): {"summary": "Authored", "gave_up": ""},
                              ("reviewer", 991): {"verdict": "approve"}})
            ctx = Context(Config(project_root=str(root)), gh, repo, rt, run_gates=lambda *_, **__: [], log=lambda _: None)
            entered, release = threading.Event(), threading.Event()
            original_push = repo.push

            def slow_push(wt, branch):
                entered.set()
                if not release.wait(2):
                    raise RuntimeError("event loop stalled while publishing assets")
                original_push(wt, branch)

            async def other_worker():
                while not entered.is_set():
                    await asyncio.sleep(0)
                release.set()

            with patch.object(repo, "push", side_effect=slow_push):
                result, _ = await asyncio.wait_for(asyncio.gather(run_task(task, ctx), other_worker()), timeout=5)
            self.assertEqual(result.result, "pr", result.detail)
            self.assertEqual(len(repo.pushed), 1)


if __name__ == "__main__":
    unittest.main()
