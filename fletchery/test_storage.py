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
        self.root = Path(self.tmp.name).resolve()/"worktrees"
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

    def test_linked_root_ancestor_defers_cleanup(self):
        original = storage._reparse
        with patch('storage._reparse', side_effect=lambda path: path == self.root.parent or original(path)):
            result = self.run_maintenance()
        self.assert_preserved(result)
        self.assertIn('root is linked', result['error'])

    @unittest.skipUnless(__import__('os').name == 'nt', 'Windows short paths')
    def test_windows_short_path_alias_is_safe(self):
        import ctypes
        buffer = ctypes.create_unicode_buffer(32768)
        length = ctypes.windll.kernel32.GetShortPathNameW(str(self.root), buffer, len(buffer))
        self.assertGreater(length, 0)
        self.repo.worktree_root = Path(buffer.value)
        result = self.run_maintenance(dry_run=True)
        self.assertFalse(result['error'], result)
        self.assertEqual(len(result['eligible']), 1, result)

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

    def test_explicit_preserved_and_open_active_registry_paths_are_preserved(self):
        self.assert_preserved(self.run_maintenance(protected_paths=[self.path]))
        task_registry=self.root/".task-checkouts.json"
        task_registry.write_text(json.dumps({"active":{},"preserved":[{"path":str(self.path)}]}))
        self.assert_preserved(self.run_maintenance())
        task_registry.write_text(json.dumps({"active":{self.repo.branch:{"path":str(self.path),"local_branch":self.repo.branch}},"preserved":[]}))
        self.assert_preserved(self.run_maintenance(open_tasks={1}))
        self.assert_preserved(self.run_maintenance(protected_paths=[self.path]))
        task_registry.unlink()
        (self.root/".merge-checkouts.json").write_text(json.dumps({"active":str(self.path),"quarantined":[]}))
        self.assert_preserved(self.run_maintenance())

    def test_finished_task_active_record_is_retired_and_forgotten(self):
        task_registry=self.root/".task-checkouts.json"
        other={"path":str(self.root/"2-other"),"local_branch":"claude/foreman-2-other"}
        task_registry.write_text(json.dumps({"active":{self.repo.branch:{"path":str(self.path),"local_branch":self.repo.branch},
                                                       "claude/foreman-2-other":other},"preserved":[]}))
        result=self.run_maintenance()
        self.assertEqual(len(result["retired"]),1,result)
        self.assertFalse(self.path.exists())
        self.assertEqual(json.loads(task_registry.read_text())["active"],{"claude/foreman-2-other":other})

    def test_final_attempt_checkout_of_a_finished_task_is_retired(self):
        attempt=self.make_candidate("1-finished-attempt-a123456789ab")
        self.repo.branch="fletchery/task-1-attempt-a123456789ab"
        (self.root/".task-checkouts.json").write_text(json.dumps({"active":{"claude/foreman-1-finished":{"path":str(attempt),
            "local_branch":self.repo.branch}},"preserved":[{"path":str(self.path)}]}))
        result=self.run_maintenance(limit=10)
        self.assertEqual([Path(x["path"]).name for x in result["retired"]],["1-finished-attempt-a123456789ab"],result)
        self.assertTrue(self.path.exists(),"the preserved first attempt stays")

    def test_merged_pr_head_counts_as_published_after_history_rewrite(self):
        self.repo.published=False  # not an ancestor of the rewritten base
        self.gh.merged_heads.return_value={"c"*40}
        self.assert_preserved(self.run_maintenance())
        self.gh.merged_heads.return_value={self.repo.head}
        result=self.run_maintenance()
        self.assertEqual(len(result["retired"]),1,result)
        self.gh.merged_heads.assert_called_with("claude/foreman-1-finished")

    def test_unreal_build_outputs_are_regenerable_but_saved_is_not(self):
        for name in ("Intermediate/Build/x.obj","Binaries/Win64/x.dll","DerivedDataCache/a.udd","Plugins/LSAnim/Intermediate/x.obj"):
            self.assertTrue(storage._regenerable_ignored(name,self.path),name)
        for name in ("Saved/Logs/x.log","Content/Intermediate/x.uasset","Plugins/Intermediate/x"):
            self.assertFalse(storage._regenerable_ignored(name,self.path),name)


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

    def test_disabled_age_limit_ignores_clock_skew_but_configured_retention_preserves(self):
        with patch('storage.time.time', return_value=0):
            result=self.run_maintenance(dry_run=True)
            self.assertEqual(len(result['eligible']),1,result)
            self.assert_preserved(self.run_maintenance(retention_hours=24))

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
        result=self.run_maintenance(dry_run=True)
        self.assertEqual(len(result["eligible"]),1,result)
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


class MergeTrimTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix="fletchery-trim-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve()/"worktrees";self.root.mkdir()
        self.repo=FixtureRepo(self.root)
        self.guard=patch("rounds.checkout_guard");self.guard_mock=self.guard.start();self.addCleanup(self.guard.stop)
        self.dirs={}
        for name in ("merge-round","merge-round-0ce9e7824c26","merge-round-24-remainder","1-finished"):
            path=self.root/name
            for sub in ("Intermediate/Build","Binaries/Win64","DerivedDataCache","Saved/Logs","Source"):
                (path/sub).mkdir(parents=True);(path/sub/"f.bin").write_bytes(b"x"*10)
            self.dirs[name]=path
        (self.root/"merge-round-0ce9e7824c26.recovery.json").write_text("{}")
        (self.root/".merge-checkouts.json").write_text(json.dumps({"active":str(self.dirs["merge-round-0ce9e7824c26"]),"quarantined":[]}))

    def test_idle_merge_checkouts_lose_only_build_outputs(self):
        result=storage.trim_merge_checkouts(self.repo,protected_paths=[self.dirs["merge-round-24-remainder"]])
        self.assertFalse(result["error"],result)
        old=self.dirs["merge-round"]
        for sub in ("Intermediate","Binaries","DerivedDataCache"):
            self.assertFalse((old/sub).exists(),sub)
        self.assertTrue((old/"Saved/Logs/f.bin").exists() and (old/"Source/f.bin").exists())
        self.assertEqual(result["freed_bytes"],30)
        for keep in ("merge-round-0ce9e7824c26","merge-round-24-remainder","1-finished"):
            self.assertTrue((self.dirs[keep]/"Intermediate/Build/f.bin").exists(),keep)

    def test_busy_checkout_or_lease_keeps_everything(self):
        self.guard_mock.side_effect=RuntimeError("Merge checkout still owned by running processes")
        result=storage.trim_merge_checkouts(self.repo)
        self.assertEqual(result["freed_bytes"],0)
        self.assertTrue((self.dirs["merge-round"]/"Intermediate/Build/f.bin").exists())

    def test_dry_run_changes_nothing(self):
        result=storage.trim_merge_checkouts(self.repo,dry_run=True)
        self.assertTrue(result["trimmed"])
        self.assertTrue((self.dirs["merge-round"]/"Binaries/Win64/f.bin").exists())


class MergeRepo(FixtureRepo):
    def __init__(self, root):
        super().__init__(root)
        self.branch = "fletchery/merge-round-20-x"
        self.dirty = {}  # checkout name -> porcelain -z status

    def git(self, *args, cwd=None, check=True):
        if args[0] == "status":
            self.calls.append((args, cwd))
            return self.dirty.get(Path(cwd).name, "")
        if args[0] == "diff":
            self.calls.append((args, cwd))
            return "diff --git a/x b/x" if self.dirty.get(Path(cwd).name) else ""
        return super().git(*args, cwd=cwd, check=check)


class MergeRetireTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix="fletchery-retire-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve()/"worktrees";self.root.mkdir()
        self.backup=Path(self.tmp.name).resolve()/"backup"
        self.repo=MergeRepo(self.root)
        self.guard=patch("rounds.checkout_guard");self.guard_mock=self.guard.start();self.addCleanup(self.guard.stop)
        names=["merge-round","merge-round-111111111111","merge-round-222222222222","merge-round-333333333333","merge-round-444444444444"]
        self.paths={}
        quarantined=[]
        for day,name in enumerate(names,start=1):
            path=self.root/name;(path/"Content").mkdir(parents=True)
            (path/".git").write_text("gitdir: fixture");(path/"Content/a.uasset").write_bytes(b"x"*100)
            self.repo.paths.add(path.resolve());self.paths[name]=path
            if name!="merge-round-444444444444":
                quarantined.append({"path":str(path),"at":f"2026-11-0{day}T00:00:00+00:00","reason":"dirty"})
        (self.root/".merge-checkouts.json").write_text(json.dumps({"active":str(self.paths["merge-round-444444444444"]),"quarantined":quarantined}))

    def run_retire(self,**kw):
        kw.setdefault("retention_hours",0)
        return storage.retire_merge_checkouts(self.repo,**kw)

    def names(self,result):
        return sorted(Path(r["path"]).name for r in result["retired"])

    def test_oldest_clean_checkouts_retire_and_newest_and_active_stay(self):
        result=self.run_retire(keep=2)
        self.assertFalse(result["error"],result)
        self.assertEqual(self.names(result),["merge-round","merge-round-111111111111"])
        for name in ("merge-round-222222222222","merge-round-333333333333","merge-round-444444444444"):
            self.assertTrue(self.paths[name].exists(),name)
        self.assertFalse(any(a[0]=="prune" or a[:2]==("branch","-D") for a,_ in self.repo.calls))

    def test_dirty_checkout_needs_a_backup_folder_and_gets_its_files_copied(self):
        changed=self.paths["merge-round"]/"Content/new.uasset";changed.write_bytes(b"generated")
        self.repo.dirty["merge-round"]="?? Content/new.uasset\0 M Content/a.uasset\0"
        result=self.run_retire(keep=3)
        self.assertEqual(result["retired"],[])
        self.assertTrue(self.paths["merge-round"].exists())
        result=self.run_retire(keep=3,backup_dir=str(self.backup))
        self.assertEqual(self.names(result),["merge-round"],result)
        saved=Path(result["retired"][0]["backup"])
        self.assertEqual((saved/"files/Content/new.uasset").read_bytes(),b"generated")
        self.assertEqual((saved/"files/Content/a.uasset").read_bytes(),b"x"*100)
        self.assertIn("fletchery/merge-round-20-x",(saved/"head.txt").read_text())
        self.assertTrue((saved/"tracked-changes.patch").read_text().startswith("diff --git"))

    def test_busy_leased_detached_or_recent_checkouts_are_kept(self):
        self.guard_mock.side_effect=RuntimeError("Merge checkout still owned by running processes")
        self.assertEqual(self.run_retire(keep=0)["retired"],[])
        self.guard_mock.side_effect=None
        self.repo.branch=""
        self.assertEqual(self.run_retire(keep=0)["retired"],[])
        self.repo.branch="fletchery/merge-round-20-x"
        self.assertEqual(self.run_retire(keep=0,retention_hours=24*365*100)["retired"],[])
        for path in self.paths.values():
            self.assertTrue(path.exists())

    def test_protected_and_unregistered_checkouts_are_kept(self):
        self.repo.paths.discard(self.paths["merge-round"].resolve())
        result=self.run_retire(keep=0,protected_paths=[self.paths["merge-round-111111111111"]])
        self.assertEqual(self.names(result),["merge-round-222222222222","merge-round-333333333333"])

    def test_dry_run_changes_nothing(self):
        result=self.run_retire(keep=0,dry_run=True)
        self.assertEqual(len(result["retired"]),4)
        self.assertTrue(all(p.exists() for p in self.paths.values()))
        self.assertFalse(any(a[:2]==("worktree","remove") for a,_ in self.repo.calls))


if __name__ == '__main__':
    unittest.main()
