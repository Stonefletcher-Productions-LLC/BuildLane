"""A `tail -f` left on a round's log must not block the merge checkout (2026-11-07: round 39 waited 3 h on one)."""
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

import rounds

ROOT = "E:/lifeshare-scratch/foreman/worktrees/merge-round-d7a071104f37"


class LogFollowerTests(unittest.TestCase):
    def test_tail_follow_on_saved_log_is_ignored(self):
        cmd = '"C:\\Program Files\\Git\\usr\\bin\\tail.exe" -n +1 -f /e/lifeshare-scratch/foreman/worktrees/merge-round-d7a071104f37/Saved/MergeRound38/auto.log'
        self.assertTrue(rounds.log_follower("tail.exe", cmd, ROOT))
        self.assertTrue(rounds.log_follower("tail.exe", f"tail -F {ROOT}/Saved/Logs/x.log", ROOT))
        self.assertTrue(rounds.log_follower("tail.exe", f"tail --follow=name {ROOT}\\Saved\\a.log".replace("/", "\\"), ROOT))

    def test_anything_else_still_owns_the_checkout(self):
        self.assertFalse(rounds.log_follower("tail.exe", f"tail -n 50 {ROOT}/Saved/a.log", ROOT))      # not following
        self.assertFalse(rounds.log_follower("tail.exe", f"tail -f {ROOT}/Source/x.cpp", ROOT))      # not a log
        self.assertFalse(rounds.log_follower("tail.exe", f"tail -f {ROOT}/Saved/a.log {ROOT}/Intermediate/b", ROOT))
        self.assertFalse(rounds.log_follower("UnrealEditor.exe", f"UnrealEditor.exe {ROOT}/LifeShare.uproject -f", ROOT))
        self.assertFalse(rounds.log_follower("bash.exe", f"bash -c 'tail -f {ROOT}/Saved/a.log'", ROOT))


class StallTests(unittest.IsolatedAsyncioTestCase):
    async def test_blocked_checkout_frees_workers_after_30_minutes(self):
        import test_dispatch_resilience as fixtures
        from datetime import datetime, timedelta, timezone
        from unittest.mock import patch
        from github import PullRequest
        engine, gh, rt = fixtures.DispatchResilienceTests.engine(self, merge_rounds=True, max_parallel=0)
        gh.prs = [PullRequest(12, "Ready", "b12", "Test-Staging", "a" * 40, ["ready"])]
        busy = rounds.CheckoutBusy("Merge checkout still owned by running processes: 2164 tail.exe")
        with patch("rounds.checkout_guard", side_effect=busy), patch.object(type(engine.ctx.repo), "worktree_root",
                                                                             engine.ctx.repo.worktree_root, create=True):
            engine._merge_round(False)
            self.assertTrue(engine.merge_priority)  # a fresh block still reserves the slot for the round
            self.assertFalse(engine._checkout_stall["loud"])
            old = (datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat()
            engine._checkout_stall["since"] = old
            engine._merge_round(False)
        self.assertTrue(engine._checkout_stall["loud"])
        self.assertFalse(engine.merge_priority)
        text = engine.status_text({}, {}, {}) if engine.status_text.__code__.co_argcount == 4 else ""
        if text:
            self.assertIn("Merging stalled", text)


@unittest.skipUnless(os.name == "nt" and shutil.which("tail"), "needs Windows and a tail executable")
class LiveQueryTests(unittest.TestCase):
    def test_live_tail_is_not_an_owner_but_a_real_process_is(self):
        root = Path(tempfile.mkdtemp(prefix="merge-round-follow-")).resolve()  # CI temp paths are 8.3 short names
        (root / "Saved").mkdir()
        log = root / "Saved" / "auto.log"
        log.write_text("x\n")
        msys = "/" + str(root).replace("\\", "/")[0].lower() + str(root).replace("\\", "/")[2:]
        follower = subprocess.Popen([shutil.which("tail"), "-n", "+1", "-f", f"{msys}/Saved/auto.log"],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        holder = subprocess.Popen(["powershell", "-NoProfile", "-Command", f"Start-Sleep 30 # {root}\\Source"],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            time.sleep(1.5)
            owners = rounds.worktree_busy(root)
            self.assertFalse(any(o.startswith(f"{follower.pid} ") for o in owners), owners)
            self.assertTrue(any(o.startswith(f"{holder.pid} ") for o in owners), owners)
            stopped = rounds.stop_log_followers(root)
            self.assertEqual([s.split()[0] for s in stopped], [str(follower.pid)])
            follower.wait(15)
            self.assertIsNone(holder.poll(), "only the log follower is stopped")
        finally:
            for proc in (follower, holder):
                proc.kill()
                proc.wait(10)


if __name__ == "__main__":
    unittest.main()
