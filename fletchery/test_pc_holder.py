"""Who holds the PC: named for people, also when the job runs outside Fletchery. No network, agents or processes."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import merge
import pc_note

NOW = datetime(2026, 11, 9, 23, 55, tzinfo=timezone.utc)


def iso(minutes_ago):
    return (NOW - timedelta(minutes=minutes_ago)).isoformat()


class HolderTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.lease = Path(tmp.name) / "heavy.json"

    def hold(self, purpose="Tools\\Build.ps1", workspace="E:\\lifeshare-scratch\\release-037", minutes=12, **more):
        self.lease.write_text(json.dumps({"runId": "local", "token": "t", "acquiredUtc": iso(minutes), "purpose": purpose,
                                          "owner": "local", "workspaces": workspace, "pid": 14040, **more}), encoding="utf-8")

    def note(self, **fields):
        merge.note_path(self.lease).write_text(json.dumps(fields), encoding="utf-8")

    def test_free_pc(self):
        self.assertEqual(merge.pc_holder(self.lease, NOW), {})
        self.assertEqual(merge.pc_status(self.lease, NOW), {"held": False, "text": "", "note": {}})
        self.assertEqual(merge.pc_status("", NOW), {})

    def test_outside_job_is_named_by_folder_script_and_time(self):
        self.hold()
        pc = merge.pc_holder(self.lease, NOW)
        self.assertEqual((pc["kind"], pc["who"], pc["step"], pc["minutes"]), ("outside", "release-037", "Build", 12))
        self.assertEqual(pc["what"], "Build.ps1 in release-037")          # the old short name is kept
        self.assertEqual(merge.pc_line(pc), "release-037 (Build, 12 min)")

    def test_round_task_and_lane_holders_are_told_apart(self):
        self.hold(workspace="E:\\lifeshare-scratch\\foreman\\worktrees\\merge-round-6e3d43c29c57")
        self.assertEqual(merge.pc_holder(self.lease, NOW)["kind"], "round")
        self.hold(workspace="E:\\x\\worktrees\\930-pet-coats-clean-attempt-f9f8ae993ea6", purpose="Tools\\ImportAnimals.ps1")
        pc = merge.pc_holder(self.lease, NOW)
        self.assertEqual((pc["kind"], pc["who"], pc["issue"], pc["step"]), ("task", "task #930", 930, "ImportAnimals"))
        self.lease.write_text(json.dumps({"schemaVersion": 1, "runId": "42709", "attempt": "1", "token": "t",
                                          "workspaces": ["E:\\GitHubRunner\\LifeShare\\_work"], "acquiredUtc": iso(200)}))
        pc = merge.pc_holder(self.lease, NOW)
        self.assertEqual((pc["kind"], pc["who"]), ("lane", "Build Lane run 42709"))
        self.assertIn("3 h 20 min", merge.pc_line(pc))

    def test_a_custom_round_folder_is_recognised_from_the_round_file(self):
        self.hold(workspace="E:\\lifeshare-scratch\\release-034-round")
        self.assertEqual(merge.pc_holder(self.lease, NOW, "E:/lifeshare-scratch/release-034-round/")["kind"], "round")

    def test_label_and_step_in_the_lease_win(self):
        self.hold(label="0.3.7 release build", step="Packaging")
        pc = merge.pc_holder(self.lease, NOW)
        self.assertEqual((pc["who"], pc["step"], pc["kind"]), ("0.3.7 release build", "Packaging (Build.ps1)", "outside"))

    def test_the_jobs_note_names_it_only_for_its_own_folder_and_while_fresh(self):
        self.hold()
        self.note(label="0.3.7 release build", step="Packaging", workspace="E:/lifeshare-scratch/release-037", updated=iso(3))
        pc = merge.pc_status(self.lease, NOW)
        self.assertEqual((pc["who"], pc["step"]), ("0.3.7 release build", "Packaging (Build.ps1)"))
        self.assertEqual(pc["text"], "0.3.7 release build (Packaging (Build.ps1), 12 min)")
        self.hold(workspace="E:\\x\\worktrees\\merge-round-aaaa")              # someone else has the PC now
        pc = merge.pc_status(self.lease, NOW)
        self.assertEqual((pc["kind"], pc["who"]), ("round", "the merge round"))
        self.assertEqual(pc["note"]["label"], "0.3.7 release build")           # still listed under Building
        self.note(label="0.3.7 release build", step="Packaging", workspace="E:/lifeshare-scratch/release-037", updated=iso(45))
        self.assertEqual(merge.pc_status(self.lease, NOW)["note"], {})         # a forgotten note is not shown for ever

    def test_note_between_two_scripts_is_shown_with_a_free_lease(self):
        self.note(label="0.3.7 release build", step="Cooking", updated=iso(1))
        pc = merge.pc_status(self.lease, NOW)
        self.assertFalse(pc["held"])
        self.assertEqual(pc["note"]["step"], "Cooking")

    def test_malformed_files_read_as_nothing(self):
        self.lease.write_text("{not json", encoding="utf-8")
        merge.note_path(self.lease).write_text("[1]", encoding="utf-8")
        self.assertEqual(merge.pc_status(self.lease, NOW), {"held": False, "text": "", "note": {}})

    def test_round_headline_says_who_it_waits_for(self):
        self.hold()
        self.note(label="0.3.7 release build", step="Packaging", workspace="E:\\lifeshare-scratch\\release-037", updated=iso(2))
        round_file = self.lease.with_name("merge-round.json")
        round_file.write_text(json.dumps({"round": "96", "state": "running", "started": iso(60), "updated": iso(1),
                                          "prs": [], "worktree": "E:\\x\\worktrees\\merge-round-6e3d",
                                          "steps": [{"name": "Build (candidate C)", "state": "waiting"}]}), encoding="utf-8")
        view = merge.view(round_file, self.lease, now=NOW)
        self.assertIn("Waiting because the PC is held by 0.3.7 release build (Packaging (Build.ps1), 12 min)", view["headline"])
        self.assertEqual(view["pc"]["kind"], "outside")


class NoteCliTests(unittest.TestCase):
    def test_write_refresh_and_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            lease = str(Path(tmp) / "heavy.json")
            first = pc_note.write(lease, "0.3.7 release build", "Cooking", "E:/r")
            self.assertEqual(Path(first["note"]), merge.note_path(lease))
            second = pc_note.write(lease, "0.3.7 release build", "Packaging", "E:/r")
            self.assertEqual(second["started"], first["started"])               # one job: its start is kept
            note = merge.pc_note(lease)
            self.assertEqual((note["label"], note["step"], note["workspace"]), ("0.3.7 release build", "Packaging", "E:/r"))
            self.assertEqual(pc_note.main(["--lease", lease, "--clear"]), 0)
            self.assertEqual(merge.pc_note(lease), {})
            self.assertEqual(pc_note.main(["--lease", lease, "--clear"]), 0)    # clearing twice is fine
            self.assertEqual(pc_note.main(["--lease", lease]), 1)               # nothing to say


if __name__ == "__main__":
    unittest.main()
