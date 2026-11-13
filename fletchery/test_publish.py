"""Downloadable builds (publish.py): zipping without debug files, splitting under the release file limit, pruning old
`build-*` releases, and the Builds hook that publishes the newest finished builds."""
import os
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import publish  # noqa: E402
from test_builds import Base, SHA  # noqa: E402


def package(root: Path) -> Path:
    folder = root / "LifeShare-0.4.0"
    files = {"Windows/LifeShare.exe": 3000, "Windows/LifeShare/Binaries/Win64/LifeShare.pdb": 5000,
             "Windows/LifeShare/Content/Paks/LifeShare-Windows.ucas": 40000, "Windows/LifeShare/Saved/Logs/LifeShare.log": 10,
             "Windows/Engine/Saved/Config/Windows/Manifest.ini": 10, "BUILD.txt": 50}
    for name, size in files.items():
        p = folder / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(os.urandom(size))  # incompressible, like a real package
    return folder


class FakeGitHub:
    def __init__(self, releases=None):
        self.releases = releases or []
        self.calls = []

    def api(self, path, method="GET", body=None, paginate=False, fresh=False):
        self.calls.append((method, path, body))
        if method == "GET" and "/releases/tags/" in path:
            raise RuntimeError("gh api GET ...: Not Found (HTTP 404)")
        if method == "GET":
            return self.releases
        if method == "POST":
            return {"html_url": f"https://github.com/o/r/releases/tag/{body['tag_name']}"}
        return None


class ZipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def names(self, zips):
        out = []
        for z in zips:
            with zipfile.ZipFile(z) as f:
                out += f.namelist()
        return sorted(out)

    def test_one_zip_without_pdb_or_saved(self):
        zips = publish.make_zips(package(self.root), self.root / "dl", "LifeShare-0.4.0-abc", 10**9)
        self.assertEqual([z.name for z in zips], ["LifeShare-0.4.0-abc.zip"])
        self.assertEqual(self.names(zips), ["LifeShare-0.4.0/BUILD.txt", "LifeShare-0.4.0/Windows/LifeShare.exe",
                                            "LifeShare-0.4.0/Windows/LifeShare/Content/Paks/LifeShare-Windows.ucas"])

    def test_splits_into_whole_zips_under_the_limit(self):
        zips = publish.make_zips(package(self.root), self.root / "dl", "B", 58000)
        self.assertEqual([z.name for z in zips], ["B-part1.zip", "B-part2.zip"])
        for z in zips:
            self.assertLess(z.stat().st_size, 58000)
        self.assertEqual(len(self.names(zips)), 3)  # every file once, across the parts

    def test_refuses_a_file_bigger_than_a_part(self):
        with self.assertRaises(ValueError):
            publish.make_zips(package(self.root), self.root / "dl", "B", 20000)

    def test_prune_keeps_the_newest_build_releases_only(self):
        rels = [{"id": i, "tag_name": t, "created_at": f"2026-11-1{i}T00:00:00Z"}
                for i, t in enumerate(["build-0.3.9-a", "v1.0", "build-0.4.0-b", "build-0.4.1-c", "build-0.4.2-d"])]
        gh = FakeGitHub(rels)
        gone = publish.Publisher(gh, "o/r").prune(3)
        self.assertEqual(gone, ["build-0.3.9-a"])
        self.assertIn(("DELETE", "repos/o/r/releases/0", None), gh.calls)
        self.assertIn(("DELETE", "repos/o/r/git/refs/tags/build-0.3.9-a", None), gh.calls)
        self.assertFalse(any("v1.0" in c[1] for c in gh.calls if c[0] == "DELETE"))

    def test_release_is_a_private_prerelease_at_the_commit(self):
        gh = FakeGitHub()
        z = self.root / "a.zip"
        z.write_bytes(b"x")
        row = {"version": "0.4.0", "sha": SHA, "ref": "Test-Staging", "name": "LifeShare"}
        with mock.patch("publish.subprocess.run", return_value=mock.Mock(returncode=0)) as run:
            url = publish.Publisher(gh, "o/r").release(row, [z])
        post = next(c for c in gh.calls if c[0] == "POST")
        self.assertEqual((post[2]["tag_name"], post[2]["target_commitish"], post[2]["prerelease"]),
                         (f"build-0.4.0-{SHA[:10]}", SHA, True))
        self.assertEqual(url, f"https://github.com/o/r/releases/tag/build-0.4.0-{SHA[:10]}")
        self.assertIn("--clobber", run.call_args[0][0])


class HookTests(Base):
    def done_row(self, version, finished, output):
        row = self.add(version=version)
        row = self.b.rows[-1]
        row.update(state="done", sha=SHA, finished=finished, output=str(output))
        return row

    def wait(self):
        for _ in range(200):
            if self.b._publishing is None or not self.b._publishing.is_alive():
                return
            time.sleep(0.02)

    def test_off_unless_the_profile_turns_it_on(self):
        self.done_row("0.4.0", "2026-11-12T04:00:00+00:00", self.engine.state_dir)
        self.b._publish_next()
        self.assertIsNone(self.b._publishing)

    def test_publishes_the_newest_builds_only_and_records_the_link(self):
        self.engine.cfg.builds = {"publish": {"to": "github", "keep": 1}}
        self.engine.ctx.gh = FakeGitHub()
        old = self.done_row("0.3.9", "2026-11-10T04:00:00+00:00", self.engine.state_dir / "nope")
        new = self.done_row("0.4.0", "2026-11-12T04:00:00+00:00", package(self.engine.state_dir))
        with mock.patch("publish.subprocess.run", return_value=mock.Mock(returncode=0)):
            self.b._publish_next()
            self.wait()
        self.assertEqual(new["publish"], "published")
        self.assertTrue(new["download"].startswith("https://github.com/o/r/releases/tag/build-0.4.0-"))
        self.assertNotIn("publish", old)
        self.assertEqual(list((self.engine.state_dir / "builds" / "_downloads").glob("*.zip")), [])
        self.b._publish_next()  # nothing left to do
        self.wait()
        self.assertNotIn("publish", old)

    def test_missing_folder_is_skipped_and_upload_failure_recorded(self):
        self.engine.cfg.builds = {"publish": {"to": "github", "keep": 3}}
        self.engine.ctx.gh = FakeGitHub()
        gone = self.done_row("0.3.9", "2026-11-10T04:00:00+00:00", self.engine.state_dir / "nope")
        self.b._publish_next()
        self.wait()
        self.assertEqual(gone["publish"], "skipped")
        row = self.done_row("0.4.0", "2026-11-12T04:00:00+00:00", package(self.engine.state_dir))
        with mock.patch("publish.subprocess.run", return_value=mock.Mock(returncode=1, stderr="HTTP 422", stdout="")):
            self.b._publish_next()
            self.wait()
        self.assertEqual(row["publish"], "failed")
        self.assertIn("422", row["publish_reason"])


if __name__ == "__main__":
    unittest.main()
