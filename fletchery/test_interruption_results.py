"""Interrupted validation preserves healthy heads and the actual stop reason."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from github import FakeGitHub
from runtime import SCHEMAS
import rounds


class InterruptionResultTests(unittest.TestCase):
    def test_interrupted_round_holds_even_new_heads_until_explicit_resume(self):
        cfg = SimpleNamespace(merge_rounds=True, merge_label='ready', base_branch='Test-Staging')
        pr = SimpleNamespace(number=12, head_sha='a'*40)
        last = {'result':'interrupted', 'key':rounds.key_of([pr]), 'at':rounds.now_iso()}
        for head in ('a'*40, 'b'*40):
            pr.head_sha = head
            start, why = rounds.should_start(cfg, last, False, False, False, [pr])
            self.assertFalse(start)
            self.assertIn('authorize resuming', why)
        last['resume_authorized'] = True
        self.assertEqual(rounds.should_start(cfg, last, False, False, False, [pr]), (True, ''))

    def test_round23_legacy_operator_stop_is_interrupted_despite_passing_summary(self):
        output = {
            'state': 'failed', 'landed_pr': 0,
            'summary': 'Final build, 973 native specs, preflight and strict NavCheck passed.',
            'gave_up': 'Computer Use reported that the user stopped it with physical Escape and instructed termination.'}
        self.assertEqual(rounds.result_of(output), 'interrupted')

    def test_explicit_interrupted_and_stop_reason_supported(self):
        self.assertEqual(rounds.result_of({'state': 'interrupted'}), 'interrupted')
        self.assertEqual(rounds.result_of({'state': 'failed', 'stop_reason': 'Operator manually stopped validation.'}), 'interrupted')

    def test_incidental_user_or_escape_words_do_not_turn_defects_into_interruptions(self):
        for reason in ('User settings cause a compiler error; stopped after failed compilation.',
                       'The Escape menu test failed with a null pointer.',
                       'Code must allow an operator to stop the animation; compile failed.'):
            with self.subTest(reason=reason):
                self.assertEqual(rounds.result_of({'state': 'failed', 'gave_up': reason}), 'failed')
        self.assertEqual(rounds.result_of({'state': 'failed', 'summary': 'User stopped with physical Escape'}), 'failed')

    def test_verified_landed_result_is_not_overridden_by_earlier_stop_note(self):
        self.assertEqual(rounds.result_of({'state': 'landed', 'landed_pr': 88,
                                          'gave_up': 'Operator stopped earlier acceptance.'}), 'landed')

    def test_mixed_real_failures_and_unfinished_acceptance_stay_separate(self):
        actual = [
            {'pr': 988, 'why': 'Nursery action defects; single repair attempt failed compilation. Evidence posted on PR.'},
            {'pr': 1008, 'why': 'Shower facing acceptance failed; single repair attempt failed compilation.'},
            {'pr': 1022, 'why': 'Second editor-game launch incorrectly exits, blocking multiplayer tests.'}]
        unfinished = [
            {'pr': 999, 'why': 'Final acceptance interrupted; candidate preserved.'},
            {'pr': 1000, 'why': 'Eating shot passed; remaining final acceptance interrupted.'},
            {'pr': 1021, 'why': 'Packaged graphics acceptance incomplete.'}]
        self.assertEqual([p['pr'] for p in actual + unfinished if not rounds.deferred(p)], [988, 1008, 1022])

    def test_unattempted_legacy_entries_are_pending(self):
        for reason in ('Unattempted healthy extra PR.', 'Not validated before the round ended.',
                       'Incomplete visual acceptance; source preserved.'):
            with self.subTest(reason=reason):
                self.assertTrue(rounds.deferred({'pr': 11, 'why': reason}))

    def test_confirmed_compile_failure_is_kept_even_if_acceptance_was_interrupted(self):
        self.assertFalse(rounds.deferred({'pr': 12,
            'why': 'Compile failed with C2039; remaining acceptance interrupted.'}))

    def test_explicit_disposition_overrides_legacy_wording(self):
        self.assertTrue(rounds.deferred({'pr': 12, 'why': 'No acceptance evidence yet', 'disposition': 'deferred'}))
        self.assertFalse(rounds.deferred({'pr': 12, 'why': 'Confirmed defect before interruption', 'disposition': 'failed'}))

    def test_interrupted_history_preserves_passed_steps_and_marks_active_check_pending(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder)/'progress.json')
            rounds.write_file(path, {'started':'2026-11-05T11:32:04Z', 'steps':[
                {'name':'Build', 'state':'done'}, {'name':'Game specs', 'state':'done'},
                {'name':'Named acceptance', 'state':'running'}, {'name':'Land', 'state':'todo'}]})
            rounds.finish_file(path, 'interrupted', 'Physical Escape; resume requires operator authorization.')
            result = rounds.read_file(path)
        self.assertEqual(result['state'], 'interrupted')
        self.assertEqual(result['result'], 'interrupted')
        self.assertTrue(result['ended'])
        self.assertEqual([s['state'] for s in result['steps']], ['done','done','waiting','todo'])
        self.assertFalse(any(s['state'] == 'failed' for s in result['steps']))

    def test_interruption_after_steps_complete_does_not_invent_checkout_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder)/'progress.json')
            rounds.write_file(path, {'steps':[{'name':'Build', 'state':'done'}]})
            rounds.finish_file(path, 'interrupted', 'Operator stopped before landing.')
            result = rounds.read_file(path)
        self.assertEqual(result['steps'][0], {'name':'Await explicit merge resume', 'state':'waiting'})
        self.assertEqual(result['steps'][1], {'name':'Build', 'state':'done'})

    def test_failure_report_keeps_actual_error_alongside_successful_checks(self):
        gh = FakeGitHub()
        number = rounds.report_failure(gh, SimpleNamespace(base_branch='Test-Staging'), {
            'round':23, 'summary':'973 specs passed; candidate preserved.',
            'error':'Cannot read saved checkpoint: permission denied', 'at':'2026-11-05T13:27:12Z'})
        self.assertIn('permission denied', gh.tasks[number].title)
        self.assertIn('973 specs passed', gh.tasks[number].body)
        self.assertIn('Cannot read saved checkpoint: permission denied', gh.tasks[number].body)
        self.assertIn('Stop/failure reason:', gh.tasks[number].body)

    def test_schema_can_describe_interruption_pending_prs_and_real_checkpoint(self):
        # Agent output must express the distinction the controller consumes.
        schema = SCHEMAS['merger']['properties']
        self.assertIn('interrupted', schema['state']['enum'])
        self.assertIn('deferred', schema)
        self.assertEqual(set(schema['checkpoint']['properties']), {'worktree','branch','head'})
        self.assertIn('stop_reason', schema)


if __name__ == '__main__':
    unittest.main()
