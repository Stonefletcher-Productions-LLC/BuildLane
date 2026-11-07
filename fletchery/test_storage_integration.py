"""Lean new text checkouts and safe, nonblocking retention scheduling."""
import asyncio
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from repo import Repo
from pipeline import Outcome
import test_dispatch_resilience as fixtures


class ThinCheckoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='fletchery-small-checkout-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base/'source'; self.source.mkdir()
        self.git('init','-q','-b','main')
        for name, text in {'Art/a.glb':'binary','Art/a.json':'{}','Art/sub/a.fbx':'binary',
                           'Content/a.uasset':'binary','Content/Strings/a.csv':'Key,Text',
                           'images/web.png':'keep web UI images','code.py':'print(1)'}.items():
            p=self.source/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(text)
        self.git('add','.')
        self.git('-c','user.name=Fixture','-c','user.email=fixture@localhost','commit','-qm','fixture')
        self.git('update-ref','refs/remotes/origin/main','HEAD')
        self.repo=Repo(self.source,self.base/'worktrees','main',thin_text_workers=True)

    def git(self,*args):
        return subprocess.run(['git',*args],cwd=self.source,text=True,capture_output=True,check=True).stdout

    def test_text_task_keeps_source_metadata_web_images_and_main_assets(self):
        path=self.repo.worktree(1,'text','claude/fletchery-1-text')
        for name in ['Art/a.json','Content/Strings/a.csv','code.py','images/web.png']:
            self.assertTrue((path/name).exists(),name)
        for name in ['Art/a.glb','Art/sub/a.fbx','Content/a.uasset']:
            self.assertFalse((path/name).exists(),name)
            self.assertTrue((self.source/name).exists(),'main checkout must be untouched')
        self.assertFalse(self.repo.dirty(path))
        self.assertEqual(self.git('status','--porcelain'),'')
        self.assertEqual(self.repo.worktree(1,'text','claude/fletchery-1-text'),path)

    def test_asset_task_and_opt_out_materialize_full_content(self):
        self.repo.asset_tasks={'2':{'paths':['Art/']}}
        path=self.repo.worktree(2,'art','claude/fletchery-2-art')
        self.assertTrue((path/'Art/a.glb').exists())
        self.repo.thin_text_workers=False
        path=self.repo.worktree(3,'full','claude/fletchery-3-full')
        self.assertTrue((path/'Content/a.uasset').exists())

    def test_existing_checkout_is_not_shrunk_and_asset_upgrade_keeps_unique_files(self):
        self.repo.thin_text_workers=False
        full=self.repo.worktree(4,'old','claude/fletchery-4-old')
        self.repo.thin_text_workers=True
        self.repo.worktree(4,'old','claude/fletchery-4-old')
        self.assertTrue((full/'Art/a.glb').exists())
        small=self.repo.worktree(5,'later-art','claude/fletchery-5-later-art')
        (small/'original.txt').write_text('keep unique work')
        self.repo.asset_tasks={'5':{'paths':['Art/']}}
        self.repo.worktree(5,'later-art','claude/fletchery-5-later-art')
        self.assertTrue((small/'Art/a.glb').exists())
        self.assertEqual((small/'original.txt').read_text(),'keep unique work')


class StorageSchedulingTests(unittest.IsolatedAsyncioTestCase):
    engine=fixtures.DispatchResilienceTests.engine

    def result(self,error=''):
        from loop import now
        return {'checked_at':now(),'error':error,'freed_bytes':0,'retired':[],'eligible':[],'skipped':[]}

    async def test_cleanup_runs_off_loop_with_zero_workers_and_persists_receipt(self):
        engine,gh,rt=self.engine(max_parallel=0)
        engine.ctx.repo.git=lambda *args,**kwargs:''
        with patch('storage.maintain',return_value=self.result()) as cleanup:
            await engine.once()
            cleanup.assert_called_once()
        self.assertTrue((engine.state_dir/'storage-cleanup.json').exists())
        self.assertFalse(rt.calls)

    async def test_busy_work_pause_and_draining_do_not_start_cleanup(self):
        engine,_,_=self.engine()
        engine.ctx.repo.git=lambda *args,**kwargs:''
        future=asyncio.get_running_loop().create_future()
        with patch('storage.maintain') as cleanup:
            engine.state.held=True;engine._maybe_storage({});self.assertIsNone(engine._storage_job)
            engine.state.held=False;engine._maybe_storage({},draining=True);self.assertIsNone(engine._storage_job)
            engine.round_job=future;engine._maybe_storage({});self.assertIsNone(engine._storage_job)
            engine.round_job=None;engine.jobs[future]=(1,None,None);engine._maybe_storage({});self.assertIsNone(engine._storage_job)
            cleanup.assert_not_called()
        future.set_result(Outcome(1,'blocked'))

    async def test_landed_round_cleans_while_workers_run_but_not_during_a_round(self):
        engine,_,_=self.engine()
        engine.ctx.repo.git=lambda *args,**kwargs:''
        future=asyncio.get_running_loop().create_future()
        trim={'checked_at':'x','freed_bytes':0,'trimmed':[],'skipped':[],'error':''}
        with patch('storage.maintain',return_value=self.result()) as cleanup, patch('storage.trim_merge_checkouts',return_value=trim) as trimmer:
            engine.jobs[future]=(1,None,None)
            engine._storage_after_round=True
            engine.round_job=future;engine._maybe_storage({});self.assertIsNone(engine._storage_job)
            engine.round_job=None;engine._maybe_storage({})
            await engine._storage_job
            cleanup.assert_called_once();trimmer.assert_called_once()
            self.assertFalse(engine._storage_after_round)
            engine._maybe_storage({})  # back to the idle-only hourly schedule
            self.assertEqual(cleanup.call_count,1)
        self.assertTrue((engine.state_dir/'merge-trim.json').exists())
        future.set_result(Outcome(1,'blocked'))

    async def test_busy_pc_retry_is_bounded_and_manual_request_survives(self):
        engine,_,_=self.engine()
        engine.ctx.repo.git=lambda *args,**kwargs:''
        engine.request_storage_cleanup()
        with patch('storage.maintain',return_value=self.result('PC heavy lease is held')) as cleanup:
            engine._maybe_storage({});await engine._storage_job
            self.assertTrue(engine.state.storage_requested)
            for _ in range(5):engine._maybe_storage({})
            self.assertEqual(cleanup.call_count,1)

    async def test_reload_waits_for_owned_cleanup_to_finish(self):
        engine,_,_=self.engine(max_parallel=0)
        engine.ctx.repo.git=lambda *args,**kwargs:''
        entered,release=threading.Event(),threading.Event()
        def cleanup(*args,**kwargs):
            entered.set();release.wait(5);return self.result()
        with patch('storage.maintain',side_effect=cleanup):
            running=asyncio.create_task(engine.run_forever())
            await asyncio.to_thread(entered.wait,2)
            self.assertTrue(entered.is_set())
            engine.request_restart('test reload')
            await asyncio.sleep(.03)
            self.assertFalse(running.done())
            release.set()
            await asyncio.wait_for(running,3)

    async def test_manual_cleanup_gets_bounded_idle_opportunity_before_new_batch(self):
        from github import PullRequest
        engine,gh,_=self.engine(max_parallel=0,merge_rounds=True)
        engine.ctx.repo.git=lambda *args,**kwargs:''
        gh.prs=[PullRequest(12,'Ready','ready','Test-Staging','a'*40,['ready'])]
        engine.request_storage_cleanup()
        with patch('storage.maintain',return_value=self.result()):
            engine.dispatch()
            self.assertIsNone(engine.round_job)
            self.assertIn('cleanup',engine.merge_why)
            await engine._storage_job
        engine.cfg.auto_cleanup=False
        engine.dispatch()
        self.assertIsNotNone(engine.round_job)
        await engine.round_job


if __name__=='__main__':
    unittest.main()
