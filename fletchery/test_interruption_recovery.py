"""Interrupted rounds preserve their real candidate and require explicit continuation."""
import json
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

import test_dispatch_resilience as fixtures
from github import PullRequest
from loop import Engine
from recovery import resume_worktree
import rounds


class InterruptedRoundTests(unittest.IsolatedAsyncioTestCase):
    engine = fixtures.DispatchResilienceTests.engine

    def pr(self, n):
        return PullRequest(n, f'PR {n}', f'branch-{n}', 'Test-Staging', str(n % 10)*40, ['ready'])

    async def stopped(self):
        engine, gh, rt = self.engine(max_parallel=0, emergency_workers=0, merge_rounds=True)
        gh.prs = [self.pr(12), self.pr(13)]
        checkpoint = {'worktree':str(engine.ctx.repo.worktree_root/'saved-candidate'),
                      'branch':'fletchery/merge-round-1-saved','head':'a'*40}
        rt.script[('merger',0)] = {'state':'failed', 'summary':'Build and native checks passed; candidate saved.',
            'gave_up':'Computer Use reported the user stopped it with physical Escape.',
            'checkpoint':checkpoint, 'left_out':[{'pr':12,'why':'Compile repair failed'},
                {'pr':13,'why':'Final acceptance interrupted; candidate preserved'}]}
        await engine.once()
        return engine, gh, rt

    async def test_operator_stop_does_not_file_failure_or_exclude_healthy_heads(self):
        engine, gh, rt = await self.stopped()
        self.assertEqual(engine.state.merge_last['result'], 'interrupted')
        self.assertIn('physical Escape', engine.state.merge_last['stop_reason'])
        self.assertEqual(set(engine.state.merge_excluded), {'12'})
        self.assertEqual([p['pr'] for p in engine.state.merge_last['deferred']], [13])
        self.assertFalse(gh.tasks, 'operator stop does not create a code failure issue')
        self.assertFalse(engine.state.fix_attempts)
        self.assertFalse(engine.state.merge_resume['authorized'])

    async def test_restart_and_new_heads_do_not_bypass_user_stop(self):
        engine, gh, rt = await self.stopped()
        resumed = Engine(engine.ctx, engine.state_dir)
        resumed.check_accounts = lambda force=False: None
        gh.prs.append(self.pr(14))
        before = len(rt.calls)
        await resumed.once()
        self.assertEqual(len(rt.calls), before)
        self.assertIn('interrupted', resumed.merge_why.lower())

    async def test_authorized_resume_waits_for_pc_without_touching_saved_candidate(self):
        engine, gh, rt = await self.stopped()
        lease = Path(engine.cfg.project_root)/'heavy.json'
        lease.write_text(json.dumps({'token':'hearth-owner','purpose':'Hearth validation'}))
        engine.cfg.heavy_lease_file = str(lease)
        engine.merge_now()
        with patch('rounds.prepare_worktree') as prepare, patch('recovery.resume_worktree') as resume:
            await engine.once()
            prepare.assert_not_called()
            resume.assert_not_called()
        self.assertTrue(engine.state.merge_resume['authorized'])
        self.assertIn('lease', engine.merge_why)

    async def test_resume_uses_only_saved_prs_and_never_prepares_resets_checkout(self):
        engine, gh, rt = await self.stopped()
        gh.prs.append(self.pr(14))
        saved = Path(engine.state.merge_resume['checkpoint']['worktree'])
        saved.mkdir(parents=True)
        generated = saved/'generated.blend'
        generated.write_bytes(b'untracked artist asset')
        engine.state.merge_resume['progress']['steps'] = [{'name':'Build','state':'done'},
                                                          {'name':'Named acceptance','state':'waiting'}]
        engine.merge_now()
        rt.script[('merger',0)] = {'state':'landed','landed_pr':77,'merged':[13],'left_out':[], 'summary':'Saved candidate accepted'}
        with patch('rounds.prepare_worktree', side_effect=AssertionError('must never reset')) as prepare, \
             patch('recovery.resume_worktree', return_value=saved) as resume:
            await engine.once()
            prepare.assert_not_called()
            resume.assert_called_once()
        self.assertEqual(engine.state.merge_last['round'], 1)
        self.assertEqual(engine.state.merge_last['merged'], [13])
        self.assertEqual(engine.state.merge_resume, {})
        self.assertEqual(generated.read_bytes(), b'untracked artist asset')
        self.assertEqual(rounds.read_file(str(engine.state_dir/'merge-round.json'))['steps'][0]['state'],'done')

    async def test_transient_interruption_can_auto_resume_but_physical_stop_cannot(self):
        engine, gh, rt = self.engine(max_parallel=0, emergency_workers=0, merge_rounds=True)
        gh.prs = [self.pr(13)]
        rt.script[('merger',0)] = {'state':'interrupted', 'summary':'Saved candidate',
            'stop_reason':'Temporary connection interruption',
            'checkpoint':{'worktree':str(engine.ctx.repo.worktree_root/'saved'),
                          'branch':'fletchery/merge-round-1-saved','head':'a'*40},
            'left_out':[], 'deferred':[{'pr':13,'why':'Acceptance not attempted'}]}
        await engine.once()
        self.assertTrue(engine.state.merge_resume['authorized'])
        self.assertTrue(engine.state.merge_last['resume_authorized'])

    async def test_startup_removes_only_legacy_deferred_exclusions(self):
        engine, gh, rt = self.engine()
        engine.state.merge_last={'round':23}
        engine.state.merge_excluded={'12':{'round':23,'head':'a'*40,'why':'Compile repair failed'},
                                     '13':{'round':23,'head':'b'*40,'why':'Final acceptance interrupted'},
                                     '14':{'round':22,'head':'c'*40,'why':'Packaged graphics acceptance incomplete'}}
        engine.state.save(engine.state_dir/'state.json')
        recovered=Engine(engine.ctx,engine.state_dir)
        self.assertEqual(set(recovered.state.merge_excluded),{'12'})
        self.assertEqual([x['pr'] for x in recovered.state.merge_last['deferred']],[13])

    async def test_adopt_legacy_recovery_validates_round_heads_and_candidate(self):
        engine, gh, rt = await self.stopped()
        record=engine.state.merge_resume
        payload={**record,'stop_reason':'User stopped Computer Use with physical Escape',
                 'progress':{**record['progress'],'branch':record['checkpoint']['branch']}}
        with patch('recovery.resume_worktree') as guard:
            answer=engine.recover_interrupted(payload)
            self.assertTrue(answer['resume_authorized'])
            guard.assert_called_once()
        with self.assertRaises(ValueError):
            engine.recover_interrupted({**payload,'round':999})
        with patch('recovery.resume_worktree'):
            with self.assertRaises(ValueError):
                engine.recover_interrupted({**payload,'prs':{'99':'a'*40}})

    async def test_external_completion_waits_through_lease_gaps_and_ignores_stale_receipt(self):
        engine, gh, rt = await self.stopped()
        wait={'name':'Hearth final acceptance','request_id':'a'*32}
        engine.merge_now()
        engine.state.merge_resume['wait_for']=wait
        before=len(rt.calls)
        await engine.once()
        self.assertEqual(len(rt.calls),before)
        self.assertIn('Hearth final acceptance',engine.merge_why)
        from recovery import ready_signal
        receipt=engine.state_dir/('resume-ready-'+wait['request_id']+'.json')
        receipt.write_text(json.dumps({'request_id':'b'*32,'complete':True}))
        self.assertFalse(ready_signal(engine.state_dir,wait))
        receipt.write_text(json.dumps({'request_id':'a'*32,'complete':True}))
        self.assertTrue(ready_signal(engine.state_dir,wait))

    async def test_expired_handoff_resumes_saved_candidate_with_unverified_evidence(self):
        engine, gh, rt = await self.stopped()
        engine.merge_now()
        engine.state.merge_resume['wait_for'] = {'name':'Hearth', 'request_id':'a'*32,
                                                'started_at':'2000-01-01T00:00:00+00:00'}
        saved = Path(engine.state.merge_resume['checkpoint']['worktree'])
        rt.script[('merger',0)] = {'state':'landed','merged':[13], 'summary':'Verified directly'}
        with patch('recovery.resume_worktree',return_value=saved), patch('rounds.prepare_worktree',side_effect=AssertionError('no reset')):
            await engine.once()
        prompts = [call[2] for call in rt.calls if call[0]=='merger']
        self.assertIn('UNCONFIRMED', prompts[-1])
        self.assertIn('do not treat this timeout as a passing check', prompts[-1])
        self.assertEqual(engine.state.merge_last['result'],'landed')

    async def test_expired_handoff_still_waits_for_real_machine_owner(self):
        engine, gh, rt = await self.stopped()
        engine.merge_now()
        engine.state.merge_resume['wait_for'] = {'name':'Hearth','request_id':'a'*32,
                                                'started_at':'2000-01-01T00:00:00+00:00'}
        lease=Path(engine.cfg.project_root)/'heavy.json'
        lease.write_text(json.dumps({'token':'current-owner','purpose':'Native validation'}))
        engine.cfg.heavy_lease_file=str(lease)
        before=len(rt.calls)
        await engine.once()
        self.assertEqual(len(rt.calls),before)
        self.assertIn('lease',engine.merge_why)
        self.assertFalse(engine.state.merge_resume.get('wait_for'))
        self.assertFalse(engine.state.merge_resume['handoff_verification']['verified'])

    async def test_merge_now_releases_handoff_but_does_not_claim_acceptance(self):
        engine, gh, rt = await self.stopped()
        engine.state.merge_resume['wait_for']={'name':'Hearth','request_id':'a'*32}
        engine.merge_now()
        self.assertFalse(engine.state.merge_resume.get('wait_for'))
        self.assertIn('unconfirmed',engine.state.merge_resume['progress']['note'])
        self.assertFalse(engine.state.merge_resume['handoff_verification']['verified'])

    async def test_deadline_survives_restart_without_authorizing_operator_stop(self):
        engine, gh, rt = await self.stopped()
        from recovery import handoff_status
        wait={'name':'Hearth','request_id':'a'*32}
        self.assertFalse(handoff_status(engine.state_dir,wait)[0])
        engine.state.merge_resume['wait_for']=wait
        engine.state.save(engine.state_dir/'state.json')
        restarted=Engine(engine.ctx,engine.state_dir)
        self.assertEqual(restarted.state.merge_resume['wait_for']['started_at'],wait['started_at'])
        self.assertFalse(restarted.state.merge_resume['authorized'])



class SavedCheckoutGuardTests(unittest.TestCase):
    def test_handoff_deadline_is_exact_and_missing_signal_never_becomes_a_pass(self):
        from datetime import datetime, timezone, timedelta
        from recovery import handoff_status
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            started=datetime(2026,11,5,tzinfo=timezone.utc)
            wait={'name':'External review','request_id':'a'*32}
            self.assertFalse(handoff_status(root,wait,started)[0])
            self.assertFalse(handoff_status(root,wait,started+timedelta(seconds=899))[0])
            ready,reason=handoff_status(root,wait,started+timedelta(seconds=900))
            self.assertTrue(ready)
            self.assertIn('UNCONFIRMED',reason)
            self.assertEqual(wait['started_at'],started.isoformat())

    def test_registered_dirty_candidate_is_read_only_and_keeps_generated_files(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            saved = root/'candidate'; saved.mkdir(); (saved/'.git').write_text('gitdir: example')
            (saved/'generated.blend').write_bytes(b'generated')
            repo = Mock(worktree_root=root)
            repo.git.side_effect = ['fletchery/merge-round-23-saved','a'*40]
            checkpoint={'worktree':str(saved),'branch':'fletchery/merge-round-23-saved','head':'a'*40}
            with patch('rounds.checkout_guard') as guard, patch('rounds._registered_checkouts',return_value={saved.resolve()}):
                self.assertEqual(resume_worktree(repo,checkpoint,'lease.json'),saved.resolve())
                self.assertEqual(guard.call_count,2)
            self.assertEqual((saved/'generated.blend').read_bytes(),b'generated')
            self.assertEqual([c.args[:2] for c in repo.git.call_args_list], [('symbolic-ref','--quiet'),('rev-parse','HEAD')])

    def test_changed_head_or_branch_is_preserved_and_refused(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); saved=root/'candidate';saved.mkdir();(saved/'.git').write_text('gitdir: example')
            for actual in [('wrong-branch','a'*40),('fletchery/merge-round-23-saved','b'*40)]:
                repo=Mock(worktree_root=root);repo.git.side_effect=list(actual)
                with patch('rounds.checkout_guard'),patch('rounds._registered_checkouts',return_value={saved.resolve()}):
                    with self.assertRaises(RuntimeError):
                        resume_worktree(repo,{'worktree':str(saved),'branch':'fletchery/merge-round-23-saved','head':'a'*40})
                self.assertTrue(saved.exists())


if __name__ == '__main__':
    unittest.main()
