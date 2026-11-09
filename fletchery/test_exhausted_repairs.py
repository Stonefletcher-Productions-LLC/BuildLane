"""A PR a merge round left out whose repairs are spent: it says why, and gets one fresh repair later. No real services.

LifeShare 2026-11-09: 11 queued tasks showed "ready" while their PRs sat left out at the same head with no fixer able to
start, and the Merge section said "no PR labelled ready" although 16 carried the label. The queue looked stopped."""
import unittest
from datetime import datetime, timedelta, timezone

import test_dispatch_resilience as fixtures
from github import PullRequest
from graph import Task, QUEUED, label_for
from loop import EXHAUSTED_RETRY_HOURS, exhausted_wait, now


def ago(hours):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")


class ExhaustedRepairTests(unittest.IsolatedAsyncioTestCase):
    def setup(self, excluded_hours, attempts, retried=False, seen_after=True):
        # Like the live tasks: queued, with a saved blocker plan that already ran on the existing PR.
        task = Task(1, 'Feature with a left-out PR', '<!-- fletchery-blocker-applied:abc123 -->', [label_for(QUEUED)])
        engine, gh, rt = fixtures.DispatchResilienceTests.engine(self, {1: task}, max_parallel=2)
        engine.state.repair_continuations['12'] = 'abc123'
        engine.ctx.run_gates = lambda *a, **kw: []
        pr = PullRequest(12, 'PR', task.branch, 'Test-Staging', 'a' * 40, ['ready'], 'Closes #1')
        gh.prs = [pr]
        at = ago(excluded_hours)
        engine.state.merge_excluded['12'] = {'head': pr.head_sha, 'why': 'Conflicts in LSBuild.h', 'at': at, 'round': 82}
        if retried:
            engine.state.merge_excluded['12']['retried'] = ago(1)
        engine.state.fix_attempts['12'] = attempts
        if seen_after:
            engine.state.seen['12'] = ago(max(0, excluded_hours - 1))
        return engine, gh, rt, task, pr

    async def test_recent_spent_repair_waits_with_a_reason_not_ready(self):
        engine, gh, rt, task, pr = self.setup(excluded_hours=1, attempts=2)
        engine.dispatch()
        self.assertFalse(engine.jobs)
        why = engine.snapshot_why = {r['number']: r['why'] for r in engine.snapshot['tasks']}[1]
        self.assertIn('PR #12 was left out of merge round 82', why)
        self.assertIn('one fresh repair starts', why)
        self.assertNotEqual(why, 'ready')

    async def test_one_attempt_already_consumed_counts_as_spent(self):
        engine, gh, rt, task, pr = self.setup(excluded_hours=1, attempts=1)
        engine.dispatch()
        self.assertFalse(engine.jobs)
        self.assertIn('fresh repair', {r['number']: r['why'] for r in engine.snapshot['tasks']}[1])

    async def test_old_spent_repair_gets_exactly_one_fresh_fixer(self):
        engine, gh, rt, task, pr = self.setup(excluded_hours=EXHAUSTED_RETRY_HOURS + 1, attempts=2)
        result = await engine.once()
        self.assertIn(('fixer', 1), [(role, n) for role, n, _ in rt.calls])
        self.assertTrue(engine.state.merge_excluded['12'].get('retried'))
        self.assertEqual(engine.state.fix_attempts['12'], engine.cfg.fixer_attempts)
        calls = len(rt.calls)
        # Same head after the fresh attempt: no third run, and the task says it needs a person.
        engine.state.merge_excluded['12']['head'] = pr.head_sha
        task.labels = [label_for(QUEUED)]
        await engine.once()
        self.assertEqual(len(rt.calls), calls)
        self.assertIn('needs a person', {r['number']: r['why'] for r in engine.snapshot['tasks']}[1])

    async def test_unspent_repair_is_untouched(self):
        engine, gh, rt, task, pr = self.setup(excluded_hours=1, attempts=0, seen_after=False)
        await engine.once()
        self.assertIn(('fixer', 1), [(role, n) for role, n, _ in rt.calls])
        self.assertFalse(engine.state.merge_excluded['12'].get('retried'))

    async def test_parked_pr_says_it_needs_a_person(self):
        engine, gh, rt, task, pr = self.setup(excluded_hours=1, attempts=0, seen_after=False)
        del engine.state.merge_excluded['12']
        engine.state.merge_parked['12'] = {'head': pr.head_sha, 'why': 'Acceptance needs a 150 min run', 'count': 2}
        engine.dispatch()
        self.assertFalse(engine.jobs)
        self.assertIn('parked', {r['number']: r['why'] for r in engine.snapshot['tasks']}[1])

    def test_new_head_clears_the_wait(self):
        pr = PullRequest(12, 'PR', 'b', 'Test-Staging', 'b' * 40, ['ready'])
        self.assertEqual(exhausted_wait(1, pr, {'head': 'a' * 40, 'at': ago(1)}, 2, 2, ago(0)), '')

    async def test_merge_section_explains_labelled_but_left_out(self):
        engine, gh, rt, task, pr = self.setup(excluded_hours=1, attempts=2)
        engine.cfg.merge_rounds = True
        engine.dispatch()
        self.assertIn('1 PR(s) labelled `ready`, but none can join a round', engine.merge_why)


if __name__ == '__main__':
    unittest.main()
