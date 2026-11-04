"""Bounded GitHub retries; mutations must never be retried after ambiguous failures."""
import json
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from github import GitHub


class GitHubRetryTests(unittest.TestCase):
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
