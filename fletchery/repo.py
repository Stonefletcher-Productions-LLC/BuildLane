"""Git for the pipeline: one worktree per task under `worktree_root`, branched from `origin/<base>`, LFS smudge off.
`FakeRepo` keeps the same surface in memory for tests and dry runs. Never prunes worktrees (PC rule): a worktree whose
folder went missing is repaired, not pruned."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


class Repo:
    def __init__(self, root: Path, worktree_root: Path, base: str, remote: str = "origin"):
        self.root = Path(root)
        self.worktree_root = Path(worktree_root)
        self.base = base
        self.remote = remote
        self.env = {**os.environ, "GIT_LFS_SKIP_SMUDGE": "1"}

    def git(self, *args: str, cwd: Path | None = None, check: bool = True) -> str:
        p = subprocess.run(["git", *args], cwd=cwd or self.root, capture_output=True, text=True, env=self.env)
        if check and p.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)}: {p.stderr.strip()}")
        return p.stdout.strip()

    def fetch_base(self) -> None:
        self.git("fetch", "--no-tags", self.remote, self.base)

    def worktree(self, number: int, slug: str, branch: str) -> Path:
        """The task's worktree, created on first call from origin/<base>; on later calls just the path."""
        path = self.worktree_root / f"{number}-{slug}"
        if path.exists() and (path / ".git").exists():
            return path
        self.worktree_root.mkdir(parents=True, exist_ok=True)
        existing = self.git("worktree", "list", "--porcelain")
        if f"worktree {path}" in existing:  # registered but folder gone: PC rule says repair, never prune
            self.git("worktree", "repair", str(path), check=False)
        branches = self.git("branch", "--list", branch)
        if branches:
            self.git("worktree", "add", str(path), branch)
        else:
            self.git("worktree", "add", "-b", branch, str(path), f"{self.remote}/{self.base}")
        return path

    def changed_files(self, wt: Path) -> list[str]:
        out = self.git("diff", "--name-only", f"{self.remote}/{self.base}...HEAD", cwd=wt, check=False)
        return [l for l in out.splitlines() if l]

    def has_commits(self, wt: Path) -> bool:
        return bool(self.git("log", "--oneline", f"{self.remote}/{self.base}..HEAD", cwd=wt, check=False))

    def dirty(self, wt: Path) -> bool:
        return bool(self.git("status", "--porcelain", cwd=wt))

    def commit_all(self, wt: Path, message: str) -> None:
        self.git("add", "-A", cwd=wt)
        self.git("-c", "user.name=Fletchery", "-c", "user.email=fletchery@localhost", "commit", "-q", "-m", message, cwd=wt)

    def lfs_tracked(self, wt: Path, files: list[str]) -> list[str]:
        if not files:
            return []
        out = self.git("check-attr", "filter", "--", *files, cwd=wt, check=False)
        return [l.split(":")[0] for l in out.splitlines() if l.endswith(": filter: lfs")]

    def push(self, wt: Path, branch: str) -> None:
        """Workers change text only, so the push skips the LFS pre-push hook (`--no-verify`), which hangs where the LFS
        endpoint is unreachable (seen from a cloud session on 2026-11-02). A branch that touches an LFS-tracked file is
        refused here: that needs Tools\\CI\\PushWithLfs.ps1 on the PC, and a person."""
        tracked = self.lfs_tracked(wt, self.changed_files(wt))
        if tracked:
            raise RuntimeError("branch changes LFS-tracked files, which Fletchery never pushes: " + ", ".join(tracked[:5]))
        self.git("push", "--no-verify", "-u", self.remote, f"HEAD:{branch}", cwd=wt)

    def head(self, wt: Path) -> str:
        return self.git("rev-parse", "HEAD", cwd=wt)


class FakeRepo:
    def __init__(self, worktree_root: Path, base: str = "Test-Staging", changed: dict[int, list[str]] | None = None):
        self.worktree_root = Path(worktree_root)
        self.base = base
        self.changed = changed or {}
        self.pushed: list[str] = []
        self.fetched = 0

    def fetch_base(self):
        self.fetched += 1

    def worktree(self, number, slug, branch):
        p = self.worktree_root / f"{number}-{slug}"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def changed_files(self, wt):
        n = int(wt.name.split("-")[0])
        return self.changed.get(n, ["Source/LSUI/Private/LSExample.cpp"])

    def has_commits(self, wt):
        return bool(self.changed_files(wt))

    def dirty(self, wt):
        return False

    def commit_all(self, wt, message):
        pass

    def push(self, wt, branch):
        self.pushed.append(branch)

    def head(self, wt):
        return "deadbeef"
