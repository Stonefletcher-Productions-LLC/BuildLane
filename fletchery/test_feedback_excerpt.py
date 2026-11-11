"""The "came back N times; stopping" comment keeps the PC check's real errors for the blocker planner (2026-11-11:
it was cut after 300 characters and the planner saw "24 changed files, 20 code fi")."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pipeline import feedback_excerpt  # noqa: E402

LOG = ("PC check failed on the build PC for head `09e9f77431` (merged onto `Test-Staging`, then built and quick-tested).\n"
       + "PR check against origin/Test-Staging: 24 changed files, 20 code files\n"
       + "\n".join(f"  [{i}/400] Compile Module.LSGame.{i}.cpp" for i in range(300))
       + "\nSource/LSTests/Private/LSPetMissingSpec.cpp(104,29): error C4459: declaration of 'Day3' hides global declaration\n"
       + "\n".join(f"  [{i}/400] Compile Module.LSUI.{i}.cpp" for i in range(300))
       + "\nSource/LSTests/Private/LSPetStorySpec.cpp(71,15): error C4459: declaration of 'Day3' hides global declaration\n"
       + "BUILD FAILED (exit 6)")


class FeedbackExcerptTests(unittest.TestCase):
    def test_long_log_keeps_first_line_errors_and_end(self):
        out = feedback_excerpt(LOG)
        self.assertLess(len(out), 3200)
        self.assertIn("PC check failed on the build PC", out)
        self.assertIn("LSPetMissingSpec.cpp(104,29): error C4459", out)
        self.assertIn("LSPetStorySpec.cpp(71,15): error C4459", out)
        self.assertIn("BUILD FAILED (exit 6)", out)

    def test_short_text_is_kept_whole(self):
        self.assertEqual(feedback_excerpt("review: rename X"), "````\nreview: rename X\n````")


if __name__ == "__main__":
    unittest.main()
