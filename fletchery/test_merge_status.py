"""Finished rounds are history, not the next merge's current status."""
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import merge


class MergeStatusTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="fletchery-merge-status-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.progress = self.root / "round.json"
        self.lease = self.root / "heavy.json"
        self.now = datetime(2026, 11, 5, 8, tzinfo=timezone.utc)

    def save(self, **changes):
        data = {"round": "19", "state": "failed", "started": "2026-11-05T00:00:00Z",
                "updated": "2026-11-05T01:00:00Z", "prs": [100, 101], "worktree": str(self.root),
                "branch": "round-19", "steps": [{"name": "Build", "state": "failed"}],
                "note": "Agent stopped; candidate preserved"}
        data.update(changes)
        self.progress.write_text(json.dumps(data), encoding="utf-8")

    def test_every_terminal_round_is_detached_from_current_status(self):
        for state in merge.TERMINAL_STATES:
            with self.subTest(state=state):
                self.save(state=state)
                with patch("merge.in_branch") as query:
                    view = merge.view(self.progress, self.lease, {100: "a" * 40}, self.now)
                self.assertFalse(view["active"])
                self.assertEqual(view["state"], "idle")
                self.assertEqual((view["round"], view["prs"], view["steps"], view["in_branch"]), ("", [], [], {}))
                self.assertFalse(view["stale"])
                self.assertNotIn("FAILED", view["headline"])
                self.assertEqual(view["previous"]["round"], "19")
                self.assertEqual(view["previous"]["duration_seconds"], 3600)
                query.assert_not_called()

    def test_finished_duration_is_frozen_and_explicit_end_beats_later_metadata(self):
        self.save(ended="2026-11-05T00:30:00Z", updated="2026-11-06T01:00:00Z")
        first = merge.view(self.progress, self.lease, now=self.now)
        later = merge.view(self.progress, self.lease, now=datetime(2026, 11, 9, tzinfo=timezone.utc))
        self.assertEqual(first["previous"], later["previous"])
        self.assertEqual(first["previous"]["duration_seconds"], 1800)
        self.assertEqual(first["previous"]["ended"], "2026-11-05T00:30:00Z")

    def test_historical_failure_does_not_hide_current_reservation(self):
        self.save()
        self.lease.write_text(json.dumps({"token": "held", "purpose": "Hair review", "pid": 123,
                                         "workspaces": str(self.root), "acquiredUtc": "2026-11-05T07:50:00Z"}))
        view = merge.view(self.progress, self.lease, now=self.now)
        self.assertIn("Hair review", view["headline"])
        self.assertIn("No merge round running", view["headline"])
        self.assertEqual(view["previous"]["note"], "Agent stopped; candidate preserved")

    def test_failed_step_in_a_live_round_remains_current(self):
        self.save(state="running", updated="2026-11-05T07:59:00Z")
        with patch("merge.in_branch", return_value=True):
            view = merge.view(self.progress, self.lease, {100: "a" * 40}, self.now)
        self.assertTrue(view["active"])
        self.assertEqual(view["state"], "failed")
        self.assertIsNone(view["previous"])
        self.assertEqual(view["prs"], [100, 101])
        self.assertIn("FAILED at Build", view["headline"])

    def test_failure_blocks_landing_despite_other_reported_activity(self):
        for activity in ("running", "waiting"):
            with self.subTest(activity=activity):
                self.save(state="running", steps=[
                    {"name": "Package", "state": activity},
                    {"name": "Game specs", "state": "failed"},
                    {"name": "Landing", "state": "todo"}])
                view = merge.view(self.progress, self.lease, now=self.now)
                self.assertEqual(view["state"], "failed")
                self.assertEqual(view["failed_steps"], ["Game specs"])
                self.assertIn("FAILED at Game specs", view["headline"])
                self.assertIn("revalidate", view["next"])
                self.assertEqual(view["reported_activity"], ["Package"] if activity == "running" else [])

    def test_revalidation_clears_failure_only_when_progress_changes(self):
        self.save(state="running", steps=[{"name": "Game specs", "state": "running"},
                                          {"name": "Landing", "state": "todo"}])
        view = merge.view(self.progress, self.lease, now=self.now)
        self.assertEqual(view["state"], "running")
        self.assertEqual(view["failed_steps"], [])
        self.assertEqual(view["next"], "Landing")

    def test_excluded_candidate_failure_stays_visible_without_blocking_revalidation(self):
        self.save(state="running", steps=[
            {"name": "Build eight-PR candidate", "state": "running"},
            {"name": "Low-VRAM old candidate", "state": "failed", "required": False},
            {"name": "Game specs eight-PR candidate", "state": "todo"}])
        view = merge.view(self.progress, self.lease, now=self.now)
        self.assertEqual(view["state"], "running")
        self.assertEqual(view["failed_steps"], [])
        self.assertEqual(view["steps"][1]["state"], "failed")
        self.assertIs(view["steps"][1]["required"], False)
        self.assertEqual(view["next"], "Game specs eight-PR candidate")

    def test_dashboard_reads_progress_without_waiting_for_scheduler_or_git(self):
        from types import SimpleNamespace
        from server import live_merge_progress
        fm = SimpleNamespace(state_dir=self.root, cfg=SimpleNamespace(
            merge_round_file=str(self.progress), heavy_lease_file=str(self.lease)))
        self.save(state="running", steps=[{"name": "Game specs", "state": "failed"}])
        with patch("merge.in_branch") as git:
            self.assertEqual(live_merge_progress(fm)["state"], "failed")
            self.save(state="running", prs=[100], steps=[{"name": "Rebuild remainder", "state": "running"}])
            current = live_merge_progress(fm)
            self.assertEqual(current["prs"], [100])
            self.assertIn("Rebuild remainder", current["headline"])
            self.assertEqual(current["state"], "running")
            git.assert_not_called()

    def test_invalid_historical_timestamps_do_not_invent_a_duration(self):
        for started, ended in [("bad", "also bad"), ("2026-11-05T02:00:00Z", "2026-11-05T01:00:00Z")]:
            with self.subTest(started=started):
                self.save(started=started, ended=ended)
                view = merge.view(self.progress, self.lease, now=self.now)
                self.assertIsNone(view["previous"]["duration_seconds"])

    def logs(self, name="Auto-Game-final.log", text="START RunM1Test at 07:58:00\nPASS RunM1Test (56s)\nSTART RunM2Test at 07:59:00\n"):
        folder=self.root/"Saved"/"MergeRound19"
        folder.mkdir(parents=True,exist_ok=True)
        log=folder/name
        log.write_text(text,encoding="utf-8")
        os.utime(log,(self.now.timestamp()-5,self.now.timestamp()-5))
        return log

    def test_real_log_checks_activity_and_safe_full_log_link(self):
        self.save(state="running",updated="2026-11-05T07:59:00Z",steps=[{"name":"Game checks","state":"running"}])
        log=self.logs()
        view=merge.view(self.progress,self.lease,now=self.now,worktree_root=self.root.parent)
        step=view['steps'][0]
        self.assertEqual([(c['name'],c['state']) for c in step['checks']], [('RunM1Test','done'),('RunM2Test','running')])
        self.assertEqual(step['checks'][0]['duration_seconds'],56)
        self.assertEqual(view['activity']['health'],'active')
        self.assertEqual(view['activity']['age_seconds'],5)
        self.assertIn('/api/merge/log?',step['files'][0]['url'])
        self.assertIn('START RunM2Test',merge.log_text(self.progress,self.root.parent,'19',log.name))
        self.assertEqual(step['state'],'running', 'log parsing never grants phase acceptance')

    def test_explicit_activity_is_not_overwritten_by_prior_log_output(self):
        self.save(state='running',updated='2026-11-05T07:59:00Z',steps=[{'name':'Game checks','state':'failed',
            'updated':'2026-11-05T07:59:00Z','activity':[{'line':'Repair check failed','at':'2026-11-05T07:59:00Z'}],
            'checks':[{'name':'Current repaired check','state':'failed'}]}])
        self.logs()
        view=merge.view(self.progress,self.lease,now=self.now,worktree_root=self.root.parent)
        step=view['steps'][0]
        self.assertEqual(step['activity'][0]['line'],'Repair check failed')
        self.assertEqual(step['checks'][0]['name'],'Current repaired check')
        self.assertEqual(step['activity_source'],'Progress checkpoint')
        self.assertIn('Auto-Game-final.log',step['log_source'])

    def test_refresh_reads_checkpoint_without_git_and_keeps_membership_only_same_round(self):
        self.save(state="running",updated="2026-11-05T07:59:00Z",steps=[{"name":"Build","state":"running"}])
        cached={'round':'19','in_branch':{'100':True}}
        with patch('merge.in_branch',side_effect=AssertionError('status must not spawn git')):
            view=merge.refresh(self.progress,self.lease,cached,self.root.parent,self.now)
            self.assertEqual(view['steps'][0]['state'],'running')
            self.assertEqual(view['in_branch'],{'100':True})
            self.save(round='20',state='running',updated='2026-11-05T07:59:00Z')
            changed=merge.refresh(self.progress,self.lease,cached,self.root.parent,self.now)
            self.assertNotEqual(changed['in_branch'],cached['in_branch'])

    def test_explicit_step_details_preserved_and_failed_step_dominates_running(self):
        self.save(state='running',updated='2026-11-05T07:59:00Z',steps=[
            {'name':'Package','state':'running'},
            {'name':'Game checks','state':'failed','started':'2026-11-05T07:00:00Z','duration_seconds':52,
             'message':'Repair needed','checks':[{'name':'Reconnect','state':'failed'}]}])
        view=merge.view(self.progress,self.lease,now=self.now)
        self.assertEqual(view['state'],'failed')
        self.assertIn('FAILED at Game checks',view['headline'])
        self.assertIn('revalidate',view['next'])
        self.assertEqual(view['steps'][1]['checks'][0]['name'],'Reconnect')
        self.assertEqual(view['steps'][1]['duration_seconds'],52)
        self.assertEqual(view['steps'][1]['message'],'Repair needed')

    def test_stale_checkpoint_with_reservation_is_not_reported_as_proven_activity(self):
        self.save(state='running')
        self.lease.write_text(json.dumps({'token':'held','purpose':'Native test','pid':123}))
        view=merge.view(self.progress,self.lease,now=self.now)
        self.assertEqual(view['activity']['health'],'possibly_stuck')
        self.assertIn('Native test',view['pc']['what'])

    def test_log_reader_refuses_traversal_wrong_round_unapproved_root_and_links(self):
        self.save(state='running',updated='2026-11-05T07:59:00Z')
        log=self.logs()
        for number,name,root in [('20',log.name,self.root.parent),('19','../secret.log',self.root.parent),
                                 ('19',str(log),self.root.parent),('19',log.name,self.root/'other')]:
            with self.subTest(number=number,name=name):
                with self.assertRaises((OSError,ValueError)):
                    merge.log_text(self.progress,root,number,name)
        with patch('merge._linked',side_effect=lambda p:p.name=='Saved'):
            with self.assertRaises((OSError,ValueError)):
                merge.log_text(self.progress,self.root.parent,'19',log.name)

    def test_large_log_read_is_bounded_and_truncation_is_visible(self):
        self.save(state='running',updated='2026-11-05T07:59:00Z')
        log=self.logs(text='x'*2_000_000+'\nEND')
        view=merge.view(self.progress,self.lease,now=self.now,worktree_root=self.root.parent)
        self.assertLess(sum(len(item['line']) for item in view['activity']['lines']),7000)
        text=merge.log_text(self.progress,self.root.parent,'19',log.name)
        self.assertLess(len(text),1_049_000)
        self.assertIn('earlier output omitted',text)
        self.assertTrue(text.endswith('END'))

    def test_dashboard_poll_reads_current_checkpoint_without_waiting_for_scheduler(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from config import Config
        from server import Dashboard
        self.save(state='running',updated='2026-11-05T07:59:00Z',steps=[{'name':'Build','state':'running'}])
        cfg=Config(merge_round_file=str(self.progress),heavy_lease_file=str(self.lease),worktree_root=str(self.root.parent))
        fm=SimpleNamespace(cfg=cfg,state_dir=self.root,
            snapshot={'tasks':[],'merge_view':{'round':'19','steps':[{'name':'Build','state':'todo'}],'in_branch':{}}},
            ctx=SimpleNamespace(live=None),state=SimpleNamespace(held=False,merge_held=False,merge_last={},merge_resume={},signed_out=False),
            round_info={},merge_why='',storage_view=lambda:{},agents_view=lambda:{},restart_status=lambda:'',
            emergency_active=set(),emergency_why='',next_pass_at='',passing=False)
        dash=Dashboard(fm,0)
        with patch('merge.in_branch',side_effect=AssertionError('must not call git')):
            first=dash.status()
            self.save(state='running',updated='2026-11-05T08:00:00Z',steps=[{'name':'Build','state':'done'}])
            second=dash.status()
        self.assertEqual(first['merge_view']['steps'][0]['state'],'running')
        self.assertEqual(second['merge_view']['steps'][0]['state'],'done')
        self.assertEqual(fm.snapshot['merge_view']['steps'][0]['state'],'todo','shared snapshot must not be mutated')

    def test_log_endpoint_is_plain_text_and_rejects_wrong_host_and_round(self):
        from config import Config
        from server import Dashboard
        from types import SimpleNamespace
        from urllib.request import Request, urlopen
        from urllib.error import HTTPError
        self.save(state='running',updated='2026-11-05T07:59:00Z')
        log=self.logs(text='<script>alert("log")</script>\nPASS RunM1Test (56s)')
        cfg=Config(merge_round_file=str(self.progress),worktree_root=str(self.root.parent))
        dash=Dashboard(SimpleNamespace(cfg=cfg,state_dir=self.root),0)
        dash.start()
        dash.port=dash.server.server_address[1]
        self.addCleanup(dash.server.server_close)
        self.addCleanup(dash.stop)
        url=f'http://127.0.0.1:{dash.port}/api/merge/log?round=19&name={log.name}'
        with urlopen(url,timeout=3) as response:
            self.assertEqual(response.headers.get_content_type(),'text/plain')
            self.assertEqual(response.headers['X-Content-Type-Options'],'nosniff')
            self.assertIn('<script>',response.read().decode())
        with self.assertRaises(HTTPError) as denied:
            urlopen(Request(url,headers={'Host':'evil.example'}),timeout=3)
        self.assertEqual(denied.exception.code,403)
        with self.assertRaises(HTTPError) as wrong_round:
            urlopen(url.replace('round=19','round=20'),timeout=3)
        self.assertEqual(wrong_round.exception.code,404)

    def test_parallel_and_isolated_retry_results_update_the_same_check(self):
        checks=merge._checks((
            'START group RunM4bTest, RunM5aTest at 13:23:40 (in parallel)',
            'START RunM4bTest at 13:23:43 (in parallel)',
            'START RunM5aTest at 13:23:44 (in parallel)',
            'FAIL RunM4bTest (68s) (alone, second try)',
            'PASS RunM5aTest (72s) (rerun alone)',
        ))
        self.assertEqual([c['name'] for c in checks],['RunM4bTest','RunM5aTest'])
        self.assertEqual([c['state'] for c in checks],['failed','done'])
        self.assertEqual(checks[0]['duration_seconds'],68)
        self.assertEqual(checks[0]['message'],'(alone, second try)')
        self.assertEqual(checks[0]['started'],'13:23:43')
        self.assertEqual(checks[1]['message'],'(rerun alone)')

    def test_check_parser_preserves_punctuation_and_latest_retry_outcome(self):
        name='Reconnect: player #2 / save-load [host]'
        checks=merge._checks((f'START {name} at 13:00:00',f'FAIL {name} (2s)',
            f'START {name} at 13:00:03 (alone, second try)',f'PASS {name} (2.5s) (alone)'))
        self.assertEqual(len(checks),1)
        self.assertEqual(checks[0]['name'],name)
        self.assertEqual(checks[0]['state'],'done')
        self.assertEqual(checks[0]['duration_seconds'],2.5)
        self.assertEqual(checks[0]['started'],'13:00:03')
        self.assertEqual(checks[0]['message'],'(alone)')

    @unittest.skipUnless(os.name == 'nt', 'Windows dashboard scheduling')
    def test_dashboard_thread_can_stay_responsive_without_raising_worker_process_priority(self):
        import ctypes
        import threading
        from server import dashboard_thread_priority
        k=ctypes.windll.kernel32
        k.GetCurrentProcess.restype=ctypes.c_void_p
        k.GetCurrentThread.restype=ctypes.c_void_p
        k.GetPriorityClass.argtypes=[ctypes.c_void_p]
        k.SetPriorityClass.argtypes=[ctypes.c_void_p,ctypes.c_uint]
        k.GetThreadPriority.argtypes=[ctypes.c_void_p]
        handle=k.GetCurrentProcess()
        original=k.GetPriorityClass(handle)
        results=[]
        try:
            self.assertTrue(k.SetPriorityClass(handle,0x00004000))
            def serve():
                dashboard_thread_priority()
                results.append((k.GetPriorityClass(handle),k.GetThreadPriority(k.GetCurrentThread())))
            thread=threading.Thread(target=serve)
            thread.start();thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(results,[(0x00004000,2)])
            self.assertEqual(k.GetPriorityClass(handle),0x00004000)
        finally:
            k.SetPriorityClass(handle,original)

    def test_non_object_progress_is_ignored(self):
        self.progress.write_text("[]")
        self.assertFalse(merge.view(self.progress, self.lease)["active"])


if __name__ == "__main__":
    unittest.main()
