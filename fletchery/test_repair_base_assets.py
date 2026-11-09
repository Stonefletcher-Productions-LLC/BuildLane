"""A repair that merges the base in must not need a grant for the base's own assets."""
from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest

from repo import Repo


class RepairBaseAssetTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="fletchery-base-assets-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        origin, wt = self.root / "origin.git", self.root / "wt"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
        subprocess.run(["git", "clone", "-q", str(origin), str(wt)], check=True, capture_output=True)
        self.wt = wt
        for key, value in (("user.name", "Fixture"), ("user.email", "fixture@localhost")):
            self.git("config", key, value)
        self.write(".gitattributes", "*.uasset filter=lfs diff=lfs merge=lfs -text\n")
        self.write("Content/Old.uasset", "old")
        self.commit("base")
        self.git("push", "-q", "--no-verify", "origin", "HEAD:main")
        self.git("switch", "-qc", "feature")
        self.write("feature.txt", "pr work")
        self.commit("feature")
        self.head = self.git("rev-parse", "HEAD").strip()
        self.git("switch", "-q", "main")
        self.write("Content/Cat.uasset", "cat from the base")
        self.write("Content/Old.uasset", "old, updated on the base")
        self.commit("base moves on")
        self.git("push", "-q", "--no-verify", "origin", "HEAD:main")
        self.git("switch", "-q", "feature")
        self.git("merge", "-q", "--no-edit", "origin/main")
        self.repo = Repo(self.wt, self.root / "worktrees", "main")

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.wt, text=True, capture_output=True, check=True).stdout

    def write(self, name, text):
        path = self.wt / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-qm", message)

    def changed(self):
        return [n for n in self.git("diff", "--no-renames", "--name-only", self.head, "HEAD").splitlines() if n]

    def test_assets_merged_from_the_base_need_no_grant(self):
        tracked = self.repo.lfs_tracked(self.wt, self.changed())
        self.assertEqual(sorted(tracked), ["Content/Cat.uasset", "Content/Old.uasset"])
        self.assertEqual(self.repo._same_as_base(self.wt, tracked), set(tracked))

    def test_the_repairs_own_asset_change_still_needs_one(self):
        self.write("Content/Cat.uasset", "cat edited by the repair")
        self.write("Content/New.uasset", "new asset from the repair")
        self.commit("repair edits assets")
        tracked = self.repo.lfs_tracked(self.wt, self.changed())
        self.assertEqual(self.repo._same_as_base(self.wt, tracked), {"Content/Old.uasset"})

    def test_without_a_merge_base_nothing_is_waived(self):
        self.repo.base = "no-such-branch"
        self.assertEqual(self.repo._same_as_base(self.wt, ["Content/Cat.uasset"]), set())


if __name__ == "__main__":
    unittest.main()
