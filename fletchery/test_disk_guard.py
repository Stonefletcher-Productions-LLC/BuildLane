"""Task-copy retirement and the disk guard; filesystem changes stay inside temporary fixtures."""
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch

import storage


class Repo:
    def __init__(self, root: Path):
        self.worktree_root = root
        self.paths: set[Path] = set()
        self.branch = {}
        self.status = {}
        self.removed = []

    def add(self, name, branch="claude/task", status="", age_hours=48.0):
        path = self.worktree_root / name
        path.mkdir()
        (path / ".git").write_text("gitdir: x", encoding="utf-8")
        (path / "Source.cpp").write_text("x" * 100, encoding="utf-8")
        old = time.time() - age_hours * 3600
        for p in (path / "Source.cpp", path):
            os.utime(p, (old, old))
        self.paths.add(path.resolve())
        self.branch[path.resolve()] = branch
        self.status[path.resolve()] = status
        return path

    def git(self, *args, cwd=None, check=True):
        key = Path(cwd).resolve() if cwd else None
        if args[:2] == ("worktree", "list"):
            return "\0".join("worktree " + str(p) for p in self.paths) + "\0"
        if args[:2] == ("branch", "--show-current"):
            return self.branch[key]
        if args == ("rev-parse", "HEAD"):
            return "a" * 40
        if args[0] == "status":
            return self.status[key]
        if args[0] == "diff":
            return ""
        if args[:3] == ("worktree", "remove", "--force"):
            target = Path(args[3]).resolve()
            assert target in self.paths and target.parent == self.worktree_root.resolve()
            shutil.rmtree(target)
            self.paths.discard(target)
            self.removed.append(target.name)
            return ""
        raise AssertionError(args)


class RetireTaskCheckouts(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="fletchery-disk-test-")
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name).resolve()
        self.root = base / "worktrees"
        self.root.mkdir()
        self.backup = base / "task-backups"
        self.repo = Repo(self.root)
        busy = patch("rounds.worktree_busy", return_value=[])
        self.busy = busy.start()
        self.addCleanup(busy.stop)

    def registry(self, active=None, preserved=None):
        (self.root / ".task-checkouts.json").write_text(json.dumps({"active": active or {}, "preserved": preserved or []}),
                                                         encoding="utf-8")

    def run_retire(self, open_tasks=(5,), **kw):
        return storage.retire_task_checkouts(self.repo, open_tasks=open_tasks, backup_dir=str(self.backup), **kw)

    def test_closed_task_copies_go_and_open_active_copy_stays(self):
        closed = self.repo.add("7-done-thing")
        closed_attempt = self.repo.add("7-done-thing-attempt-0123456789ab")
        active = self.repo.add("5-open-thing-attempt-aaaaaaaaaaaa")
        stale = self.repo.add("5-open-thing")
        self.registry(active={"claude/open": {"path": str(active), "local_branch": "fletchery/task-5-attempt-aaaaaaaaaaaa"}},
                      preserved=[{"path": str(stale), "local_branch": "claude/open"}])
        result = self.run_retire()
        self.assertEqual(result["error"], "")
        self.assertCountEqual(self.repo.removed, [closed.name, closed_attempt.name, stale.name])
        self.assertTrue(active.exists())
        data = json.loads((self.root / ".task-checkouts.json").read_text(encoding="utf-8"))
        self.assertTrue(data["preserved"][0].get("retired_at"))

    def test_recent_copies_wait(self):
        self.repo.add("7-done-thing", age_hours=0.2)
        self.repo.add("5-open-thing-repair-0123456789ab", age_hours=5)
        self.assertEqual(self.run_retire()["retired"], [])

    def test_busy_detached_and_merge_checkouts_stay(self):
        self.repo.add("7-done-thing")
        self.repo.add("8-detached", branch="")
        (self.root / "merge-round-abc").mkdir()
        self.busy.side_effect = lambda p: ["123 UnrealEditor.exe"] if Path(p).name == "7-done-thing" else []
        result = self.run_retire()
        self.assertEqual(self.repo.removed, [])
        self.assertTrue((self.root / "merge-round-abc").exists())
        reasons = " ".join(s["reason"] for s in result["skipped"])
        self.assertIn("In use", reasons)
        self.assertIn("Detached", reasons)

    def test_uncommitted_files_are_backed_up_first(self):
        path = self.repo.add("7-done-thing", status=" M Source.cpp\0?? notes.txt\0")
        (path / "notes.txt").write_text("keep me", encoding="utf-8")
        old = time.time() - 48 * 3600
        os.utime(path / "notes.txt", (old, old))
        logs = path / "Saved" / "Logs"
        logs.mkdir(parents=True)
        (logs / "LifeShare.log").write_text("log", encoding="utf-8")
        for p in (logs / "LifeShare.log", logs, path / "Saved", path):
            os.utime(p, (old, old))
        result = self.run_retire()
        self.assertEqual(self.repo.removed, ["7-done-thing"])
        backup = Path(result["retired"][0]["backup"])
        self.assertEqual((backup / "files" / "notes.txt").read_text(encoding="utf-8"), "keep me")
        self.assertTrue((backup / "logs" / "LifeShare.log").exists())

    def test_large_uncommitted_work_is_left_for_a_person(self):
        self.repo.add("7-done-thing", status=" M Source.cpp\0")
        result = self.run_retire(max_backup_bytes=10)
        self.assertEqual(self.repo.removed, [])
        self.assertIn("uncommitted", result["skipped"][0]["reason"])

    def test_no_open_tasks_with_active_records_defers(self):
        path = self.repo.add("5-open-thing")
        self.registry(active={"claude/open": {"path": str(path), "local_branch": "claude/open"}})
        result = self.run_retire(open_tasks=())
        self.assertIn("deferred", result["error"])
        self.assertEqual(self.repo.removed, [])

    def test_dry_run_removes_nothing(self):
        self.repo.add("7-done-thing")
        result = self.run_retire(dry_run=True)
        self.assertEqual(len(result["retired"]), 1)
        self.assertEqual(self.repo.removed, [])

    def test_backup_inside_worktree_root_refused(self):
        self.repo.add("7-done-thing")
        result = storage.retire_task_checkouts(self.repo, open_tasks=(5,), backup_dir=str(self.root / "b"))
        self.assertIn("inside the worktree root", result["error"])

    def test_running_task_copies_stay(self):
        self.repo.add("5-open-thing-attempt-0123456789ab")
        self.repo.add("7-done-thing")
        result = self.run_retire(running_tasks={5, 7})
        self.assertEqual(self.repo.removed, [])
        self.assertIn("running", result["skipped"][0]["reason"])

    def test_limit(self):
        for n in range(5):
            self.repo.add(f"{10 + n}-done")
        self.assertEqual(len(self.run_retire(limit=2)["retired"]), 2)


class DiskReport(unittest.TestCase):
    def test_top_folders(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, size in (("big", 3000), ("small", 10), ("mid", 500)):
                (root / name).mkdir()
                (root / name / "f.bin").write_bytes(b"x" * size)
            report = storage.disk_report(tmp, tmp, top=2)
            self.assertIsNotNone(report["free_gb"])
            self.assertEqual([Path(t["path"]).name for t in report["top"]], ["big", "mid"])

    def test_prune_backups(self):
        with tempfile.TemporaryDirectory() as tmp:
            old, new = Path(tmp) / "old", Path(tmp) / "new"
            old.mkdir(); new.mkdir()
            past = time.time() - 30 * 86400
            os.utime(old, (past, past))
            self.assertEqual(storage.prune_backups(tmp, 14), 1)
            self.assertFalse(old.exists())
            self.assertTrue(new.exists())


class DiskGuard(unittest.TestCase):
    def guard(self, free_gb, limit):
        from types import SimpleNamespace
        import loop
        logs = []
        fake = SimpleNamespace(cfg=SimpleNamespace(work_min_free_gb=limit, worktree_root=tempfile.gettempdir()),
                               dry_run=False, _storage_job=None, state=SimpleNamespace(storage_requested=False),
                               _disk_cleanup_at=0.0, disk_free_gb=None, ctx=SimpleNamespace(log=logs.append))
        usage = SimpleNamespace(free=free_gb * 1024 ** 3, total=1, used=1)
        with patch("shutil.disk_usage", return_value=usage):
            why = loop.Engine._disk_guard(fake)
        return why, fake, logs

    def test_low_disk_holds_new_work_and_requests_cleanup_once(self):
        why, fake, logs = self.guard(50, 100)
        self.assertIn("50 GB free", why)
        self.assertTrue(fake.state.storage_requested)
        self.assertEqual(len(logs), 1)
        fake.state.storage_requested = False  # cleanup ran; the next request waits 10 minutes
        import loop
        with patch("shutil.disk_usage", return_value=__import__("types").SimpleNamespace(free=50 * 1024 ** 3)):
            loop.Engine._disk_guard(fake)
        self.assertFalse(fake.state.storage_requested)

    def test_enough_space_or_off(self):
        self.assertEqual(self.guard(150, 100)[0], "")
        self.assertEqual(self.guard(5, 0)[0], "")


if __name__ == "__main__":
    unittest.main()
