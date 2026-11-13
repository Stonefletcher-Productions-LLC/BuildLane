"""Downloadable builds: after a build finishes, zip it and put it on a private GitHub release, keeping only the newest few.

Alex, 2026-11-13: "I want to be able to have you kick off a build, tell me when it's done, and then I can go on a
different computer and download the build." The project repository is private, so a release on it is only visible to
people with access to the repository: sign in to GitHub on any computer and download it from the release page.

Turned on in the profile, in `workers.builds`:
    "publish": {"to": "github", "keep": 3}
optional: "repo" (default: the project repository), "partGb" (largest zip, default 1.9: GitHub refuses release files
of 2 GiB or more).

What happens to a finished build (`publish` on its row):
    zipping    the package folder is zipped without debug symbols (*.pdb) or Saved folders (logs, saves and settings
               from smoke tests on this PC). A build bigger than one zip is split into parts that each unzip on their
               own into the same folder.
    uploading  a pre-release `build-<version>-<commit>` is made at the build's commit and the zips are uploaded.
    published  `download` on the row is the release page. Older `build-*` releases beyond `keep` are deleted with their
               tags. The local zips are deleted.
    failed     `publish_reason` says why; it is tried again after a restart of Fletchery or on the next build.
    skipped    the package folder is not on this PC (a node build that was not copied back).
Only the newest `keep` finished builds are ever published, so turning this on does not upload old builds.
"""
from __future__ import annotations

import os
import subprocess
import zipfile
from pathlib import Path

TAG_PREFIX = "build-"
STORED = {".pak", ".ucas", ".utoc", ".zip", ".png", ".jpg", ".ogg", ".mp4", ".bk2"}  # already compressed
SKIP_SUFFIX = {".pdb"}
SKIP_DIR = "Saved"


def conf(builds_conf: dict) -> dict | None:
    p = (builds_conf or {}).get("publish")
    if not isinstance(p, dict) or p.get("to") != "github":
        return None
    return p


def files_to_ship(folder: Path) -> list[Path]:
    """Every file of the package except debug symbols and Saved folders (smoke tests write logs, saves and settings
    there; a player starts without them)."""
    out = []
    for root, dirs, files in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if d != SKIP_DIR)
        for name in sorted(files):
            p = Path(root) / name
            if p.suffix.lower() in SKIP_SUFFIX:
                continue
            out.append(p)
    return out


def make_zips(folder: Path, dest: Path, stem: str, part_bytes: int) -> list[Path]:
    """Zip `folder` into dest/<stem>.zip, or <stem>-part1.zip, -part2.zip ... when one zip would reach `part_bytes`.
    Each part is a whole zip: unzipping every part into one folder gives the whole build. Paths inside start with the
    folder's own name (LifeShare-0.4.0/Windows/LifeShare.exe)."""
    dest.mkdir(parents=True, exist_ok=True)
    for old in dest.glob(f"{stem}*.zip"):
        old.unlink()
    files = files_to_ship(folder)
    if not files:
        raise ValueError(f"{folder} has no files to zip")
    parts: list[Path] = []
    z = None
    try:
        for f in files:
            size = f.stat().st_size
            if size + 16384 >= part_bytes:
                raise ValueError(f"{f.name} is {size / 2**30:.2f} GiB: too big for one release file")
            # A zip only grows by at most a file's size plus headers, so this keeps every part under the limit.
            if z is None or z.fp.tell() + size + 16384 >= part_bytes:
                if z is not None:
                    z.close()
                parts.append(dest / f"{stem}-part{len(parts) + 1}.zip")
                z = zipfile.ZipFile(parts[-1], "w", zipfile.ZIP_DEFLATED, allowZip64=True, compresslevel=6)
            arc = Path(folder.name) / f.relative_to(folder)
            z.write(f, arc.as_posix(), compress_type=zipfile.ZIP_STORED if f.suffix.lower() in STORED else zipfile.ZIP_DEFLATED)
    finally:
        if z is not None:
            z.close()
    if len(parts) == 1:
        single = dest / f"{stem}.zip"
        parts[0].replace(single)
        parts = [single]
    return parts


def notes(row: dict, zips: list[Path]) -> str:
    how = ("Download the zip and unzip it, then run `Windows\\LifeShare.exe`." if len(zips) == 1 else
           f"Download all {len(zips)} parts and unzip each one into the same folder (they fill in different files), "
           "then run `Windows\\LifeShare.exe`.")
    tests = " with the Full tests first" if row.get("tests") == "full" else ""
    return (f"Packaged build {row['version']} of `{row['ref']}` at {row['sha']}{tests}, made by Fletchery on "
            f"{row.get('machine') or 'this PC'} (finished {row.get('finished') or '?'}).\n\n{how}\n\n"
            "Debug symbols (.pdb) are left out. Older `build-*` releases are deleted automatically; only the newest "
            "few are kept.")


class Publisher:
    def __init__(self, gh, repo: str, gh_cli: str = "gh", log=lambda s: None):
        self.gh, self.repo, self.cli, self.log = gh, repo, gh_cli, log

    def tag(self, row: dict) -> str:
        return f"{TAG_PREFIX}{row['version']}-{row['sha'][:10]}"

    def release(self, row: dict, zips: list[Path]) -> str:
        """Make (or reuse) the pre-release for this build, upload the zips, return its page."""
        tag = self.tag(row)
        try:
            rel = self.gh.api(f"repos/{self.repo}/releases/tags/{tag}", fresh=True)
        except RuntimeError as e:
            if "404" not in str(e) and "Not Found" not in str(e):
                raise
            rel = None
        if not rel:
            rel = self.gh.api(f"repos/{self.repo}/releases", "POST", {
                "tag_name": tag, "target_commitish": row["sha"], "name": f"{row.get('name') or 'Build'} {row['version']} ({row['sha'][:10]})",
                "body": notes(row, zips), "prerelease": True, "make_latest": "false"})
        # `gh release upload` streams large files and retries; --clobber replaces a half-uploaded asset after a failure.
        p = subprocess.run([self.cli, "release", "upload", tag, *[str(z) for z in zips], "--clobber", "--repo", self.repo],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=6 * 3600)
        if p.returncode:
            raise RuntimeError(f"upload failed: {(p.stderr or p.stdout).strip()[-400:]}")
        return str(rel.get("html_url") or f"https://github.com/{self.repo}/releases/tag/{tag}")

    def prune(self, keep: int) -> list[str]:
        """Delete `build-*` releases (and their tags) beyond the newest `keep`. Other releases are never touched."""
        rels = self.gh.api(f"repos/{self.repo}/releases?per_page=100", paginate=True, fresh=True) or []
        ours = [r for r in rels if str(r.get("tag_name") or "").startswith(TAG_PREFIX)]
        ours.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
        gone = []
        for r in ours[max(1, keep):]:
            self.gh.api(f"repos/{self.repo}/releases/{r['id']}", "DELETE")
            try:
                self.gh.api(f"repos/{self.repo}/git/refs/tags/{r['tag_name']}", "DELETE")
            except RuntimeError as e:
                self.log(f"tag {r['tag_name']} not deleted: {e}")
            gone.append(r["tag_name"])
        return gone
