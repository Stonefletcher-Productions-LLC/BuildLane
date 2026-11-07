"""Playtest acceptance: people-only checks land and go on a checklist; parked PRs leave the backlog."""
import unittest
from datetime import datetime, timedelta, timezone

import rounds
from config import Config
from github import PullRequest


class PlaytestTextTests(unittest.TestCase):
    def test_prompt_section_only_when_on(self):
        self.assertEqual(rounds.playtest_text(Config()), "")
        text = rounds.playtest_text(Config(merge_playtest=True))
        self.assertIn("`playtest` array", text)
        self.assertIn("missing inputs", text)

    def test_lines_only_for_landed_prs(self):
        lines = rounds.playtest_lines(35, [{"pr": 1020, "check": "Steam invite  and join\nfrom a second PC"},
                                           {"pr": 1041, "check": "host and client buy a bike"},
                                           {"pr": "x", "check": "junk"}], [1020])
        self.assertEqual(lines, ["- [ ] #1020 Steam invite and join from a second PC (round 35)"])


class PeopleOnlyTests(unittest.TestCase):
    def test_classifies_real_round_reasons(self):
        self.assertTrue(rounds.people_only("Named acceptance is a two-PC Steam invite and join (joiner and host logs)."))
        self.assertTrue(rounds.people_only("manual listen-host plus two clients checklist in Docs/Parties.md"))
        self.assertTrue(rounds.people_only("needs a screenshot of the open Commitments panel at 1280x720; no harness"))
        self.assertFalse(rounds.people_only("ImportFurniture needs the external input folder E:/x, which is still absent "
                                            "on this PC. This is missing input, not a defect."))
        self.assertFalse(rounds.people_only("BikeLane offset -539.8 expected 540"))

    def test_unpark_keeps_missing_input_parks(self):
        now = datetime.now(timezone.utc).isoformat()
        parked = {"1020": {"head": "a", "parked": now, "why": "two-PC Steam join"},
                  "1028": {"head": "b", "parked": now, "why": "input folder is still absent on this PC"},
                  "1044": {"head": "c", "parked": "", "why": "manual check"}}
        self.assertEqual(rounds.unpark_people_only(parked), [1020])
        self.assertEqual(set(parked), {"1028", "1044"})

    def test_is_parked_window_and_head(self):
        now = datetime.now(timezone.utc)
        parked = {"7": {"head": "a", "parked": now.isoformat(), "why": "x"},
                  "8": {"head": "a", "parked": (now - timedelta(hours=rounds.PARK_HOURS + 1)).isoformat(), "why": "x"}}
        self.assertTrue(rounds.is_parked(parked, 7, "a"))
        self.assertFalse(rounds.is_parked(parked, 7, "b"))
        self.assertFalse(rounds.is_parked(parked, 8, "a"))
        self.assertFalse(rounds.is_parked(parked, 9, "a"))


class BacklogAndChecklistTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import test_dispatch_resilience as fixtures
        self.engine_factory = fixtures.DispatchResilienceTests.engine

    def test_parked_prs_do_not_count_toward_backlog(self):
        engine, gh, rt = self.engine_factory(self, merge_rounds=True)
        prs = {n: PullRequest(n, f"PR {n}", f"b{n}", engine.cfg.base_branch, str(n) * 40, ["ready"]) for n in (11, 12, 13)}
        engine.state.merge_parked = {"12": {"head": prs[12].head_sha, "parked": datetime.now(timezone.utc).isoformat(), "why": "x"}}
        engine._backlog_full(prs)
        self.assertEqual(engine.merge_backlog, 2)

    def test_checklist_issue_made_once_then_appended(self):
        engine, gh, rt = self.engine_factory(self, merge_rounds=True)
        bodies = {}

        def api(path, method="GET", body=None, paginate=False):
            n = int(path.rsplit("/", 1)[1])
            return {"state": "open", "body": bodies.get(n, "")}
        gh.api = api
        gh.edit_body = lambda n, b: bodies.__setitem__(n, b)
        real_create = gh.create_issue

        def create(title, body, labels):
            n = real_create(title, body, labels)
            bodies[n] = body
            return n
        gh.create_issue = create
        engine._record_playtest(40, {"playtest": [{"pr": 5, "check": "two players dance"}], "merged": [5]})
        number = engine.state.playtest_issue
        self.assertTrue(number)
        engine._record_playtest(41, {"playtest": [{"pr": 6, "check": "listen host"}, {"pr": 5, "check": "two players dance"}],
                                     "merged": [5, 6]})
        body = bodies[number]
        self.assertEqual(engine.state.playtest_issue, number)
        self.assertIn("- [ ] #5 two players dance (round 40)", body)
        self.assertIn("- [ ] #6 listen host (round 41)", body)
        self.assertTrue(body.rstrip().endswith(engine.PLAYTEST_MARKER))


if __name__ == "__main__":
    unittest.main()
