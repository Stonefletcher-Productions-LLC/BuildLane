"""A round's base fetch survives a moment of "cannot lock ref" from another fetch on the shared .git (LifeShare #1589)."""
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from repo import Repo  # noqa: E402

LOCK = ("error: cannot lock ref 'refs/remotes/origin/Test-Staging': Unable to create "
        "'E:/x/.git/refs/remotes/origin/Test-Staging.lock': File exists.")


def done(code, err=""):
    return subprocess.CompletedProcess([], code, "", err)


class FetchRetryTests(unittest.TestCase):
    def setUp(self):
        self.repo = Repo(Path("."), Path("wt"), "Test-Staging")
        self.waits = []

    def run_with(self, results):
        calls = []

        def fake(argv, **kw):
            calls.append(argv)
            return results[len(calls) - 1]
        with patch("repo.subprocess.run", side_effect=fake):
            try:
                self.repo.fetch_base(sleep=self.waits.append)
            finally:
                self.calls = calls

    def test_lock_clash_is_retried_with_growing_waits(self):
        self.run_with([done(1, LOCK), done(1, LOCK), done(0)])
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.calls[0], ["git", "fetch", "--no-tags", "origin", "Test-Staging"])
        self.assertEqual(self.waits, [2, 4])

    def test_gives_up_after_five_tries(self):
        with self.assertRaises(RuntimeError) as cm:
            self.run_with([done(1, LOCK)] * 5)
        self.assertIn("cannot lock ref", str(cm.exception))
        self.assertEqual(len(self.calls), 5)
        self.assertEqual(self.waits, [2, 4, 8, 16])

    def test_other_errors_are_not_retried(self):
        with self.assertRaises(RuntimeError):
            self.run_with([done(128, "fatal: couldn't find remote ref Test-Staging")])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.waits, [])


if __name__ == "__main__":
    unittest.main()
