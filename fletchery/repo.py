"""Git for the pipeline: one worktree per task under `worktree_root`, branched from `origin/<base>`, LFS smudge off.
`FakeRepo` keeps the same surface in memory for tests and dry runs. Never prunes worktrees (PC rule): a worktree whose
folder went missing is repaired, not pruned."""
from __future__ import annotations

import os
import json
import threading
import uuid
import re
import subprocess
from pathlib import Path

_TASK_CHECKOUT_LOCK = threading.RLock()


class Repo:
    def __init__(self, root: Path, worktree_root: Path, base: str, remote: str = "origin", asset_tasks: dict | None = None):
        self.root = Path(root)
        self.worktree_root = Path(worktree_root)
        self.base = base
        self.remote = remote
        self.asset_tasks = asset_tasks or {}
        self.env = {**os.environ, "GIT_LFS_SKIP_SMUDGE": "1"}

    def git(self, *args: str, cwd: Path | None = None, check: bool = True) -> str:
        p = subprocess.run(["git", *args], cwd=cwd or self.root, capture_output=True, text=True, encoding="utf-8", errors="replace", env=self.env)
        if check and p.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)}: {p.stderr.strip()}")
        return p.stdout.strip()

    def fetch_base(self) -> None:
        self.git("fetch", "--no-tags", self.remote, self.base)

    def _registered_worktrees(self) -> set[Path]:
        listed = self.git("worktree", "list", "--porcelain", "-z")
        return {Path(field[len("worktree "):]).resolve() for field in listed.split("\0")
                if field.startswith("worktree ")}

    def _assert_owned_checkout(self, path: Path, local_branch: str, lease_file: str = "") -> None:
        if path.resolve().parent != self.worktree_root.resolve():
            raise RuntimeError("Task checkout must stay inside its configured worktree root")
        from rounds import checkout_guard
        checkout_guard(path)
        if lease_file:
            try:
                lease = json.loads(Path(lease_file).read_text(encoding="utf-8-sig"))
            except FileNotFoundError:
                lease = {}
            except (OSError, ValueError) as e:
                raise RuntimeError(f"Cannot verify task checkout lease: {e}") from e
            if not isinstance(lease, dict):
                raise RuntimeError("Cannot verify task checkout lease: invalid lease data")
            if lease.get("token"):
                spaces = lease.get("workspaces")
                spaces = spaces.split(";") if isinstance(spaces, str) else spaces
                if not isinstance(spaces, list) or not spaces or any(not isinstance(x, str) or not x for x in spaces):
                    raise RuntimeError("PC lease ownership is unknown; task checkout is preserved")
                resolved = path.resolve()
                for space in spaces:
                    if not Path(space).is_absolute():
                        raise RuntimeError("PC lease workspace is not absolute; task checkout is preserved")
                    holder = Path(space).resolve()
                    if resolved == holder or holder in resolved.parents or resolved in holder.parents:
                        raise RuntimeError("Task checkout is held by the PC heavy lease")
        if not (path / ".git").exists() or path.resolve() not in self._registered_worktrees():
            raise RuntimeError("Task directory is not this repository's registered checkout; preserved")
        if self.git("branch", "--show-current", cwd=path) != local_branch:
            raise RuntimeError("Task checkout is on another local branch; preserved")

    def _task_checkout_state(self) -> dict:
        state_file = self.worktree_root / ".task-checkouts.json"
        try:
            state = json.loads(state_file.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return {"active": {}, "preserved": []}
        except (OSError, ValueError) as e:
            raise RuntimeError(f"Cannot read task checkout state: {e}") from e
        if not isinstance(state, dict) or not isinstance(state.get("active"), dict) or not isinstance(state.get("preserved"), list):
            raise RuntimeError("Invalid task checkout state")
        if any(not isinstance(key, str) or not isinstance(entry, dict)
               or not isinstance(entry.get("path"), str) or not entry["path"]
               or not isinstance(entry.get("local_branch"), str) or not entry["local_branch"]
               for key, entry in state["active"].items()):
            raise RuntimeError("Invalid active task checkout record")
        if any(not isinstance(entry, dict) or not isinstance(entry.get("path"), str) or not entry["path"]
               for entry in state["preserved"]):
            raise RuntimeError("Invalid preserved task checkout record")
        return state

    def _task_location(self, number: int, slug: str, branch: str, state: dict) -> tuple[Path, str]:
        default = self.worktree_root / f"{number}-{slug}"
        entry = state["active"].get(branch)
        path = Path(entry["path"]) if entry else default
        if (path.resolve().parent != self.worktree_root.resolve()
                or not re.fullmatch(re.escape(default.name) + r"(?:-attempt-[0-9a-f]{12})?", path.name)):
            raise RuntimeError("Invalid active task checkout path")
        if any(Path(entry["path"]).resolve() == path.resolve() for entry in state["preserved"]):
            raise RuntimeError("Active task checkout was already preserved")
        return path, entry["local_branch"] if entry else branch

    def worktree(self, number: int, slug: str, branch: str, lease_file: str = "") -> Path:
        """Return this task's owned active checkout. Escalated attempts use fresh paths;
        the original checkout and all generated/staged content remain untouched."""
        with _TASK_CHECKOUT_LOCK:
            state = self._task_checkout_state()
            path, local_branch = self._task_location(number, slug, branch, state)
            if path.exists():
                self._assert_owned_checkout(path, local_branch, lease_file)
                return path
            if branch in state["active"] or path.resolve() in self._registered_worktrees():
                raise RuntimeError("Registered task checkout is missing; prior work and registration are preserved")
            self.worktree_root.mkdir(parents=True, exist_ok=True)
            branches = self.git("branch", "--list", local_branch)
            if branches:
                self.git("worktree", "add", str(path), local_branch)
            else:
                self.git("worktree", "add", "-b", local_branch, str(path), f"{self.remote}/{self.base}")
            return path

    def ensure_task_checkout(self, number: int, slug: str, branch: str, path: Path,
                             lease_file: str = "", require_clean: bool = False) -> None:
        with _TASK_CHECKOUT_LOCK:
            current, local_branch = self._task_location(number, slug, branch, self._task_checkout_state())
            if current.resolve() != Path(path).resolve():
                raise RuntimeError("Task checkout is no longer its active attempt; preserved")
            self._assert_owned_checkout(current, local_branch, lease_file)
            if require_clean and self.dirty(current):
                raise RuntimeError("Task checkout has uncommitted changes; it was not pushed")

    def restart_task_worktree(self, number: int, slug: str, branch: str, previous: Path,
                              reason: str, lease_file: str = "") -> Path:
        """Preserve the failed attempt in place and start a separate candidate at base.
        The logical remote branch stays stable; no reset, clean, move or force operation."""
        from rounds import now_iso, write_file
        with _TASK_CHECKOUT_LOCK:
            state = self._task_checkout_state()
            path, local_branch = self._task_location(number, slug, branch, state)
            if path.resolve() != Path(previous).resolve():
                raise RuntimeError("Only this task's active checkout can be escalated")
            self._assert_owned_checkout(path, local_branch, lease_file)
            self.fetch_base()
            self._assert_owned_checkout(path, local_branch, lease_file)
            attempt = uuid.uuid4().hex[:12]
            replacement = self.worktree_root / f"{number}-{slug}-attempt-{attempt}"
            candidate_branch = f"fletchery/task-{number}-attempt-{attempt}"
            self.git("worktree", "add", "-b", candidate_branch, str(replacement), f"{self.remote}/{self.base}")
            state["preserved"].append({"path": str(path.resolve()), "local_branch": local_branch,
                                       "head": self.head(path), "reason": reason[:4000], "at": now_iso()})
            state["active"][branch] = {"path": str(replacement.resolve()), "local_branch": candidate_branch}
            write_file(str(self.worktree_root / ".task-checkouts.json"), state)
            return replacement

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
        if path.exists():
            self.ensure_repair_checkout(path, branch, head_sha)
        else:
            self.worktree_root.mkdir(parents=True, exist_ok=True)
            local = f"fletchery/repair-{number}-{head_sha[:12]}"
            self.git("worktree", "add", "-b", local, str(path), head_sha)
        return path

    def ensure_repair_checkout(self, path: Path, branch: str, head_sha: str,
                               lease_file: str = "", require_clean: bool = False) -> None:
        match = re.fullmatch(r"([0-9]+)-.+-repair-" + re.escape(head_sha[:12]), Path(path).name)
        if not match:
            raise RuntimeError("Unexpected repair checkout path; preserved")
        self._assert_owned_checkout(Path(path), f"fletchery/repair-{match[1]}-{head_sha[:12]}", lease_file)
        self.git("merge-base", "--is-ancestor", head_sha, "HEAD", cwd=path)
        if require_clean and self.dirty(path):
            raise RuntimeError("Repair checkout has uncommitted changes; it was not pushed")

    def push_repair(self, wt: Path, branch: str, expected_head: str, lease_file: str = "") -> None:
        """Only fast-forward the unchanged existing PR, preserving its original commits."""
        self.ensure_repair_checkout(wt, branch, expected_head, lease_file, require_clean=True)
        if self._repair_remote_head(branch) != expected_head:
            raise RuntimeError("The remote PR head changed during repair; local fix is preserved and was not pushed")
        self.git("merge-base", "--is-ancestor", expected_head, "HEAD", cwd=wt)
        changed = [line for line in self.git("diff", "--name-only", expected_head, "HEAD", cwd=wt).splitlines() if line]
        tracked = self.lfs_tracked(wt, changed)
        if tracked:
            raise RuntimeError("Repair changes LFS-tracked files; use the project's asset delivery lane: " + ", ".join(tracked[:5]))
        # Recheck after remote inspection in case another process took ownership.
        self.ensure_repair_checkout(wt, branch, expected_head, lease_file, require_clean=True)
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
        raise RuntimeError("Destructive worker resets are disabled; preserve and restart the task checkout")

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
            from asset_jobs import grant_for, permitted
            grant=grant_for(self.asset_tasks,self.worktree_root,wt)
            if not grant or any(not permitted(grant,name) for name in tracked):
                raise RuntimeError("LFS asset changes require an operator grant for every changed asset path: " + ", ".join(tracked[:5]))
            if grant.get("mode")=="delete":
                remaining=self.git("diff","--no-renames","--name-only","--diff-filter=ACMRTUXB",f"{self.remote}/{self.base}...HEAD",cwd=wt).splitlines()
                if any(name in remaining for name in tracked):
                    raise RuntimeError("Deletion-only asset grant cannot publish added or modified assets")
                self.git("push","-u",self.remote,f"HEAD:{branch}",cwd=wt)
                return
            helper=self.root / "Tools" / "CI" / "PushWithLfs.ps1"
            if not helper.is_file():
                raise RuntimeError("Authorized assets need the project's Tools/CI/PushWithLfs.ps1 delivery helper")
            proc=subprocess.run(["pwsh","-NoProfile","-File",str(helper),"-Branch",branch,"-Remote",self.remote],
                                cwd=wt,env=self.env,capture_output=True,text=True,timeout=1800)
            if proc.returncode:
                raise RuntimeError("Asset delivery failed: "+(proc.stdout+proc.stderr)[-4000:])
            return
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
        self.active_checkouts: dict[str, Path] = {}

    def fetch_base(self):
        self.fetched += 1

    def worktree(self, number, slug, branch, lease_file=""):
        p = self.active_checkouts.get(branch, self.worktree_root / f"{number}-{slug}")
        p.mkdir(parents=True, exist_ok=True)
        return p

    def repair_worktree(self, number, slug, branch, head_sha):
        return self.worktree(number, slug, branch)

    def ensure_repair_checkout(self, wt, branch, expected_head, lease_file="", require_clean=False):
        pass

    def push_repair(self, wt, branch, expected_head, lease_file=""):
        return self.push(wt, branch)

    def ensure_task_checkout(self, number, slug, branch, path, lease_file="", require_clean=False):
        pass

    def restart_task_worktree(self, number, slug, branch, previous, reason, lease_file=""):
        self.resets.append(previous.name)
        self.active_checkouts[branch] = self.worktree_root / f"{number}-{slug}-attempt-{len(self.resets)}"
        return self.worktree(number, slug, branch)

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
