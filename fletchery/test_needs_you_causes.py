"""Causes of tasks landing in Needs you without a person being needed (LifeShare 2026-11-04..07)."""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent))

import blockers  # noqa: E402
import loop  # noqa: E402
import pipeline  # noqa: E402
from agents import AgentPool  # noqa: E402


def c(body, fletchery=True):
    return {"body": body, "fletchery": fletchery}


class RecentRequeues(unittest.TestCase):
    def test_old_requeue_before_a_pr_does_not_count(self):
        comments = [c("Fletchery requeued this task: session crashed"), c("Fletchery opened PR #5 from `b`"),
                    c("Fletchery requeued this task: fetch failed")]
        self.assertEqual(pipeline.recent_requeues(comments), 1)

    def test_a_person_resets_the_run(self):
        comments = [c("Fletchery requeued this task: a"), c("Fletchery requeued this task: b"),
                    c("Try again please", fletchery=False)]
        self.assertEqual(pipeline.recent_requeues(comments), 0)

    def test_consecutive_requeues_count(self):
        comments = [c("Fletchery blocked this task (cost so far $1.00):\n\nx"),
                    c("Fletchery requeued this task: a"), c("Fletchery requeued this task: b")]
        self.assertEqual(pipeline.recent_requeues(comments), 2)


class FakeGh:
    def __init__(self, comments=(), merged=None):
        self.comments, self.merged, self.writes, self.status = list(comments), merged, [], {}

    def issue_thread(self, n):
        return {"comments": self.comments}

    def set_status(self, n, s):
        self.status[n] = s

    def comment(self, n, body):
        self.writes.append(body)
        self.comments.append(c(body))

    def merged_pr(self, branch):
        return self.merged


def ctx_for(gh):
    return SimpleNamespace(gh=gh, log=lambda *_: None, cfg=SimpleNamespace(project_root="."))


class InfraRetry(unittest.TestCase):
    def test_second_blip_in_a_row_is_still_requeued(self):
        gh = FakeGh([c("Fletchery requeued this task: a")])
        out = pipeline.Outcome(7, "")
        pipeline._requeue_or_block(ctx_for(gh), SimpleNamespace(number=7), out, "boom")
        self.assertEqual(out.result, "requeue")

    def test_third_blip_in_a_row_blocks(self):
        gh = FakeGh([c("Fletchery requeued this task: a"), c("Fletchery requeued this task: b")])
        out = pipeline.Outcome(7, "")
        pipeline._requeue_or_block(ctx_for(gh), SimpleNamespace(number=7), out, "boom")
        self.assertEqual(out.result, "blocked")


class AlreadyMerged(unittest.TestCase):
    def test_merged_branch_goes_to_pr_state(self):
        gh = FakeGh(merged=(42, "Test-Staging", "2026-11-06T00:00:00Z"))
        out = pipeline.Outcome(9, "")
        res = pipeline._already_merged(SimpleNamespace(number=9, branch="claude/foreman-9-x"), ctx_for(gh), out)
        self.assertIs(res, out)
        self.assertEqual((out.result, out.pr_number), ("pr", 42))
        self.assertEqual(gh.status[9], pipeline.PR)

    def test_nothing_merged_is_a_real_failure(self):
        self.assertIsNone(pipeline._already_merged(SimpleNamespace(number=9, branch="b"), ctx_for(FakeGh()),
                                                   pipeline.Outcome(9, "")))


class PlannerRoot(unittest.TestCase):
    def test_unusable_evidence_checkout_falls_back_to_project(self):
        def bad(*_):
            raise RuntimeError("Task evidence checkout belongs to another local branch")
        ctx = SimpleNamespace(repo=SimpleNamespace(existing_task_worktree=bad), log=lambda *_: None,
                              cfg=SimpleNamespace(project_root="E:/proj"))
        self.assertEqual(blockers.planner_root(SimpleNamespace(number=1, slug="s", branch="b"), ctx), Path("E:/proj"))


class Recent(unittest.TestCase):
    def test_recent(self):
        fresh = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")
        old = (datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat(timespec="seconds")
        self.assertTrue(loop._recent(fresh, 30))
        self.assertFalse(loop._recent(old, 30))
        self.assertFalse(loop._recent("", 30))


class PlanBySkill(unittest.TestCase):
    def test_skill_on_the_list_is_planned(self):
        pool = SimpleNamespace(multi=True, routing={"plan": {"levels": ["hard"], "skills": ["art"]}})
        pool.plan_policy = lambda: AgentPool.plan_policy(pool)
        self.assertTrue(AgentPool.wants_plan(pool, "easy", "body", "art"))
        self.assertFalse(AgentPool.wants_plan(pool, "easy", "body", "cpp"))
        self.assertTrue(AgentPool.wants_plan(pool, "hard", "body", "cpp"))


if __name__ == "__main__":
    unittest.main()
