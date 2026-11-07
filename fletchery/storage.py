"""Conservative retirement of closed, published task checkouts.

This never prunes Git/LFS objects or removes merge/recovery candidates. A retired
checkout's branch and shared object store remain available for reconstruction.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time

import rounds


def _reparse(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _active_tasks(root: Path) -> dict[Path, str]:
    """Active task checkouts by path -> logical (pushed) branch. The registry never forgets a finished task, so an
    active entry only protects a checkout while its issue is open; `maintain` verifies closure before using this."""
    path = root / ".task-checkouts.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    return {Path(entry["path"]).resolve(): branch for branch, entry in (data.get("active") or {}).items()
            if isinstance(entry, dict) and isinstance(entry.get("path"), str) and Path(entry["path"]).is_absolute()}


def _forget_active(root: Path, path: Path) -> None:
    """Drop a retired checkout's active record, so a reopened issue starts a fresh checkout instead of failing on a
    missing one. Preserved (failed-attempt) records are history and stay."""
    from repo import _TASK_CHECKOUT_LOCK
    registry = root / ".task-checkouts.json"
    with _TASK_CHECKOUT_LOCK:
        if not registry.exists():
            return
        data = json.loads(registry.read_text(encoding="utf-8-sig"))
        active = data.get("active") or {}
        keep = {b: e for b, e in active.items() if not (isinstance(e, dict) and Path(str(e.get("path", ""))).resolve() == path)}
        if len(keep) != len(active):
            data["active"] = keep
            rounds.write_file(str(registry), data)


BUILD_OUTPUTS = ("Intermediate", "Binaries", "DerivedDataCache")


def _build_output(relative: Path) -> bool:
    """Unreal regenerates these on the next build: the project's own or a plugin's (Plugins/<name>/...)."""
    parts = relative.parts
    return (len(parts) > 1 and parts[0] in BUILD_OUTPUTS) or (len(parts) > 3 and parts[0] == "Plugins" and parts[2] in BUILD_OUTPUTS)


def _registry_protected(root: Path) -> set[Path]:
    result: set[Path] = set()
    for name in (".task-checkouts.json", ".merge-checkouts.json"):
        path = root / name
        if not path.exists():
            continue
        if _reparse(path):
            raise RuntimeError("Recovery registry is a link; cleanup deferred")
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise RuntimeError("Malformed recovery registry; cleanup deferred")
        active = data.get("active", {} if name.startswith(".task") else None)
        if name.startswith(".task"):
            if not isinstance(active, dict) or not isinstance(data.get("preserved", []), list):
                raise RuntimeError("Malformed task registry; cleanup deferred")
            entries = list(active.values()) + data.get("preserved", [])
        else:
            if active is not None and (not isinstance(active, str) or not active):
                raise RuntimeError("Malformed active merge registry; cleanup deferred")
            if not isinstance(data.get("quarantined", []), list):
                raise RuntimeError("Malformed merge recovery registry; cleanup deferred")
            entries = ([{"path": active}] if active else []) + data.get("quarantined", [])
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) or not entry["path"]:
                raise RuntimeError("Malformed recovery path; cleanup deferred")
            path = Path(entry["path"])
            if not path.is_absolute():
                raise RuntimeError("Relative recovery path; cleanup deferred")
            result.add(path.resolve())
    return result


def _tree_info(path: Path) -> tuple[int, float]:
    """Count logical bytes and latest filesystem activity without following links."""
    total, newest = 0, path.stat().st_mtime
    stack = [path]
    while stack:
        directory = stack.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                info = entry.stat(follow_symlinks=False)
                if entry.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise RuntimeError("Linked/reparse content is preserved")
                newest = max(newest, info.st_mtime)
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    total += info.st_size
                else:
                    raise RuntimeError("Special filesystem content is preserved")
    return total, newest


def _regenerable_ignored(name: str, checkout: Path) -> bool:
    relative = Path(name.replace("\\", "/"))
    if _build_output(relative):
        return True
    if relative.parent.name != "__pycache__" or relative.suffix not in (".pyc", ".pyo"):
        return False
    base = re.split(r"\.(?:cpython|pypy|graalpy)-", relative.name, maxsplit=1)[0]
    if base == relative.name:
        base = relative.stem
    return (checkout / relative.parent.parent / (base + ".py")).is_file()


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _lfs_recoverable(repo, path: Path) -> None:
    listing = json.loads(repo.git("lfs", "ls-files", "--json", cwd=path))
    if not isinstance(listing, dict) or not isinstance(listing.get("files"), list):
        raise RuntimeError("Cannot verify LFS payload inventory")
    if not listing["files"]:
        return
    environment = repo.git("lfs", "env", cwd=path)
    match = re.search(r"^LocalMediaDir=(.+)$", environment, re.MULTILINE)
    if not match or not Path(match[1].strip()).is_absolute():
        raise RuntimeError("Cannot verify shared LFS object store")
    media = Path(match[1].strip()).resolve()
    if media == path or path in media.parents:
        raise RuntimeError("LFS object store is inside the candidate checkout")
    for item in listing["files"]:
        if not isinstance(item, dict):
            raise RuntimeError("Invalid LFS inventory entry")
        name, oid, size = item.get("name"), item.get("oid"), item.get("size")
        if not isinstance(name, str) or not re.fullmatch(r"[a-f0-9]{64}", str(oid)) or type(size) is not int or size < 0:
            raise RuntimeError("Invalid LFS identity")
        relative = Path(name)
        target = path / relative
        if relative.is_absolute() or ".." in relative.parts or not target.resolve().is_relative_to(path):
            raise RuntimeError("LFS path escapes checkout")
        if not target.exists():
            continue  # Sparse/pointer-only materialization contains no payload to lose.
        if _reparse(target):
            raise RuntimeError("Linked LFS file is preserved")
        with target.open("rb") as stream:
            prefix = stream.read(1024)
        pointer = re.fullmatch(rb"version https://git-lfs.github.com/spec/v1\r?\noid sha256:([a-f0-9]{64})\r?\nsize ([0-9]+)\r?\n?", prefix)
        if pointer and target.stat().st_size == len(prefix):
            if pointer[1].decode() != oid or int(pointer[2]) != size:
                raise RuntimeError("LFS pointer differs from its recorded identity")
            continue
        obj = media / oid[:2] / oid[2:4] / oid
        if obj.resolve().is_relative_to(path) or not obj.is_file() or _reparse(obj) or obj.stat().st_size != size or _sha(obj) != oid:
            raise RuntimeError("Hydrated LFS payload lacks a verified shared object")
        if target.stat().st_size != size or _sha(target) != oid:
            raise RuntimeError("Hydrated LFS payload differs from its recorded object")


def _published(repo, gh, head: str, base_head: str, logical: str) -> None:
    """The exact commit is in the remote base, or is the head of a merged PR from the task's branch (GitHub keeps
    refs/pull/<n>/head). The second covers deleted branches and the 2026-11-05 base history rewrite, after which
    merged task heads are no longer ancestors of Test-Staging."""
    try:
        repo.git("merge-base", "--is-ancestor", head, base_head)
        return
    except RuntimeError as e:
        reason = str(e)
    if logical and hasattr(gh, "merged_heads") and head in gh.merged_heads(logical):
        return
    raise RuntimeError(reason or "Commit is not published")


def _proof(repo, path: Path, root: Path, base_head: str, protected: set[Path], lease_file: str,
           gh=None, logical: str = "") -> tuple[str, int, float]:
    if _reparse(root) or _reparse(path) or path.resolve().parent != root or path.resolve() in protected:
        raise RuntimeError("Protected or linked checkout")
    if path.resolve() not in rounds._registered_checkouts(repo) or not (path / ".git").is_file() or _reparse(path / ".git"):
        raise RuntimeError("Not this repository's registered task checkout")
    rounds.checkout_guard(path, lease_file)
    issue = int(path.name.split("-", 1)[0])
    branch = repo.git("branch", "--show-current", cwd=path)
    ordinary = re.fullmatch(r"claude/[A-Za-z0-9_-]+-" + str(issue) + r"-[A-Za-z0-9._/-]+", branch)
    attempt = logical and re.fullmatch(r"fletchery/task-" + str(issue) + r"-attempt-[0-9a-f]{12}", branch)
    if not (ordinary or attempt):
        raise RuntimeError("Not an ordinary Fletchery task branch")
    head = repo.git("rev-parse", "HEAD", cwd=path)
    if not re.fullmatch(r"[a-f0-9]{40}", head):
        raise RuntimeError("Cannot verify candidate commit")
    _published(repo, gh, head, base_head, logical or branch)
    if repo.git("status", "--porcelain=v1", "--untracked-files=all", cwd=path):
        raise RuntimeError("Tracked, staged or untracked work is preserved")
    ignored = repo.git("ls-files", "--others", "--ignored", "--exclude-standard", "-z", cwd=path)
    if any(not _regenerable_ignored(name, path) for name in ignored.split("\0") if name):
        raise RuntimeError("Unique ignored files are preserved")
    index = repo.git("ls-files", "-v", "-z", cwd=path)
    for record in index.split("\0"):
        if not record:
            continue
        if len(record) < 3 or record[1] != " " or record[0].islower():
            raise RuntimeError("Index flags can hide local changes; checkout preserved")
        if record[0] == "S" and (path / record[2:]).exists():
            raise RuntimeError("Materialized skip-worktree content is preserved")
    size, newest = _tree_info(path)
    _lfs_recoverable(repo, path)
    return head, size, newest


def trim_merge_checkouts(repo, protected_paths=(), lease_file="", dry_run=False) -> dict:
    """Delete Unreal build outputs (Intermediate, Binaries, DerivedDataCache) in merge checkouts no round will use
    again. Each idle merge round left 6-12 GB of these; 8 of them filled E: on 2026-11-07. The active checkout keeps
    its outputs for incremental builds, and the checkout itself (sources, Saved evidence, registration) stays."""
    summary = {"checked_at": rounds.now_iso(), "freed_bytes": 0, "trimmed": [], "skipped": [], "error": ""}
    try:
        root = Path(repo.worktree_root).absolute()
        if not root.is_dir() or any(_reparse(part) for part in (root, *root.parents)):
            return summary
        root = root.resolve()
        keep = {Path(p).resolve() for p in protected_paths}
        keep.add(rounds.checkout_path(repo))
        for path in sorted(root.iterdir(), key=lambda p: p.name):
            if not re.fullmatch(r"merge-round(?:-[0-9A-Za-z-]+)?", path.name) or not path.is_dir() or path.suffix:
                continue
            try:
                if _reparse(path) or path.resolve() in keep:
                    raise RuntimeError("Active, protected or linked merge checkout")
                rounds.checkout_guard(path, lease_file)
                for name in BUILD_OUTPUTS:
                    target = path / name
                    if not target.exists():
                        continue
                    if _reparse(target):
                        raise RuntimeError(f"{name} is a link; preserved")
                    size, _ = _tree_info(target)  # also refuses links/junctions anywhere inside
                    if not dry_run:
                        rounds.checkout_guard(path, lease_file)  # an editor may have started meanwhile
                        _remove_tree(target)
                        if target.exists():
                            raise RuntimeError(f"Could not fully remove {name}")
                    summary["trimmed"].append({"path": str(target), "logical_bytes": size})
                    summary["freed_bytes"] += 0 if dry_run else size
            except Exception as exc:
                summary["skipped"].append({"path": str(path), "reason": str(exc)[:500]})
    except Exception as exc:
        summary["error"] = str(exc)[:1000]
    return summary


def _remove_tree(target: Path) -> None:
    """rmtree with Windows long-path support; Unreal's Intermediate paths routinely pass 260 characters."""
    import shutil

    def writable(func, name, _exc):
        os.chmod(name, stat.S_IWRITE)
        func(name)

    spelled = str(target)
    if os.name == "nt" and not spelled.startswith("\\\\?\\"):
        spelled = "\\\\?\\" + spelled
    shutil.rmtree(spelled, onerror=writable)


def maintain(repo, gh, state_dir, protected_paths=(), open_tasks=(), lease_file="", retention_hours=24,
             limit=2, dry_run=False) -> dict:
    """Retire only closed tasks whose exact clean commits are published in the remote base.

    Unknown ownership, missing metadata, unavailable GitHub/Git/LFS and malformed
    registries all preserve files. `freed_bytes` counts logical checkout bytes;
    it is not a claim about physical volume allocation. No branch/object pruning.
    """
    summary = {"checked_at": rounds.now_iso(), "dry_run": bool(dry_run), "freed_bytes": 0,
               "retired": [], "eligible": [], "skipped": [], "error": ""}
    protected_paths = tuple(protected_paths)
    root = Path(repo.worktree_root).absolute()
    if not root.is_dir() or limit <= 0:
        return summary
    try:
        # Resolve harmless Windows 8.3 aliases only after checking every
        # original path component for junctions/symlinks.
        if any(_reparse(part) for part in (root, *root.parents)):
            raise RuntimeError("Worktree root is linked; cleanup deferred")
        root = root.resolve()
        protected = {Path(p).resolve() for p in protected_paths}
        explicit = set(protected)
        protected.update(_registry_protected(root))
        active = _active_tasks(root)
        rounds.checkout_guard(root / ".storage-probe", lease_file)
        try:  # merge-base needs the base's objects; failing here only means fewer checkouts qualify
            repo.fetch_base()
        except Exception:
            pass
        remote = repo.git("ls-remote", "--exit-code", repo.remote, "refs/heads/" + repo.base)
        rows = [row.split() for row in remote.splitlines() if row.strip()]
        if len(rows) != 1 or len(rows[0]) != 2 or not re.fullmatch(r"[a-f0-9]{40}", rows[0][0]) or rows[0][1] != "refs/heads/" + repo.base:
            raise RuntimeError("Cannot verify exact remote base identity")
        base_head = rows[0][0]
        # No fetch or pruning is required: unknown remote objects simply cannot
        # pass merge-base and their checkouts are kept until a normal fetch.
        opened = {int(n) for n in open_tasks}
        candidates = sorted(root.iterdir(), key=lambda path: path.name)
        for path in candidates:
            if len(summary["eligible"]) >= limit:
                break
            if not re.fullmatch(r"[0-9]+-[a-z0-9][a-z0-9-]*", path.name):
                continue  # In particular: every merge/recovery candidate is kept.
            resolved = path.resolve()
            logical = active.get(resolved, "")
            if path.name.endswith("-repair") or (re.search(r"-attempt-[a-f0-9]{12}$", path.name) and not logical):
                summary["skipped"].append({"path": str(path), "reason": "Repair and preserved-attempt checkouts are retained"})
                continue
            issue = int(path.name.split("-", 1)[0])
            # A finished task's active record no longer protects it; an open task's, an explicit or a preserved one does.
            guarded = (protected - {p for p in active if p not in explicit}) if issue not in opened else protected
            try:
                if issue in opened or resolved in guarded:
                    raise RuntimeError("Open task or active/preserved checkout")
                if not path.is_dir() or _reparse(path):
                    raise RuntimeError("Linked/non-directory candidate")
                if retention_hours > 0 and time.time() - path.stat().st_mtime < retention_hours * 3600:
                    raise RuntimeError("Within retention window")
                if not gh.issue_closed(issue):
                    raise RuntimeError("Task closure not verified")
                head, size, newest = _proof(repo, path, root, base_head, guarded, lease_file, gh, logical)
                if retention_hours > 0 and time.time() - newest < retention_hours * 3600:
                    raise RuntimeError("Recent checkout activity; retained")
                entry = {"path": str(path), "issue": issue, "head": head, "logical_bytes": size}
                if dry_run:
                    summary["eligible"].append(entry)
                    continue
                # Re-read safety state immediately before Git's own removal.
                current_active = _active_tasks(root)
                if current_active.get(resolved, "") != logical:
                    raise RuntimeError("Task checkout record changed during cleanup; retained")
                current_protected = ({Path(p).resolve() for p in protected_paths} | _registry_protected(root)) - (
                    {resolved} if logical and resolved not in explicit else set())
                if not gh.issue_closed(issue):
                    raise RuntimeError("Task reopened during cleanup")
                fresh_head, fresh_size, fresh_newest = _proof(repo, path, root, base_head, current_protected, lease_file, gh, logical)
                if (fresh_head, fresh_size, fresh_newest) != (head, size, newest):
                    raise RuntimeError("Checkout changed during cleanup; retained")
                if path.resolve().parent != root or _reparse(path):
                    raise RuntimeError("Cleanup target changed; retained")
                # --force permits only the pycache files already allowlisted.
                # Unique/untracked/ignored assets cannot reach this operation.
                repo.git("worktree", "remove", "--force", str(path))
                if path.exists():
                    raise RuntimeError("Git did not remove the checkout")
                if logical:
                    _forget_active(root, resolved)
                summary["eligible"].append(entry)
                summary["retired"].append(entry)
                summary["freed_bytes"] += size
            except Exception as exc:
                summary["skipped"].append({"path": str(path), "reason": str(exc)[:500]})
    except Exception as exc:
        summary["error"] = str(exc)[:1000]
    return summary
