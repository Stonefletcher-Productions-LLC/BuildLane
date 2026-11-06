"""Usage-limit pauses follow the reset time the provider gives, and limit messages stay readable."""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from pipeline import Limited  # noqa: E402
from runtime import MAX_LIMIT_PAUSE_MINUTES, limit_pause_minutes  # noqa: E402

EST = timezone(timedelta(hours=-5))
NOW = datetime(2026, 11, 6, 10, 0, tzinfo=EST)


class LimitPauseTests(unittest.TestCase):
    def test_codex_dated_reset(self):
        msg = ("codex failed: You’ve hit your usage limit. Visit https://chatgpt.com/settings/usage to purchase "
               "more credits or try again at Nov 12th, 2026 1:22 AM.")
        expected = (datetime(2026, 11, 12, 1, 22, tzinfo=EST) - NOW).total_seconds() / 60 + 1
        self.assertAlmostEqual(limit_pause_minutes(msg, 30, NOW), expected)

    def test_clock_reset_later_today_and_tomorrow(self):
        self.assertAlmostEqual(limit_pause_minutes("5-hour limit reached ∙ resets 3pm", 30, NOW), 5 * 60 + 1)
        self.assertAlmostEqual(limit_pause_minutes("limit will reset at 9:30am", 30, NOW), 23.5 * 60 + 1)

    def test_epoch_reset(self):
        at = int((NOW + timedelta(hours=2)).timestamp())
        self.assertAlmostEqual(limit_pause_minutes(f"Claude AI usage limit reached|{at}", 30, NOW), 121)

    def test_unknown_past_or_huge_fall_back_to_bounds(self):
        self.assertEqual(limit_pause_minutes("rate limit exceeded (429)", 30, NOW), 30)
        self.assertEqual(limit_pause_minutes("try again at Nov 6th, 2026 9:59 AM", 30, NOW), 30)
        self.assertEqual(limit_pause_minutes("try again at Mar 1st, 2027 1:00 AM", 30, NOW), MAX_LIMIT_PAUSE_MINUTES)
        self.assertEqual(limit_pause_minutes("try again at the 3 PM slot", 30, NOW), 30)
        self.assertEqual(limit_pause_minutes("", 45, NOW), 45)

    def test_limited_reads_as_its_message(self):
        e = Limited("codex failed: usage limit", 0.0, "limited", "codex-sol")
        self.assertEqual(f"codex-sol hit a limit: {e}", "codex-sol hit a limit: codex failed: usage limit")
        self.assertEqual(e.args[3], "codex-sol")
        self.assertEqual(str(Limited()), "")


if __name__ == "__main__":
    unittest.main()
