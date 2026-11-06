"""Long path lists go to git on stdin: thousands of paths on a Windows command line fail with WinError 206."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from repo import Repo  # noqa: E402


class LongPathListTests(unittest.TestCase):
    def test_lfs_tracked_handles_more_paths_than_a_command_line_holds(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            (root / ".gitattributes").write_text("*.uasset filter=lfs diff=lfs merge=lfs -text\n", encoding="utf-8")
            repo = Repo(root, root / "worktrees", "main")
            deep = "Content/Characters/Folks/Wardrobe/Outfits/Generated/VeryLongFolderNameForTesting"
            files = [f"{deep}/Item_{n:05d}.uasset" for n in range(1500)] + [f"{deep}/Notes_{n:05d}.md" for n in range(1500)]
            self.assertGreater(sum(len(f) + 1 for f in files), 32767)
            tracked = repo.lfs_tracked(root, files)
            self.assertEqual(len(tracked), 1500)
            self.assertTrue(all(f.endswith(".uasset") for f in tracked))
            self.assertEqual(repo.lfs_tracked(root, []), [])


if __name__ == "__main__":
    unittest.main()
