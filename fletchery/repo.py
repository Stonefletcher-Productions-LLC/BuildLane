"""Git for the pipeline: one worktree per task under `worktree_root`, branched from `origin/<base>`, LFS smudge off.
`FakeRepo` keeps the same surface in memory for tests and dry runs. Never prunes worktrees (PC rule): a worktree whose
folder went missing is repaired, not pruned."""
from __future__ import annotations

import os
import re
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
        p = subprocess.run(["git", *args], cwd=cwd or self.root, capture_output=True, text=True, encoding="utf-8", errors="replace", env=self.env)
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

    def _repair_remote_head(self, branch: str) -> str:
        if not branch or branch.startswith("-") or branch in (self.base, "main", "master"):
            raise RuntimeError("A fixer cannot update the base/default branch")
        self.git("check-ref-format", "--branch", branch)
        ref = f"refs/heads/{branch}"
        rows = [line.split() for line in self.git("ls-remote", "--heads", self.remote, ref).splitlines()]
        heads = [row[0] for row in rows if len(row) == 2 and row[1] == ref]
        if len(heads) != 1:
            raise RuntimeError("The PR branch is not on the configured remote; repair cannot safely push it")
        return heads[0]

    def repair_worktree(self, number: int, slug: str, branch: str, head_sha: str) -> Path:
        """An owned repair checkout rooted at the exact PR head, never at the base.
        Prior repair attempts remain available; a changed PR head gets a different path.
        No original PR checkout, branch or uncommitted work is reset or moved."""
        if not re.fullmatch(r"[0-9a-fA-F]{40}", head_sha):
            raise RuntimeError("Repair needs a full PR head commit")
        if self._repair_remote_head(branch) != head_sha:
            raise RuntimeError("The PR head changed before repair; refresh it before starting another attempt")
        input_ref = f"refs/fletchery/repair-input/{number}-{head_sha}"
        self.git("fetch", "--no-tags", self.remote, f"refs/heads/{branch}:{input_ref}")
        if self.git("rev-parse", input_ref) != head_sha:
            raise RuntimeError("The PR head changed while fetching repair inputs")
        path = self.worktree_root / f"{number}-{slug}-repair-{head_sha[:12]}"
        if path.resolve().parent != self.worktree_root.resolve():
            raise RuntimeError("Repair checkout path must stay inside its configured worktree root")
        from rounds import checkout_guard
        checkout_guard(path)
        if path.exists():
            registered = self.git("worktree", "list", "--porcelain", "-z")
            roots = {Path(field[len("worktree "):]).resolve() for field in registered.split("\0")
                     if field.startswith("worktree ")}
            if not (path / ".git").exists() or path.resolve() not in roots:
                raise RuntimeError("Existing repair directory is not this repository's registered checkout; preserved")
            self.git("merge-base", "--is-ancestor", head_sha, "HEAD", cwd=path)
        else:
            self.worktree_root.mkdir(parents=True, exist_ok=True)
            local = f"fletchery/repair-{number}-{head_sha[:12]}"
            self.git("worktree", "add", "-b", local, str(path), head_sha)
        return path

    def push_repair(self, wt: Path, branch: str, expected_head: str) -> None:
        """Only fast-forward the unchanged existing PR, preserving its original commits."""
        if self._repair_remote_head(branch) != expected_head:
            raise RuntimeError("The remote PR head changed during repair; local fix is preserved and was not pushed")
        self.git("merge-base", "--is-ancestor", expected_head, "HEAD", cwd=wt)
        changed = [line for line in self.git("diff", "--name-only", expected_head, "HEAD", cwd=wt).splitlines() if line]
        tracked = self.lfs_tracked(wt, changed)
        if tracked:
            raise RuntimeError("Repair changes LFS-tracked files; use the project's asset delivery lane: " + ", ".join(tracked[:5]))
        # No force or force-with-lease: another writer advancing after the check is
        # still protected by Git's normal fast-forward receive rule.
        self.git("push", "--no-verify", "-u", self.remote, f"HEAD:refs/heads/{branch}", cwd=wt)

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

    def reset_to_base(self, wt: Path) -> None:
        """Throw away a failed attempt's work (commits and files) before a stronger agent starts on the same branch.
        Only ever inside the task's own worktree."""
        self.git("reset", "--hard", f"{self.remote}/{self.base}", cwd=wt)
        self.git("clean", "-fdq", cwd=wt)

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
        self.resets: list[str] = []

    def fetch_base(self):
        self.fetched += 1

    def worktree(self, number, slug, branch):
        p = self.worktree_root / f"{number}-{slug}"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def repair_worktree(self, number, slug, branch, head_sha):
        return self.worktree(number, slug, branch)

    def push_repair(self, wt, branch, expected_head):
        return self.push(wt, branch)

    def changed_files(self, wt):
        n = int(wt.name.split("-")[0])
        return self.changed.get(n, ["Source/LSUI/Private/LSExample.cpp"])

    def has_commits(self, wt):
        return bool(self.changed_files(wt))

    def dirty(self, wt):
        return False

    def commit_all(self, wt, message):
        pass

    def reset_to_base(self, wt):
        self.resets.append(wt.name)

    def push(self, wt, branch):
        self.pushed.append(branch)

    def head(self, wt):
        return "deadbeef"
