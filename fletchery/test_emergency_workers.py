"""Independent bounded build/merge repair capacity; no real sessions or services."""
import asyncio
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import test_dispatch_resilience as fixtures
from config import Config
from github import Feedback, PullRequest
from graph import Task, PR, BLOCKED, label_for
from loop import Engine, State, now, today
from pipeline import Outcome
from live import Live
from server import Dashboard
import projects
import rounds


class EmergencyWorkerTests(unittest.IsolatedAsyncioTestCase):
    def engine(self, *args, **kwargs):
        engine, gh, rt = fixtures.DispatchResilienceTests.engine(self, *args, **kwargs)
        engine.ctx.run_gates = lambda *a, **kw: []
        return engine, gh, rt

    def repair(self, n=1):
        return Task(n, 'Repair failed build', '<!-- fletchery-merge-failure:abc -->')

    async def test_zero_normal_runs_only_one_emergency(self):
        engine, gh, rt = self.engine({1: self.repair(), 2: self.repair(2), 3: Task(3, 'Feature')}, max_parallel=0)
        engine.dispatch()
        self.assertEqual(engine.emergency_active, {1})
        self.assertEqual(engine.free_slots(), 0)
        self.assertEqual(len(engine.jobs), 1)
        await engine._wait_for_jobs(None)
        self.assertEqual([n for role, n, _ in rt.calls if role == 'worker'], [1])
        self.assertFalse(engine.emergency_active)

    async def test_extra_slot_does_not_consume_normal_capacity(self):
        engine, gh, _ = self.engine({1: self.repair(), 2: Task(2, 'Feature')}, max_parallel=1)
        engine.dispatch()
        self.assertEqual(engine.emergency_active, {1})
        self.assertEqual(engine.active, {1, 2})
        while engine.jobs:
            await engine._wait_for_jobs(None)

    async def test_off_pause_draining_and_budget_hold_repairs(self):
        for reason in ('off', 'pause', 'draining', 'budget'):
            with self.subTest(reason=reason):
                engine, gh, rt = self.engine({1: self.repair()}, max_parallel=0)
                if reason == 'off': engine.cfg.emergency_workers = 0
                if reason == 'pause': engine.state.held = True
                if reason == 'budget': engine.state.spent[today()] = engine.cfg.daily_budget_usd
                engine.dispatch(draining=reason == 'draining')
                self.assertFalse(engine.jobs)
                self.assertFalse(rt.calls)

    async def test_failed_check_fixer_works_during_merge_priority_at_zero(self):
        task = Task(1, 'Existing PR', labels=[label_for(PR)])
        engine, gh, rt = self.engine({1: task, 2: Task(2, 'Feature')}, max_parallel=0)
        gh.prs = [PullRequest(12, 'PR', task.branch, 'Test-Staging', 'a'*40, ['ready'], 'Closes #1')]
        gh.feedback[12] = [Feedback(12, 'check', 'Compile failed', now())]
        active_round = asyncio.get_running_loop().create_future()
        engine.round_job = active_round
        engine.dispatch()
        self.assertTrue(engine.merge_priority)
        self.assertEqual(engine.emergency_active, {1})
        await engine._wait_for_jobs(None)
        self.assertIn(('fixer', 1), [(role, n) for role, n, _ in rt.calls])
        self.assertFalse(active_round.done())
        self.assertIn('1', State.load(engine.state_dir/'state.json').emergency_repairs)
        active_round.set_result({})

    async def test_review_comment_alone_cannot_use_emergency_slot(self):
        task = Task(1, 'Feature PR', labels=[label_for(PR)])
        engine, gh, rt = self.engine({1: task}, max_parallel=0)
        gh.prs = [PullRequest(12, 'PR', task.branch, 'Test-Staging', 'a'*40, [], 'Closes #1')]
        gh.feedback[12] = [Feedback(12, 'comment', 'Please reword this', now())]
        await engine.once()
        self.assertFalse(rt.calls)
        self.assertFalse(engine.state.fix_attempts)

    async def test_external_excluded_pr_repaired_on_original_branch(self):
        task = Task(1, 'Repair existing PR', 'Existing PR: #12\nRepair branch: external\n<!-- fletchery-repair-pr:12 -->', [label_for(PR)])
        engine, gh, rt = self.engine({1: task}, max_parallel=0)
        pr = PullRequest(12, 'External', 'external', 'Test-Staging', 'a'*40, ['ready'])
        gh.prs = [pr]
        engine.state.merge_excluded['12'] = {'head': pr.head_sha, 'why': 'Compile failed', 'at': now(), 'repair_task': 1}
        with patch.object(gh, 'open_prs', return_value=[]):
            result = await engine.once()
        self.assertEqual(result[0].result, 'fixed', result[0].detail)
        self.assertIn('external', engine.ctx.repo.pushed)

    async def test_repair_prerequisite_can_run_but_unrelated_feature_cannot(self):
        parent = self.repair()
        parent.body += '\nDepends on: #2'
        engine, gh, rt = self.engine({1: parent, 2: Task(2, 'Required fix'), 3: Task(3, 'Feature')}, max_parallel=0)
        await engine.once()
        self.assertEqual([n for role, n, _ in rt.calls if role == 'worker'], [2])

    async def test_resource_wait_preserves_attempt_and_does_not_start_session(self):
        engine, gh, rt = self.engine({1: self.repair()}, max_parallel=0)
        lease = Path(engine.cfg.project_root)/'heavy.json'
        lease.write_text(json.dumps({'token':'other-job','purpose':'Other validation'}))
        engine.cfg.heavy_lease_file = str(lease)
        engine.state.resource_waits['1'] = '2000-01-01T00:00:00+00:00'
        await engine.once()
        self.assertFalse(rt.calls)
        self.assertFalse(engine.state.fix_attempts)

    async def test_generic_failed_round_files_one_repair_issue(self):
        engine, gh, rt = self.engine(max_parallel=0, emergency_workers=0, merge_rounds=True)
        engine.state.merge_last = {'round': 20, 'result': 'failed', 'summary': 'Build failed', 'at': now()}
        await engine.once()
        number = engine.state.merge_last['failure_issue']
        self.assertIn(number, engine.repair_numbers(gh.tasks))
        await engine.once()
        self.assertEqual(len(gh.tasks), 1)

    async def test_toggle_is_hot_validated_persisted_and_keeps_running_job(self):
        engine, gh, rt = self.engine({1: self.repair()}, max_parallel=0)
        engine.cfg._path = Path(engine.cfg.project_root)/'profile.json'
        engine.cfg._path.write_text('{}')
        engine.dispatch()
        job = next(iter(engine.jobs))
        result = projects.apply_settings(engine.cfg, {'emergency_workers':0})
        self.assertFalse(result['restart'])
        self.assertFalse(job.cancelled())
        self.assertEqual(Config.load(engine.cfg._path).emergency_workers, 0)
        for bad in (-1, 2, 0.5):
            with self.assertRaises(ValueError): projects.apply_settings(engine.cfg, {'emergency_workers':bad})
        await engine._wait_for_jobs(None)

    async def test_live_status_separates_emergency_session_immediately(self):
        engine, gh, rt = self.engine(max_parallel=0)
        engine.ctx.live = Live()
        engine.ctx.live.stage(12, 'Fixing', 'Build repair')
        engine.emergency_active.add(12)
        result = Dashboard(engine, 0).status()
        self.assertTrue(result['live'][0]['emergency'])
        self.assertEqual(result['emergency_workers']['live'][0]['task'], 12)
        engine.cfg.emergency_workers = 0
        self.assertEqual(Dashboard(engine, 0).status()['emergency_workers']['max_parallel'], 0)

    async def test_blocked_merge_repair_planner_uses_emergency_slot_at_zero(self):
        repair = self.repair()
        repair.labels = [label_for(BLOCKED)]
        feature = Task(2, 'Unrelated blocked feature', labels=[label_for(BLOCKED)])
        engine, gh, rt = self.engine({1: repair, 2: feature}, max_parallel=0)
        engine.cfg.auto_prerequisites = True
        async def planned(task, ctx, out):
            return out
        with patch('blockers.pending', return_value=True), patch('blockers.resolve', side_effect=planned):
            engine.dispatch()
            self.assertEqual(engine.emergency_active, {1})
            await engine._wait_for_jobs(None)
        self.assertNotIn('2', engine.state.blocker_checked)

    async def test_new_failed_round_reports_failure_and_preserves_failed_head(self):
        engine, gh, rt = self.engine(max_parallel=0, emergency_workers=0, merge_rounds=True)
        gh.prs = [PullRequest(12, 'Broken', 'external', 'Test-Staging', 'a'*40, ['ready'])]
        rt.script[('merger', 0)] = {'state':'failed', 'summary':'Concrete compile failure',
                                  'left_out':[{'pr':12, 'why':'compile failed'}]}
        await engine.once()
        self.assertEqual(engine.state.merge_last['result'], 'failed')
        self.assertIn('failure_issue', engine.state.merge_last)
        self.assertEqual(engine.state.merge_excluded['12']['head'], 'a'*40)

    async def test_bounded_continuation_repairs_existing_pr_without_duplicate(self):
        task = Task(1, 'Existing feature', '<!-- fletchery-blocker-applied:abc123 -->')
        engine, gh, rt = self.engine({1: task}, max_parallel=0)
        gh.prs = [PullRequest(12, 'PR', task.branch, 'Test-Staging', 'a'*40, [], 'Closes #1')]
        engine.state.emergency_repairs['1'] = 12
        engine.state.fix_attempts['12'] = 2
        result = await engine.once()
        self.assertEqual(result[0].result, 'fixed', result[0].detail)
        self.assertEqual(len(gh.prs), 1)
        self.assertEqual(engine.state.fix_attempts['12'], 1)
        self.assertEqual(engine.state.repair_continuations['12'], 'abc123')
        task.labels = [label_for('queued')]
        gh.feedback[12] = [Feedback(12, 'check', 'Still failing', '2099-01-01T00:00:00Z')]
        await engine.once()
        self.assertEqual(engine.state.fix_attempts['12'], 2, 'same plan cannot reset repair attempts again')


if __name__ == '__main__':
    unittest.main()
