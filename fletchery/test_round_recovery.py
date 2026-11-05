"""Checkout recovery and failed-dependency isolation; uses only disposable Git repositories."""
from __future__ import annotations

import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import rounds
import merge
from github import PullRequest
from graph import Task
from repo import Repo


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fletchery-recovery-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.git("init", "-q", "--bare", "-b", "main", str(self.root / "origin.git"))
        self.git("clone", "-q", str(self.root / "origin.git"), str(self.root / "host"))
        for key, value in (("user.name", "Fixture"), ("user.email", "fixture@localhost")):
            self.git("config", key, value, cwd=self.root / "host")
        (self.root / "host" / "a.txt").write_text("base", encoding="utf-8")
        (self.root / "host" / ".gitignore").write_text("Generated/\n", encoding="utf-8")
        self.git("add", "a.txt", ".gitignore", cwd=self.root / "host")
        self.git("commit", "-qm", "base", cwd=self.root / "host")
        self.git("push", "-q", "origin", "HEAD:main", cwd=self.root / "host")
        self.repo = Repo(self.root / "host", self.root / "worktrees", "main")
        self.probe = patch.object(rounds, "worktree_busy", return_value=[])
        self.probe.start()
        self.addCleanup(self.probe.stop)
        self.path = rounds.prepare_worktree(self.repo, "round-1")
        (self.path / "Generated").mkdir()
        (self.path / "Generated" / "artist.uasset").write_bytes(b"keep this asset")

    def git(self, *args, cwd=None):
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout

    def test_clean_checkout_reused_without_removing_ignored_assets(self):
        other = rounds.prepare_worktree(self.repo, "round-2")
        self.assertEqual(other, self.path)
        self.assertEqual((other / "Generated/artist.uasset").read_bytes(), b"keep this asset")

    def test_unlink_failure_is_reported_and_next_round_uses_separate_path(self):
        real_git = self.repo.git
        def unlink(*args, **kwargs):
            if args[0] == "checkout":
                raise RuntimeError("git checkout: unable to unlink old 'Content/locked.uasset': Invalid argument")
            return real_git(*args, **kwargs)
        with patch.object(self.repo, "git", side_effect=unlink):
            with self.assertRaises(rounds.CheckoutRecovery) as failed:
                rounds.prepare_worktree(self.repo, "round-2")
        self.assertEqual(failed.exception.path, self.path)
        self.assertNotEqual(failed.exception.next_path, self.path)
        self.assertIn("unable to unlink", str(failed.exception))
        self.assertEqual((self.path / "Generated/artist.uasset").read_bytes(), b"keep this asset")
        self.assertEqual(self.git("branch", "--show-current", cwd=self.path).strip(), "round-1")
        receipt = rounds.read_file(str(self.path.parent / (self.path.name + ".recovery.json")))
        self.assertIn("unable to unlink", receipt["reason"])
        next_path = rounds.prepare_worktree(self.repo, "round-3")
        self.assertEqual(next_path, failed.exception.next_path)
        self.assertEqual((next_path / "a.txt").read_text(), "base")

    def test_staged_unstaged_and_generated_changes_remain_in_old_checkout(self):
        (self.path / "a.txt").write_text("staged", encoding="utf-8")
        self.git("add", "a.txt", cwd=self.path)
        (self.path / "a.txt").write_text("unstaged", encoding="utf-8")
        (self.path / "new.uasset").write_bytes(b"generated")
        next_path = rounds.prepare_worktree(self.repo, "round-2")
        self.assertNotEqual(next_path, self.path)
        self.assertEqual(self.git("show", ":a.txt", cwd=self.path), "staged")
        self.assertEqual((self.path / "a.txt").read_text(), "unstaged")
        self.assertEqual((self.path / "new.uasset").read_bytes(), b"generated")

    def test_base_cannot_overwrite_ignored_generated_asset(self):
        host_asset = self.root / "host/Generated/artist.uasset"
        host_asset.parent.mkdir()
        host_asset.write_bytes(b"new tracked upstream asset")
        self.git("add", "-f", "Generated/artist.uasset", cwd=self.root / "host")
        self.git("commit", "-qm", "track upstream asset", cwd=self.root / "host")
        self.git("push", "-q", "origin", "HEAD:main", cwd=self.root / "host")
        with self.assertRaises(rounds.CheckoutRecovery):
            rounds.prepare_worktree(self.repo, "round-2")
        self.assertEqual((self.path / "Generated/artist.uasset").read_bytes(), b"keep this asset")
        next_path = rounds.prepare_worktree(self.repo, "round-3")
        self.assertEqual((next_path / "Generated/artist.uasset").read_bytes(), b"new tracked upstream asset")

    def test_new_owner_after_fetch_blocks_quarantine_and_mutation(self):
        (self.path / "a.txt").write_text("new artist work", encoding="utf-8")
        with patch.object(rounds, "worktree_busy", side_effect=[[], ["42 UnrealEditor.exe"]]):
            with self.assertRaisesRegex(RuntimeError, "owned"):
                rounds.prepare_worktree(self.repo, "round-2")
        self.assertFalse((self.path.parent / ".merge-checkouts.json").exists())
        self.assertEqual((self.path / "a.txt").read_text(), "new artist work")

    def test_new_lease_after_fetch_blocks_preparation(self):
        lease = self.root / "heavy.json"
        fetch = self.repo.fetch_base
        def acquire_during_fetch():
            fetch()
            lease.write_text(json.dumps({"token": "other-owner", "pid": 42}), encoding="utf-8")
        with patch.object(self.repo, "fetch_base", side_effect=acquire_during_fetch):
            with self.assertRaisesRegex(rounds.CheckoutBusy, "lease is held"):
                rounds.prepare_worktree(self.repo, "round-2", str(lease))
        self.assertEqual(self.git("branch", "--show-current", cwd=self.path).strip(), "round-1")

    def test_confirmed_checkout_owner_defers_without_quarantining(self):
        with patch.object(rounds, "worktree_busy", return_value=["42 UnrealEditor.exe"]):
            with self.assertRaisesRegex(rounds.CheckoutBusy, "owned by running processes"):
                rounds.prepare_worktree(self.repo, "round-2")
        self.assertFalse((self.path.parent / ".merge-checkouts.json").exists())
        self.assertEqual(self.git("branch", "--show-current", cwd=self.path).strip(), "round-1")

    def test_lease_query_errors_are_not_classified_as_busy(self):
        lease = self.root / "heavy.json"
        lease.write_text("{unfinished", encoding="utf-8")
        with self.assertRaises(RuntimeError) as error:
            rounds.checkout_guard(self.path, str(lease))
        self.assertNotIsInstance(error.exception, rounds.CheckoutBusy)
        with patch.object(rounds, "worktree_busy", side_effect=RuntimeError("CIM unavailable")):
            with self.assertRaises(RuntimeError) as error:
                rounds.checkout_guard(self.path)
        self.assertNotIsInstance(error.exception, rounds.CheckoutBusy)

    def test_bad_lease_or_registry_fails_closed(self):
        lease = self.root / "heavy.json"
        lease.write_text("{unfinished", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "Cannot verify.*lease"):
            rounds.prepare_worktree(self.repo, "round-2", str(lease))
        registry = self.path.parent / ".merge-checkouts.json"
        for value in ("{unfinished", "[]", '{"quarantined": ["bad"]}'):
            registry.write_text(value, encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "recovery state|quarantined.*record"):
                rounds.checkout_path(self.repo)

    def test_registry_cannot_escape_or_reactivate_quarantine(self):
        registry = self.path.parent / ".merge-checkouts.json"
        for value in ({"active": str(self.root / "outside")},
                      {"active": str(self.path), "quarantined": [{"path": str(self.path)}]}):
            registry.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "Invalid active|quarantined"):
                rounds.checkout_path(self.repo)

    def test_unregistered_directory_is_preserved(self):
        # A directory under our root with .git is not proof it belongs to our repository.
        fake_git = self.path.parent / "merge-round-aaaaaaaaaaaa"
        fake_git.mkdir()
        (fake_git / ".git").write_text("unrelated", encoding="utf-8")
        (fake_git / "asset.blend").write_bytes(b"unrelated artist scene")
        rounds.write_file(str(self.path.parent / ".merge-checkouts.json"), {"active": str(fake_git)})
        next_path = rounds.prepare_worktree(self.repo, "round-2")
        self.assertNotEqual(next_path, fake_git)
        self.assertEqual((fake_git / "asset.blend").read_bytes(), b"unrelated artist scene")

    def test_failed_fresh_add_is_recoverable(self):
        (self.path / "a.txt").write_text("preserve", encoding="utf-8")
        real_git = self.repo.git
        def failed_add(*args, **kwargs):
            if args[:2] == ("worktree", "add"):
                path = Path(args[4]); path.mkdir()
                (path / "generated.bin").write_bytes(b"partial add evidence")
                raise RuntimeError("worktree add: unable to unlink old file")
            return real_git(*args, **kwargs)
        with patch.object(self.repo, "git", side_effect=failed_add):
            with self.assertRaises(rounds.CheckoutRecovery) as failed:
                rounds.prepare_worktree(self.repo, "round-2")
        self.assertEqual((failed.exception.path / "generated.bin").read_bytes(), b"partial add evidence")
        self.assertNotEqual(rounds.prepare_worktree(self.repo, "round-3"), failed.exception.path)

    def test_damaged_checkout_status_is_recoverable(self):
        real_git = self.repo.git
        def broken_status(*args, **kwargs):
            if args[0] == "status":
                raise RuntimeError("git status: damaged worktree metadata")
            return real_git(*args, **kwargs)
        with patch.object(self.repo, "git", side_effect=broken_status):
            with self.assertRaises(rounds.CheckoutRecovery):
                rounds.prepare_worktree(self.repo, "round-2")
        self.assertEqual((self.path / "Generated/artist.uasset").read_bytes(), b"keep this asset")
        self.assertNotEqual(rounds.prepare_worktree(self.repo, "round-3"), self.path)


class SelectionTests(unittest.TestCase):
    def pr(self, number, body="", head=None):
        return PullRequest(number, "Fixture", f"branch-{number}", "main", head or "a" * 40, ["ready"], body)

    def test_transitive_issue_and_direct_pr_dependencies_are_held(self):
        prs = [self.pr(10, "Closes #1"), self.pr(20, "Closes #2"), self.pr(30, "Closes #3"),
               self.pr(40, "Depends on: #20"), self.pr(50)]
        tasks = {1: Task(1, "Root"), 2: Task(2, "Child", "Depends on: #1"),
                 3: Task(3, "Grandchild", "Depends on: #2")}
        out = rounds.without_failed_dependencies(prs, {"10": {"head": "a" * 40}}, tasks)
        self.assertEqual([p.number for p in out], [50])
        prs[0].head_sha = "b" * 40
        self.assertEqual(rounds.without_failed_dependencies(prs, {"10": {"head": "a" * 40}}, tasks), prs)

    def test_unrelated_refs_and_number_prefixes_do_not_match(self):
        prs = [self.pr(10, "Closes #11"), self.pr(20, "Closes #2")]
        tasks = {1: Task(1, "Other", closed=True), 2: Task(2, "Child", "Depends on: #1")}
        self.assertEqual([p.number for p in rounds.without_failed_dependencies(prs, {"10": {"head": "a" * 40}}, tasks)], [20])

    def test_busy_round_does_not_impose_failure_cooldown(self):
        prs = [self.pr(10)]
        last = {"result": "waiting", "key": rounds.key_of(prs), "at": rounds.now_iso()}
        cfg = SimpleNamespace(merge_rounds=True, merge_retry_minutes=45)
        self.assertEqual(rounds.should_start(cfg, last, False, False, False, prs), (True, ""))
        last["result"] = "failed"
        self.assertFalse(rounds.should_start(cfg, last, False, False, False, prs)[0])

    def test_validation_includes_base_and_caps_path_preview(self):
        text = rounds.validation_plan([f"Content/{n}.uasset" for n in range(110)], "integration")
        self.assertIn("origin/integration", text)
        self.assertNotIn("{base}", text)
        self.assertIn("10 additional paths", text)
        self.assertNotIn("Content/100.uasset", text)

    def test_open_prerequisite_without_ready_label_holds_descendants(self):
        child, grandchild = self.pr(20, "Closes #2"), self.pr(30, "Closes #3")
        tasks = {1: Task(1, "Prerequisite", pr_number=10),
                 2: Task(2, "Child", "Depends on: #1"),
                 3: Task(3, "Grandchild", "Depends on: #2")}
        self.assertEqual(rounds.without_failed_dependencies([child, grandchild], {"10": {"head": "a" * 40}}, tasks), [])

    def test_candidate_size_cut_cannot_drop_later_prerequisite(self):
        child, independent, prerequisite = self.pr(10, "Closes #2"), self.pr(20), self.pr(30, "Closes #1")
        tasks = {1: Task(1, "Prerequisite", pr_number=30), 2: Task(2, "Child", "Depends on: #1")}
        all_prs = [child, independent, prerequisite]
        self.assertEqual(rounds.without_failed_dependencies(all_prs, {}, tasks), all_prs)
        self.assertEqual(rounds.without_failed_dependencies(all_prs[:2], {}, tasks), [independent])

    def test_direct_issue_ref_without_a_ready_pr_is_held(self):
        child = self.pr(20, "Depends on: #1")
        self.assertEqual(rounds.without_failed_dependencies([child], {}, {1: Task(1, "Still writing")}), [])

    def test_direct_pr_ref_to_open_nonready_pr_is_held(self):
        child = self.pr(20, "Depends on: #10")
        self.assertEqual(rounds.without_failed_dependencies([child], {}, {1: Task(1, "Prerequisite", pr_number=10)}), [])


class ProcessProbeTests(unittest.TestCase):
    def test_probe_failure_and_exact_path_boundaries(self):
        # Avoid live process enumeration; inspect the actual generated probe and simulate its failure.
        with tempfile.TemporaryDirectory() as temp:
            with patch("os.name", "nt"), patch.object(subprocess, "run") as run:
                run.return_value.stdout = ""
                rounds.worktree_busy(Path(temp))
                script = run.call_args.args[0][-1]
                self.assertIn("-ErrorAction Stop", script)
                self.assertTrue(run.call_args.kwargs["check"])
                pattern = script.split("-match '", 1)[1].split("' }", 1)[0].replace("''", "'")
                normalized = str(Path(temp).resolve()).replace("\\", "/")
                self.assertRegex(normalized + '/LifeShare.uproject', pattern)
                self.assertIsNone(re.search(pattern, normalized + '-aaaaaaaaaaaa/LifeShare.uproject'))
                run.side_effect = subprocess.CalledProcessError(1, ["powershell"])
                with self.assertRaisesRegex(RuntimeError, "Cannot verify.*ownership"):
                    rounds.worktree_busy(Path(temp))

    def test_rotating_checkout_names_do_not_duplicate_failure_tasks(self):
        class Tracker:
            def __init__(self):
                self.body = ""
                self.created = 0
                self.comments = []
            def marked_issues(self, marker):
                return [{"number": 7}] if marker in self.body else []
            def issue_thread(self, _):
                return {"body": self.body, "comments": [{"body": c} for c in self.comments]}
            def create_issue(self, title, body, labels):
                self.created += 1
                self.body = body
                return 7
            def comment(self, number, body):
                self.comments.append(body)
        tracker = Tracker()
        for number, path, next_path in ((1, "merge-round", "merge-round-aaaaaaaaaaaa"),
                                        (2, "merge-round-aaaaaaaaaaaa", "merge-round-bbbbbbbbbbbb")):
            result = {"round": number, "summary": f"Merge checkout preserved at E:/wt/{path}; next checkout E:/wt/{next_path}. unable to unlink old asset"}
            self.assertEqual(rounds.report_failure(tracker, SimpleNamespace(base_branch="main"), result), 7)
        self.assertEqual(tracker.created, 1)
        self.assertEqual(len(tracker.comments), 1)

    def test_busy_round_keeps_waiting_progress_without_failed_steps(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "round.json")
            rounds.start_file(path, 18, "round-18", folder, "codex", [], "main")
            rounds.finish_file(path, "waiting", "Hearth acquired the PC during fetch")
            data = rounds.read_file(path)
            self.assertEqual(data["state"], "waiting")
            self.assertEqual(data["steps"][0], {"name": "Prepare merge checkout", "state": "waiting"})
            self.assertFalse(any(step["state"] == "failed" for step in data["steps"]))
            self.assertEqual(merge.view(path, "")["state"], "waiting")
            data["steps"] = [{"name": "Compile", "state": "done"},
                             {"name": "Imports", "state": "running"}]
            rounds.write_file(path, data)
            rounds.finish_file(path, "waiting", "Native work still owns the PC")
            self.assertEqual([s["state"] for s in rounds.read_file(path)["steps"]], ["done", "waiting"])

    def test_failed_round_updates_running_steps_and_view(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "round.json"
            rounds.write_file(str(path), {"state": "running", "round": 17, "steps": [
                {"name": "Compile", "state": "done"}, {"name": "Game specs", "state": "running"},
                {"name": "Land", "state": "todo"}]})
            rounds.finish_file(str(path), "failed", "Preserved interrupted round")
            data = rounds.read_file(str(path))
            self.assertEqual(data["steps"][0]["state"], "done")
            self.assertEqual(data["steps"][1]["state"], "failed")
            self.assertEqual(data["steps"][2]["state"], "todo")
            self.assertEqual(merge.view(str(path), "")["state"], "failed")
            rounds.write_file(str(path), {"state": "running", "round": 18, "steps": [{"name": "Compile", "state": "todo"}]})
            rounds.finish_file(str(path), "checkout_failed", "Could not prepare checkout")
            self.assertEqual(merge.view(str(path), "")["state"], "failed")
            self.assertEqual(rounds.read_file(str(path))["steps"][0]["state"], "todo")


if __name__ == "__main__":
    unittest.main()
