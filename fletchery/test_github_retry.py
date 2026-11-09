"""GitHub writes retry only when the connection never opened, so a retry can't post twice."""
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from github import GitHub  # noqa: E402

NO_ROUTE = ('Patch "https://api.github.com/repos/o/r/issues/1": dial tcp 140.82.113.6:443: connectex: A connection '
            'attempt failed because the connected party did not properly respond')


def done(code, out="", err=""):
    return subprocess.CompletedProcess([], code, out, err)


class WriteRetryTests(unittest.TestCase):
    def setUp(self):
        sleep = patch("github.time.sleep")
        sleep.start()
        self.addCleanup(sleep.stop)

    def test_patch_that_never_connected_is_retried(self):
        with patch("github.subprocess.run", side_effect=[done(1, err=NO_ROUTE), done(0, "{}")]) as run:
            GitHub("o/r").api("repos/o/r/issues/1", "PATCH", {"state": "closed"})
        self.assertEqual(run.call_count, 2)

    def test_post_rejected_by_github_is_not_retried(self):
        with patch("github.subprocess.run", side_effect=[done(1, err="HTTP 422: Validation Failed")]) as run:
            with self.assertRaises(RuntimeError):
                GitHub("o/r").api("repos/o/r/issues/1/comments", "POST", {"body": "x"})
        self.assertEqual(run.call_count, 1)

    def test_post_reset_after_sending_is_not_retried(self):
        with patch("github.subprocess.run", side_effect=[done(1, err="read: connection reset by peer")]) as run:
            with self.assertRaises(RuntimeError):
                GitHub("o/r").api("repos/o/r/issues/1/comments", "POST", {"body": "x"})
        self.assertEqual(run.call_count, 1)

    def test_write_that_timed_out_is_not_repeated(self):
        with patch("github.subprocess.run", side_effect=subprocess.TimeoutExpired("gh", 90)) as run:
            with self.assertRaises(RuntimeError):
                GitHub("o/r").api("repos/o/r/issues/1/comments", "POST", {"body": "x"})
        self.assertEqual(run.call_count, 1)

    def test_write_gives_up_after_three_unreachable_attempts(self):
        with patch("github.subprocess.run", side_effect=[done(1, err=NO_ROUTE)] * 3) as run:
            with self.assertRaises(RuntimeError):
                GitHub("o/r").api("repos/o/r/issues/1", "PATCH", {"state": "closed"})
        self.assertEqual(run.call_count, 3)


if __name__ == "__main__":
    unittest.main()
