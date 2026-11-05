"""Merge rounds: Fletchery keeps PRs merging by itself.

Whenever PRs labelled `ready` (workers.mergeLabel) wait on the base branch and no round is running, Fletchery starts one:
an agent session in a fresh worktree on this PC merges them together, builds and tests them the way the project's notes
say (`.fletchery/merger.md` in the repository, or the file `workers.mergeNotes` names), leaves out any PR it cannot make
pass, and lands the rest on the base branch through one "Merge round N" PR that it merges itself. Nobody is asked: the
project owner turned rounds on, which is the approval. The round reports its steps in merge-round.json (merge.py shows
them in the Merge section).

When it starts one, and when it doesn't:
- `workers.mergeRounds` is on, merging isn't paused (the Merge section's switch) and Fletchery isn't restarting.
- Workers at 0 do not stop rounds: 0 pauses new code, never merging.
- After a round that landed nothing (failed, gave up, hit a limit), the next waits `mergeRetryMinutes` unless the
  ready PRs change (a new PR, a new head), so a broken PR doesn't loop.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

STEPS = ["Merge the PRs into the round branch", "Build", "Content and imports", "Checks and tests", "Land on {base}"]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ready_prs(prs: list, cfg) -> list:
    """Open PRs into the base branch carrying the merge label, oldest first, at most `merge_round_max`."""
    label = (cfg.merge_label or "ready").lower()
    out = [p for p in prs if p.base == cfg.base_branch and label in [l.lower() for l in p.labels]
           and not getattr(p, "draft", False)]
    out.sort(key=lambda p: p.number)
    return out


def key_of(prs: list) -> str:
    """Changes when the set of ready PRs or any of their heads changes."""
    text = ",".join(f"{p.number}@{p.head_sha}" for p in sorted(prs, key=lambda p: p.number))
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def should_start(cfg, last: dict, held: bool, draining: bool, running: bool, ready: list,
                 now: datetime | None = None) -> tuple[bool, str]:
    """(start?, why not) for the Merge section."""
    if not getattr(cfg, "merge_rounds", False):
        return False, "merge rounds are off in Settings"
    if running:
        return False, "a round is running"
    if draining:
        return False, "Fletchery is restarting on new code"
    if held:
        return False, "merging is paused"
    if not ready:
        return False, f"no PR labelled `{cfg.merge_label or 'ready'}` into {cfg.base_branch}"
    if last and last.get("result") not in ("landed", "waiting") and last.get("key") == key_of(ready) and last.get("at"):
        now = now or datetime.now(timezone.utc)
        try:
            at = datetime.fromisoformat(str(last["at"]).replace("Z", "+00:00"))
        except ValueError:
            at = now - timedelta(days=1)
        wait = timedelta(minutes=float(getattr(cfg, "merge_retry_minutes", 45) or 45))
        if now - at < wait:
            nxt = (at + wait).isoformat(timespec="minutes")
            return False, f"the last round didn't land; the same PRs are tried again at {nxt[11:16]}Z or as soon as one changes"
    return True, ""


def pr_list_text(prs: list, flags_for=None) -> str:
    lines = []
    for p in prs:
        extra = []
        if p.mergeable is False:
            extra.append("conflicts with the base")
        for f in (flags_for(p) if flags_for else []):
            extra.append(f)
        lines.append(f"- #{p.number} {p.title} (branch `{p.head}`, head `{p.head_sha[:10]}`)"
                     + (" · " + " · ".join(extra) if extra else ""))
    return "\n".join(lines) or "- (none)"


def start_file(path: str, round_no: int, branch: str, worktree: str, agent: str, prs: list, base: str) -> None:
    write_file(path, {"round": str(round_no), "branch": branch, "worktree": worktree,
                      "owner": f"Fletchery ({agent})", "started": now_iso(), "updated": now_iso(),
                      "prs": [p.number for p in prs],
                      "steps": [{"name": s.format(base=base), "state": "todo"} for s in STEPS],
                      "note": "", "state": "running"})


def finish_file(path: str, result: str, note: str) -> None:
    d = read_file(path)
    if not d:
        return
    d["state"] = "waiting" if result == "waiting" else "done" if result in ("landed", "nothing") else "failed"
    if d["state"] == "waiting":
        running = [step for step in d.get("steps", []) if step.get("state") == "running"]
        for step in running:
            step["state"] = "waiting"
        if not any(step.get("state") == "waiting" for step in d.get("steps", [])):
            d.setdefault("steps", []).insert(0, {"name": "Prepare merge checkout", "state": "waiting"})
    if d["state"] == "failed":
        running = [step for step in d.get("steps", []) if step.get("state") in ("running", "waiting")]
        for step in running:
            step["state"] = "failed"
        if not running and not any(step.get("state") == "failed" for step in d.get("steps", [])):
            d.setdefault("steps", []).append({"name": "Round stopped", "state": "failed"})
    d["note"] = note[:400]
    d["updated"] = now_iso()
    write_file(path, d)


def read_file(path: str) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


def write_file(path: str, data: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(p)


def notes_text(rules, cfg) -> str:
    """The project's own instructions for rounds: .fletchery/merger.md, else the file workers.mergeNotes names."""
    extra = rules.additions.get("merger", "").strip() if rules else ""
    if not extra and getattr(cfg, "merge_notes", ""):
        try:
            extra = Path(cfg.merge_notes).read_text(encoding="utf-8-sig").strip()
        except OSError:
            extra = ""
    return extra


def result_of(output: dict) -> str:
    """landed | nothing | failed, from the merger's answer."""
    st = str(output.get("state", "")).lower()
    if st in ("landed", "nothing", "failed"):
        return st
    return "landed" if output.get("landed_pr") else "failed"


def worktree_busy(path) -> list[str]:
    """Return process owners of this exact checkout, failing closed when Windows cannot be queried."""
    import os
    import subprocess
    if os.name != "nt" or not Path(path).exists():
        return []
    # CIM command lines may spell paths with either slash. A directory boundary avoids
    # treating merge-round-<id> as an owner of its quarantined merge-round predecessor.
    normalized = str(Path(path).resolve()).replace("\\", "/")
    pattern = (re.escape(normalized) + r"(?=$|[\\/\s\"'])").replace("'", "''")
    ps = ("$ErrorActionPreference='Stop'; Get-CimInstance Win32_Process -ErrorAction Stop | "
          "Where-Object { $_.CommandLine -and $_.ProcessId -ne $PID -and "
          "$_.CommandLine.Replace('\\','/') -match '" + pattern + "' } | "
          "ForEach-Object { \"$($_.ProcessId) $($_.Name)\" }")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True,
                             text=True, timeout=60, encoding="utf-8", errors="replace", check=True).stdout
    except (OSError, subprocess.SubprocessError) as e:
        raise RuntimeError(f"Cannot verify merge checkout ownership: {e}") from e
    return [l.strip() for l in out.splitlines() if l.strip()]


class CheckoutBusy(RuntimeError):
    """A confirmed process or PC lease owns the checkout; defer without blaming a PR."""


class CheckoutRecovery(RuntimeError):
    """The failed checkout stays in place; a subsequent round uses next_path."""
    def __init__(self, path: Path, next_path: Path, error: str):
        self.path, self.next_path = path, next_path
        super().__init__(f"Merge checkout preserved at {path}; next checkout {next_path}. {error}")


def _checkout_state(repo) -> dict:
    """Recovery metadata is safety state, unlike optional UI progress. Never ignore corruption."""
    path = Path(repo.worktree_root).resolve() / ".merge-checkouts.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        raise RuntimeError(f"Cannot read merge checkout recovery state: {e}") from e
    if not isinstance(data, dict) or not isinstance(data.get("quarantined", []), list):
        raise RuntimeError("Invalid merge checkout recovery state")
    if any(not isinstance(item, dict) or not isinstance(item.get("path"), str) or not item["path"]
           for item in data.get("quarantined", [])):
        raise RuntimeError("Invalid quarantined merge checkout record")
    return data


def checkout_path(repo) -> Path:
    root = Path(repo.worktree_root).resolve()
    data = _checkout_state(repo)
    raw = data.get("active")
    if raw is not None and (not isinstance(raw, str) or not raw):
        raise RuntimeError("Invalid active merge checkout path")
    path = Path(raw or root / "merge-round").resolve()
    # Never accept arbitrary paths or symlinks escaping the configured worktree root.
    if path.parent != root or not re.fullmatch(r"merge-round(?:-[0-9a-f]{12})?", path.name):
        raise RuntimeError("Invalid active merge checkout path")
    if any(isinstance(item, dict) and Path(str(item.get("path", ""))).resolve() == path
           for item in data.get("quarantined", [])):
        raise RuntimeError("Active merge checkout is marked quarantined")
    return path


def checkout_guard(path: Path, lease_file: str = "") -> None:
    from merge import pc_holder
    busy = worktree_busy(path)
    if busy:
        raise CheckoutBusy("Merge checkout still owned by running processes: " + ", ".join(busy[:3]))
    if lease_file:
        # pc_holder's forgiving UI reader intentionally ignores malformed files. Checkout
        # preparation must not interpret unreadable/mid-write lease data as a free PC.
        try:
            lease = json.loads(Path(lease_file).read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            lease = {}
        except (OSError, ValueError) as e:
            raise RuntimeError(f"Cannot verify the PC heavy lease: {e}") from e
        if not isinstance(lease, dict):
            raise RuntimeError("Cannot verify the PC heavy lease: invalid lease data")
        if lease.get("token") or pc_holder(lease_file):
            raise CheckoutBusy("The PC heavy lease is held; merge checkout preparation waits")


def quarantine_checkout(repo, path: Path, reason: str) -> Path:
    root = Path(repo.worktree_root).resolve()
    if path.resolve().parent != root:
        raise RuntimeError("Refusing to quarantine a checkout outside its worktree root")
    state_file = str(root / ".merge-checkouts.json")
    data = _checkout_state(repo)
    replacement = root / ("merge-round-" + uuid.uuid4().hex[:12])
    entry = {"path": str(path), "at": now_iso(), "reason": reason[:7000]}
    data.setdefault("quarantined", []).append(entry)
    data["active"] = str(replacement)
    write_file(state_file, data)
    # Diagnostics live beside the checkout. No moves, deletes or writes to its assets.
    write_file(str(root / (path.name + ".recovery.json")), entry)
    return replacement


def _registered_checkouts(repo) -> set[Path]:
    # NUL porcelain preserves names containing spaces, quotes or non-ASCII characters.
    listed = repo.git("worktree", "list", "--porcelain", "-z")
    return {Path(field[len("worktree "):]).resolve() for field in listed.split("\0")
            if field.startswith("worktree ")}


def prepare_worktree(repo, branch: str, lease_file: str = "") -> Path:
    """Reuse only a clean, registered, idle checkout for incremental builds. Dirty or failed
    checkouts stay in place; never clean generated content or force-reset an active checkout."""
    if not hasattr(repo, "git"):
        return repo.worktree(0, "merge-round", branch)
    path = checkout_path(repo)
    checkout_guard(path, lease_file)
    repo.fetch_base()
    base = f"{repo.remote}/{repo.base}"
    checkout_guard(path, lease_file)  # a process/lease may have appeared during fetch
    registered = _registered_checkouts(repo)
    if path.exists() and (not (path / ".git").exists() or path not in registered):
        path = quarantine_checkout(repo, path, "Existing directory is not this repository's registered checkout; preserved")
    elif (path / ".git").exists():
        # This includes staged/unstaged edits, unresolved merges and untracked generated assets.
        try:
            dirty = repo.git("status", "--porcelain", "--untracked-files=normal", cwd=path)
        except RuntimeError as e:
            checkout_guard(path, lease_file)
            replacement = quarantine_checkout(repo, path, str(e))
            raise CheckoutRecovery(path, replacement, str(e)) from e
        checkout_guard(path, lease_file)
        if dirty:
            path = quarantine_checkout(repo, path, "Uncommitted/generated content preserved before starting a new round")
    elif path in registered:
        # A deleted folder remains registered. Preserve that record as recovery evidence,
        # rather than pruning/moving another worktree or repeatedly retrying its path.
        path = quarantine_checkout(repo, path, "Registered checkout directory is missing; registration preserved")
    checkout_guard(path, lease_file)
    try:
        if (path / ".git").exists():
            repo.git("checkout", "--no-overwrite-ignore", "-B", branch, base, cwd=path)
        else:
            Path(repo.worktree_root).mkdir(parents=True, exist_ok=True)
            repo.git("worktree", "add", "-b", branch, str(path), base)
    except RuntimeError as e:
        replacement = quarantine_checkout(repo, path, str(e))
        raise CheckoutRecovery(path, replacement, str(e)) from e
    return path


def report_failure(gh, cfg, result: dict, worktree: str = "") -> int:
    """One durable task per recurring crash, with a deduplicated occurrence for each round."""
    import graph
    summary = str(result.get("summary", ""))
    signature = re.sub(r"fletchery/merge-round-[\w-]+", "<round-branch>", summary.splitlines()[0] if summary else "unknown")
    signature = re.sub(r"\b[0-9a-f]{40}\b", "<sha>", signature)
    signature = re.sub(r"merge-round(?:-[0-9a-f]{12})?", "<merge-checkout>", signature)
    key = hashlib.sha256(signature.encode()).hexdigest()[:20]
    marker = f"<!-- fletchery-merge-failure:{key} -->"
    occurrence = f"<!-- fletchery-merge-occurrence:{result.get('round')} -->"
    evidence = (f"Round {result.get('round')} ({result.get('agent') or 'unknown agent'}), {result.get('at', '')}.\n"
                f"Worktree: `{worktree}`. Base: `{cfg.base_branch}`.\n\n"
                f"```text\n{summary[:7000]}\n```\n\n{occurrence}")
    found = gh.marked_issues(marker)
    if found:
        number = found[0]["number"]
        thread = gh.issue_thread(number)
        if occurrence not in thread.get("body", "") and not any(occurrence in c.get("body", "") for c in thread.get("comments", [])):
            gh.comment(number, "Fletchery: merge failure repeated\n\n" + evidence)
        return number
    body = ("Fletchery's merge round crashed. Diagnose and fix the underlying harness or checkout failure; "
            "add a regression proving the failure is handled and the next round can start. Preserve generated "
            "content and other worktrees; never bypass leases, weaken checks or kill processes by name. "
            "Do not run an extra build or reset the active merge worktree from a worker.\n\n" + evidence + "\n\n" + marker)
    return gh.create_issue("Fix merge-round crash: " + signature[:150], body, [graph.PREFIX, graph.label_for(graph.QUEUED)])


def without_failed_dependencies(ready: list, excluded: dict, tasks: dict, known_closed: set[int] | None = None) -> list:
    """Keep failed heads and their issue/PR dependency closure out of the candidate.

    A PR with a changed head is not itself excluded. Existing open task dependencies
    still protect its descendants until that prerequisite actually lands.
    """
    prs = {p.number: p for p in ready}
    known_closed = set(known_closed or ()) | {n for n, t in tasks.items() if t.closed}
    tasks = {n: t for n, t in tasks.items() if not t.closed}
    blocked_prs = {n for n, p in prs.items() if excluded.get(str(n), {}).get("head") == p.head_sha}
    issue_prs: dict[int, set[int]] = {t.number: {t.pr_number} for t in tasks.values() if t.pr_number is not None}
    blocked_prs.update(n for numbers in issue_prs.values() for n in numbers if n not in prs)
    for n, p in prs.items():
        for t in tasks.values():
            if t.pr_number == n or re.search(r"\bcloses\s+#" + str(t.number) + r"\b", p.body or "", re.I) or p.head == t.branch:
                issue_prs.setdefault(t.number, set()).add(n)
    # Every entry is an open issue. Its dependent cannot land unless its prerequisite
    # is already closed (absent here) or has a PR included in this exact candidate.
    blocked_issues: set[int] = {n for n in tasks if not (issue_prs.get(n, set()) & prs.keys())}
    refs = {n: {int(ref) for line in re.findall(r"^\s*depends\s+on\s*:\s*(.+)$", p.body or "", re.I | re.M)
                for ref in re.findall(r"#(\d+)", line)} for n, p in prs.items()}
    # Absence from the harness issue list is not evidence of completion: it may
    # be an unlabelled open issue, a missing PR, or a failed GitHub lookup.
    dependencies = {d for t in tasks.values() for d in t.depends_on} | {d for ds in refs.values() for d in ds}
    blocked_issues.update(dependencies - tasks.keys() - prs.keys() - known_closed)
    while True:
        old = (len(blocked_prs), len(blocked_issues))
        for issue, numbers in issue_prs.items():
            if numbers & blocked_prs:
                blocked_issues.add(issue)
        for t in tasks.values():
            if set(t.depends_on) & (blocked_issues | blocked_prs):
                blocked_issues.add(t.number)
        for issue in blocked_issues:
            blocked_prs.update(issue_prs.get(issue, set()))
        blocked_prs.update(n for n, deps in refs.items() if deps & (blocked_prs | blocked_issues))
        if old == (len(blocked_prs), len(blocked_issues)):
            break
    return [p for p in ready if p.number not in blocked_prs]


def validation_plan(files: list[str], base: str = "Test-Staging") -> str:
    """Conservative hints; the runner re-evaluates the final branch including integration fixes."""
    shown = files[:100]
    suffix = f"; plus {len(files) - len(shown)} additional paths" if len(files) > len(shown) else ""
    return ("For LifeShare's Tools/RunAllTests.ps1, when it supports Auto, run "
            f"`Tools/RunAllTests.ps1 -Set Auto -BaseRef origin/{base} -FailFast -AutoParallel`. "
            "Auto must use the final branch diff, including your fixes. "
            "If these switches are unavailable, run each required deterministic preflight alone, stop on its first "
            "failure, then the selected suite; never continue a known-red batch or claim missing tests passed. "
            "Docs-only changes: quick checks; isolated UI/content: their suites; gameplay, saves, networking, "
            "shared systems, test/runner changes, mixed or unknown files: Game. Full remains required nightly and "
            "before release. Keep the project's named acceptance tests and strict NavCheck where required. "
            "Changed PR paths (recheck after integration fixes): " + ", ".join(shown) + suffix)
