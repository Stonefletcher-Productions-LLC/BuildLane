"""Retirement safety checks; all filesystem mutations are isolated temporary fixtures."""
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch

import storage


class FixtureRepo:
    def __init__(self, root):
        self.worktree_root = root
        self.root = root.parent
        self.remote, self.base = "origin", "Test-Staging"
        self.head, self.base_head = "a"*40, "b"*40
        self.branch = "claude/foreman-1-finished"
        self.paths = set()
        self.calls = []
        self.status = ""
        self.ignored = ""
        self.flags = "H source.py\0"
        self.published = True
        self.lfs_files = []
        self.media = root.parent/"lfs-objects"
        self.remote_error = False

    def git(self, *args, cwd=None, check=True):
        self.calls.append((args, cwd))
        if args[:2] == ("worktree", "list"):
            return "\0".join("worktree " + str(p) for p in self.paths) + "\0"
        if args[0] == "ls-remote":
            if self.remote_error:
                raise RuntimeError("Remote unavailable")
            return self.base_head + "\trefs/heads/" + self.base
        if args[:2] == ("branch", "--show-current"):
            return self.branch
        if args == ("rev-parse", "HEAD"):
            return self.head
        if args[:2] == ("merge-base", "--is-ancestor"):
            if not self.published:
                raise RuntimeError("Unpublished commit")
            return ""
        if args[0] == "status":
            return self.status
        if args[:2] == ("ls-files", "--others"):
            return self.ignored
        if args[:2] == ("ls-files", "-v"):
            return self.flags
        if args[:2] == ("lfs", "ls-files"):
            return json.dumps({"files":self.lfs_files})
        if args == ("lfs", "env"):
            return "LocalMediaDir=" + str(self.media)
        if args[:3] == ("worktree", "remove", "--force"):
            target = Path(args[3]).resolve()
            assert target in self.paths and target.parent == self.worktree_root.resolve()
            shutil.rmtree(target)  # Only a known test fixture inside TemporaryDirectory.
            self.paths.remove(target)
            return ""
        raise AssertionError(args)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="fletchery-storage-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)/"worktrees"
        self.root.mkdir()
        self.repo = FixtureRepo(self.root)
        self.gh = Mock()
        self.gh.issue_closed.return_value = True
        self.path = self.make_candidate("1-finished")
        self.guard = patch("rounds.checkout_guard")
        self.guard_mock = self.guard.start()
        self.addCleanup(self.guard.stop)

    def make_candidate(self, name):
        path = self.root/name
        path.mkdir()
        (path/".git").write_text("gitdir: fixture")
        (path/"source.py").write_text("print('source')")
        self.repo.paths.add(path.resolve())
        return path

    def run_maintenance(self, **kw):
        return storage.maintain(self.repo, self.gh, self.root.parent, retention_hours=kw.pop("retention_hours", 0), **kw)

    def assert_preserved(self, result):
        self.assertTrue(self.path.exists(), result)
        self.assertFalse(result["retired"])
        self.assertFalse(any(args[:2] == ("worktree","remove") for args, _ in self.repo.calls))

    def test_closed_published_clean_retirement_keeps_branch_and_objects(self):
        result = self.run_maintenance()
        self.assertFalse(result["error"])
        self.assertFalse(self.path.exists())
        self.assertEqual(len(result["retired"]),1)
        self.assertGreater(result["freed_bytes"],0)
        self.assertTrue(all(args[0] not in ("prune","reset","clean","branch") or args == ("branch","--show-current") for args,_ in self.repo.calls))
        self.assertIn((("merge-base","--is-ancestor","a"*40,"b"*40),None),self.repo.calls)
        self.assertGreaterEqual(self.guard_mock.call_count,3)

    def test_dry_run_is_non_mutating_and_counts_eligible_bytes(self):
        result=self.run_maintenance(dry_run=True)
        self.assert_preserved(result)
        self.assertEqual(len(result["eligible"]),1)
        self.assertEqual(result["freed_bytes"],0)

    def test_open_task_not_known_absence_requires_verified_closed(self):
        self.assert_preserved(self.run_maintenance(open_tasks={1}))
        self.gh.issue_closed.assert_not_called()
        self.gh.issue_closed.return_value=False
        self.assert_preserved(self.run_maintenance())
        self.gh.issue_closed.side_effect=RuntimeError("GitHub unavailable")
        self.assert_preserved(self.run_maintenance())

    def test_unpublished_commit_and_missing_remote_are_preserved(self):
        self.repo.published=False
        self.assert_preserved(self.run_maintenance())
        self.repo.published=True
        self.repo.remote_error=True
        result=self.run_maintenance()
        self.assert_preserved(result)
        self.assertIn("Remote unavailable",result["error"])

    def test_every_merge_repair_and_preserved_attempt_is_excluded(self):
        for name in ("merge-round", "merge-round-824adfa2b98a", "1-finished-repair", "1-finished-attempt-a123456789ab"):
            self.make_candidate(name)
        result=self.run_maintenance(dry_run=True,limit=10)
        self.assertEqual([Path(x["path"]).name for x in result["eligible"]],["1-finished"])

    def test_explicit_active_and_preserved_registry_paths_are_preserved(self):
        self.assert_preserved(self.run_maintenance(protected_paths=[self.path]))
        task_registry=self.root/".task-checkouts.json"
        for data in ({"active":{"branch":{"path":str(self.path),"local_branch":"branch"}},"preserved":[]},
                     {"active":{},"preserved":[{"path":str(self.path)}]}):
            task_registry.write_text(json.dumps(data))
            self.assert_preserved(self.run_maintenance())
        task_registry.unlink()
        (self.root/".merge-checkouts.json").write_text(json.dumps({"active":str(self.path),"quarantined":[]}))
        self.assert_preserved(self.run_maintenance())

    def test_malformed_recovery_registry_fails_closed(self):
        for text in ("{partial", "[]", '{"active":{},"preserved":[{}]}'):
            (self.root/".task-checkouts.json").write_text(text)
            result=self.run_maintenance()
            self.assert_preserved(result)
            self.assertTrue(result["error"])

    def test_native_owner_or_heavy_lease_refuses_cleanup(self):
        self.guard_mock.side_effect=RuntimeError("PC lease or native owner")
        result=self.run_maintenance(lease_file="heavy.json")
        self.assert_preserved(result)
        self.assertIn("PC lease",result["error"])

    def test_dirty_staged_untracked_and_unique_ignored_files_are_preserved(self):
        for status in (" M source.py", "M  source.py", "?? artist.blend"):
            self.repo.status=status
            self.assert_preserved(self.run_maintenance())
        self.repo.status=""
        for ignored in ("Saved/ReviewScripts/model.blend\0", "Art/handmade.png\0", "Saved/Logs/only-diagnostic.log\0"):
            self.repo.ignored=ignored
            self.assert_preserved(self.run_maintenance())

    def test_only_source_backed_python_cache_is_regenerable(self):
        cache=self.path/"__pycache__";cache.mkdir()
        bytecode=cache/"source.cpython-311.pyc";bytecode.write_bytes(b"reproducible")
        self.repo.ignored="__pycache__/source.cpython-311.pyc\0"
        result=self.run_maintenance(dry_run=True)
        self.assertEqual(len(result["eligible"]),1)
        self.repo.ignored="__pycache__/missing.cpython-311.pyc\0"
        self.assert_preserved(self.run_maintenance())

    def test_index_flags_cannot_hide_unique_work(self):
        for flag in ("h source.py\0","S source.py\0"):
            self.repo.flags=flag
            self.assert_preserved(self.run_maintenance())

    def test_unregistered_or_linked_candidate_is_preserved(self):
        self.repo.paths.clear()
        self.assert_preserved(self.run_maintenance())
        self.repo.paths.add(self.path.resolve())
        original=storage._reparse
        with patch('storage._reparse',side_effect=lambda p: p==self.path or original(p)):
            self.assert_preserved(self.run_maintenance())

    def test_retention_keeps_recent_sources(self):
        self.assert_preserved(self.run_maintenance(retention_hours=24))

    def test_fresh_recheck_refuses_reopened_or_changed_candidate(self):
        self.gh.issue_closed.side_effect=[True,False]
        self.assert_preserved(self.run_maintenance())
        self.gh.issue_closed.side_effect=None
        original=storage._proof
        count=0
        def change_after_first(*args,**kw):
            nonlocal count
            count+=1
            result=original(*args,**kw)
            if count==1:
                self.repo.status="?? artist.blend"
            return result
        with patch('storage._proof',side_effect=change_after_first):
            self.assert_preserved(self.run_maintenance())

    def test_lfs_payload_requires_verified_shared_object(self):
        data=b"artist payload"
        target=self.path/"asset.blend";target.write_bytes(data)
        oid=hashlib.sha256(data).hexdigest()
        self.repo.lfs_files=[{"name":"asset.blend","oid":oid,"size":len(data)}]
        self.assert_preserved(self.run_maintenance())
        obj=self.repo.media/oid[:2]/oid[2:4]/oid
        obj.parent.mkdir(parents=True);obj.write_bytes(b"wrong payload!")
        self.assert_preserved(self.run_maintenance())
        obj.write_bytes(data)
        result=self.run_maintenance()
        self.assertEqual(len(result["retired"]),1)
        self.assertEqual(obj.read_bytes(),data,'shared LFS cache is never removed')

    def test_lfs_store_inside_checkout_is_not_recoverable(self):
        data=b"only payload";oid=hashlib.sha256(data).hexdigest()
        (self.path/"asset.blend").write_bytes(data)
        self.repo.lfs_files=[{"name":"asset.blend","oid":oid,"size":len(data)}]
        self.repo.media=self.path/"objects"
        obj=self.repo.media/oid[:2]/oid[2:4]/oid;obj.parent.mkdir(parents=True);obj.write_bytes(data)
        self.assert_preserved(self.run_maintenance())

    def test_lfs_pointer_only_copy_matches_recorded_identity(self):
        oid="a"*64
        self.repo.lfs_files=[{"name":"asset.blend","oid":oid,"size":12345}]
        pointer=self.path/"asset.blend"
        pointer.write_bytes(("version https://git-lfs.github.com/spec/v1\noid sha256:"+oid+"\nsize 12345\n").encode())
        self.assertEqual(len(self.run_maintenance(dry_run=True)["eligible"]),1)
        pointer.write_bytes(("version https://git-lfs.github.com/spec/v1\noid sha256:"+"b"*64+"\nsize 12345\n").encode())
        self.assert_preserved(self.run_maintenance())

    def test_batch_limit_is_bounded(self):
        self.make_candidate("2-finished")
        # Each task's branch has to name its own issue.
        original=self.repo.git
        def dispatch(*args,**kw):
            if args[:2]==("branch","--show-current"):
                return "claude/foreman-"+Path(kw["cwd"]).name
            return original(*args,**kw)
        with patch.object(self.repo,'git',side_effect=dispatch):
            result=self.run_maintenance(limit=1)
        self.assertEqual(len(result["retired"]),1)
        self.assertEqual(len(self.repo.paths),1)


if __name__ == '__main__':
    unittest.main()
