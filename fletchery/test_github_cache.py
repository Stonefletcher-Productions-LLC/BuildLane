"""Short-lived GET cache in GitHub: repeat reads don't spend the REST limit; writes are seen at once. No network."""
import unittest
from unittest.mock import patch

import graph
from github import GitHub


class Calls:
    def __init__(self, answers):
        self.answers, self.log = answers, []

    def __call__(self, path, method="GET", body=None, paginate=False):
        self.log.append((method, path))
        return self.answers.get(path, "{}")


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.gh = GitHub("o/r")
        self.calls = Calls({
            "repos/o/r/issues/5": '{"number": 5, "state": "open", "labels": [{"name": "foreman"}]}',
            "repos/o/r/issues/7": '{"number": 7, "state": "closed", "state_reason": "completed"}',
            "repos/o/r/issues/5/comments?per_page=100": "[]",
        })
        patcher = patch.object(self.gh, "_request", self.calls)
        patcher.start()
        self.addCleanup(patcher.stop)

    def gets(self, path):
        return sum(1 for m, p in self.calls.log if m == "GET" and p == path)

    def test_repeat_reads_are_cached(self):
        for _ in range(5):
            self.gh.issue_thread(5)
            self.assertTrue(self.gh.issue_closed(7))
        self.assertEqual(self.gets("repos/o/r/issues/5"), 1)
        self.assertEqual(self.gets("repos/o/r/issues/5/comments?per_page=100"), 1)
        self.assertEqual(self.gets("repos/o/r/issues/7"), 1)

    def test_a_write_drops_that_issue(self):
        self.gh.issue_thread(5)
        self.gh.comment(5, "hello")
        self.gh.issue_thread(5)
        self.assertEqual(self.gets("repos/o/r/issues/5/comments?per_page=100"), 2)
        self.assertEqual(self.gets("repos/o/r/issues/5"), 2)

    def test_set_status_reads_labels_fresh(self):
        self.gh.issue_thread(5)
        self.gh.set_status(5, "queued")
        self.assertEqual(self.gets("repos/o/r/issues/5"), 2)

    def test_entries_expire(self):
        self.gh.cache_seconds = 0
        self.gh.issue_thread(5)
        self.gh.issue_thread(5)
        self.assertEqual(self.gets("repos/o/r/issues/5"), 2)

    def test_lists_are_not_cached(self):
        path = f"repos/o/r/issues?state=open&labels={graph.PREFIX}&per_page=100"
        self.calls.answers[path] = "[]"
        self.gh.open_tasks()
        self.gh.open_tasks()
        self.assertEqual(self.gets(path), 2)


if __name__ == "__main__":
    unittest.main()
