"""Bounded GitHub retries; mutations must never be retried after ambiguous failures."""
import json
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from github import GitHub


class GitHubRetryTests(unittest.TestCase):
    def test_abandoned_pr_does_not_satisfy_a_dependency(self):
        gh = GitHub('example/repo')
        for merged in (None, '2026-11-04T00:00:00Z'):
            with self.subTest(merged=merged), patch.object(gh, 'api', side_effect=[
                    {'state': 'closed', 'pull_request': {'url': 'pr'}}, {'merged_at': merged}]) as api:
                self.assertEqual(gh.issue_closed(90), merged is not None)
                self.assertEqual(api.call_args.args[0], 'repos/example/repo/pulls/90')

    def test_create_pr_reuses_the_open_pr_from_a_half_finished_attempt(self):
        gh = GitHub('example/repo')
        calls = []
        def api(path, method='GET', body=None, paginate=False):
            calls.append((method, path))
            if method == 'GET' and '/pulls?state=open&head=example:fletchery/9-x' in path:
                return [{'number': 1093}]
            return {}
        with patch.object(gh, 'api', side_effect=api):
            self.assertEqual(gh.create_pr('fletchery/9-x', 'Test-Staging', 't', 'b', ['foreman', 'ready'], None), 1093)
        self.assertNotIn(('POST', 'repos/example/repo/pulls'), calls)
        self.assertIn(('POST', 'repos/example/repo/issues/1093/labels'), calls)

    def test_unassignable_assignee_does_not_fail_the_pr(self):
        gh = GitHub('example/repo')
        def api(path, method='GET', body=None, paginate=False):
            if method == 'GET':
                return []
            if path.endswith('/pulls'):
                return {'number': 1094}
            if path.endswith('/assignees'):
                raise RuntimeError('gh api POST: Validation Failed (HTTP 422)')
            return {}
        with patch.object(gh, 'api', side_effect=api) as mock:
            self.assertEqual(gh.create_pr('b', 'Test-Staging', 't', 'body', ['ready'], 'ProjektPayne'), 1094)
        self.assertIn(('repos/example/repo/issues/1094/labels', 'POST', {'labels': ['ready']}),
                      [c.args[:3] for c in mock.call_args_list])
        self.assertEqual(len(gh.warnings), 1)

    def test_ambiguous_create_finds_the_pr_it_opened(self):
        gh = GitHub('example/repo')
        lookups = iter([[], [{'number': 7}]])
        def api(path, method='GET', body=None, paginate=False):
            if method == 'GET':
                return next(lookups)
            if path.endswith('/pulls'):
                raise RuntimeError('gh api POST: Validation Failed (HTTP 422)')
            return {}
        with patch.object(gh, 'api', side_effect=api):
            self.assertEqual(gh.create_pr('b', 'main', 't', 'body', ['ready'], None), 7)

    def test_plain_log_download_timeout_is_bounded(self):
        with patch('github.subprocess.run', side_effect=subprocess.TimeoutExpired('gh', 15)) as run:
            with self.assertRaisesRegex(RuntimeError, 'timed out'):
                GitHub('example/repo').api_text('repos/example/repo/actions/jobs/12/logs')
            self.assertEqual(run.call_count, 2)
            self.assertTrue(all(call.kwargs['timeout'] == 15 for call in run.call_args_list))

    def test_plain_log_download_retries_and_preserves_text(self):
        output = 'Build failed\nnot JSON: [diagnostic]\n'
        responses = [SimpleNamespace(returncode=1, stderr='TLS handshake timeout', stdout=''),
                     SimpleNamespace(returncode=0, stderr='', stdout=output)]
        with patch('github.subprocess.run', side_effect=responses):
            self.assertEqual(GitHub('example/repo').api_text('job/logs'), output)

    def test_get_retries_connect_failure_once(self):
        responses = [SimpleNamespace(returncode=1, stderr='connectex: connection timed out', stdout=''),
                     SimpleNamespace(returncode=0, stderr='', stdout='{"ok": true}')]
        with patch('github.subprocess.run', side_effect=responses) as run:
            self.assertEqual(GitHub('example/repo').api('repos/example/repo'), {'ok': True})
            self.assertEqual(run.call_count, 2)
            self.assertEqual(run.call_args.kwargs['timeout'], 15)

    def test_get_timeout_is_bounded(self):
        with patch('github.subprocess.run', side_effect=subprocess.TimeoutExpired('gh', 15)) as run:
            with self.assertRaisesRegex(RuntimeError, 'timed out'):
                GitHub('example/repo').api('repos/example/repo')
            self.assertEqual(run.call_count, 2)

    def test_mutation_timeout_is_not_retried(self):
        with patch('github.subprocess.run', side_effect=subprocess.TimeoutExpired('gh', 90)) as run:
            with self.assertRaisesRegex(RuntimeError, 'timed out'):
                GitHub('example/repo').api('repos/example/repo/issues', 'POST', {'title': 'repair'})
            self.assertEqual(run.call_count, 1)

    def test_permanent_error_is_not_retried(self):
        with patch('github.subprocess.run', return_value=SimpleNamespace(returncode=1, stderr='HTTP 404: not found', stdout='')) as run:
            with self.assertRaisesRegex(RuntimeError, '404'):
                GitHub('example/repo').api('repos/example/repo')
            self.assertEqual(run.call_count, 1)


if __name__ == '__main__':
    unittest.main()
