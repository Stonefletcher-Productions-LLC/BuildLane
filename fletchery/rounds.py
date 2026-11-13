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

from graph import Task

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
    if last and last.get("result") == "interrupted" and not last.get("resume_authorized"):
        return False, "merge acceptance was interrupted; press Merge now to authorize resuming the saved candidate"
    if not ready:
        return False, f"no PR labelled `{cfg.merge_label or 'ready'}` into {cfg.base_branch}"
    if last and last.get("result") not in ("landed", "waiting", "interrupted") and last.get("key") == key_of(ready) and last.get("at"):
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
    d["state"] = result if result in ("waiting", "interrupted") else "done" if result in ("landed", "nothing") else "failed"
    if d["state"] in ("waiting", "interrupted"):
        running = [step for step in d.get("steps", []) if step.get("state") == "running"]
        for step in running:
            step["state"] = "waiting"
        if not any(step.get("state") == "waiting" for step in d.get("steps", [])):
            name = "Await explicit merge resume" if result == "interrupted" else "Prepare merge checkout"
            d.setdefault("steps", []).insert(0, {"name": name, "state": "waiting"})
    if d["state"] == "failed":
        running = [step for step in d.get("steps", []) if step.get("state") in ("running", "waiting")]
        for step in running:
            step["state"] = "failed"
        if not running and not any(step.get("state") == "failed" for step in d.get("steps", [])):
            d.setdefault("steps", []).append({"name": "Round stopped", "state": "failed"})
    d["note"] = note[:400]
    d["updated"] = now_iso()
    d["result"] = result
    if result != "waiting":
        d["ended"] = d["updated"]
    write_file(path, d)


def stop_orphan(path: str, live_round, note: str, booted: str = "", ended=()) -> str:
    """A round file still saying running when no round of that number runs in this Fletchery (a restart, a crash, a
    cancelled round) is history: mark it stopped so the Merging card says Idle instead of "May be stuck". Returns the
    stopped round's number, or "" when nothing changed."""
    import merge
    d = read_file(path)
    if not d or str(d.get("state", "")).lower() in merge.TERMINAL_STATES:
        return ""
    n = str(d.get("round", ""))
    if live_round is not None and n == str(live_round):
        return ""
    # Only a round this Fletchery started and saw end (a cancel), or one started before this Fletchery did (a restart).
    started = _when(d.get("started"))
    if n not in {str(x) for x in ended} and not (started and booted and started < _when(booted)):
        return ""
    for step in d.get("steps", []) or []:
        if isinstance(step, dict) and step.get("state") in ("running", "waiting"):
            step["state"] = "todo"
    d["state"] = d["result"] = "stopped"
    d["note"] = note[:400]
    d["updated"] = d["ended"] = now_iso()
    write_file(path, d)
    return n


def _when(text):
    try:
        t = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


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
    """Classify an explicit interruption and older operator-stop reports separately from defects."""
    st = str(output.get("state", "")).lower()
    if st == "interrupted":
        return st
    # Older merger prompts required `failed` for every early stop. Inspect the
    # actual stop reason, never a reassuring summary that merely mentions Escape.
    reason = str(output.get("stop_reason") or output.get("gave_up") or "")
    if st != "landed" and operator_interruption(reason):
        return "interrupted"
    if st in ("landed", "nothing", "failed"):
        return st
    return "landed" if output.get("landed_pr") else "failed"


def output_cut_short(reason: str) -> bool:
    """The agent's reply hit its output limit (Codex: "Incomplete response returned, reason: max_output_tokens"). The
    round's work is fine and on disk, so it is an interruption to resume, not a failure that resets the worktree
    (LifeShare #1537, round 100 on 2026-11-10)."""
    return bool(re.search(r"max_output_tokens|incomplete response returned", reason or "", re.I))


def session_stop_result(error: str) -> str:
    """How a merger session that ended without a valid answer counts."""
    return "interrupted" if operator_interruption(error) or output_cut_short(error) else "failed"


def operator_interruption(reason: str) -> bool:
    """A reported physical/operator stop, not a general validation failure."""
    return bool(re.search(
        r"\bphysical\s+escape\b|\b(?:user|operator)\s+(?:(?:has|had)\s+)?(?:(?:explicitly|manually)\s+)?"
        r"(?:stopped|interrupted|cancelled|canceled|requested\s+(?:a\s+)?(?:stop|interruption|cancellation))\b|"
        r"\b(?:stop(?:ped)?|interrupt(?:ed)?|cancel(?:led|ed)?)\b.{0,80}\b(?:by\s+(?:the\s+)?(?:user|operator))\b",
        str(reason), re.I | re.S))


def deferred(item: dict) -> bool:
    """Untested/incomplete PRs are pending, not evidence of a broken PR head.

    New agents report the disposition. Legacy agents put all unlanded PRs in
    left_out, so recognize their explicit interruption/incomplete-acceptance
    reasons while retaining real compile/test/defect evidence.
    """
    if not isinstance(item, dict):
        return False
    disposition = str(item.get("disposition", "")).lower()
    if disposition in ("failed", "deferred"):
        return disposition == "deferred"
    reason = str(item.get("why", ""))
    confirmed = re.search(
        r"\b(?:compile|compilation|build|test|check|repair)\b.{0,35}\b(?:failed|failure|error)\b|"
        r"\bfailed\s+(?:compilation|compile|build|test|check)\b|\bconfirmed\s+(?:failure|defect|bug)\b",
        reason, re.I)
    pending = re.search(
        r"\binterrupt(?:ed|ion)\b|\bunattempted\b|\bnot\s+(?:attempted|validated|tested)\b|"
        r"\bacceptance\s+(?:is\s+)?incomplete\b|\bincomplete\s+(?:(?:final|visual|runtime|packaged)\s+)*acceptance\b",
        reason, re.I)
    return bool(pending and not confirmed)


def worktree_busy(path) -> list[str]:
    """Return process owners of this exact checkout, failing closed when Windows cannot be queried."""
    import os
    import subprocess
    if os.name != "nt" or not Path(path).exists():
        return []
    # CIM command lines may spell paths with either slash. A directory boundary avoids
    # treating merge-round-<id> as an owner of its quarantined merge-round predecessor.
    normalized = str(Path(path).resolve()).replace("\\", "/")
    spelled = re.escape(normalized)
    drive = re.match(r"([A-Za-z]):/(.*)", normalized)
    if drive:  # Git Bash / MSYS spell E:/x as /e/x (a `tail -f` on a round log was missed, 2026-11-07)
        spelled = f"(?:{spelled}|/{drive[1].lower()}/{re.escape(drive[2])})"
    pattern = ("(?i)" + spelled + r"(?=$|[\\/\s\"'])").replace("'", "''")
    ps = ("$ErrorActionPreference='Stop'; Get-CimInstance Win32_Process -ErrorAction Stop | "
          "Where-Object { $_.CommandLine -and $_.ProcessId -ne $PID -and "
          "$_.CommandLine.Replace('\\','/') -match '" + pattern + "' } | "
          "ForEach-Object { \"$($_.ProcessId)`t$($_.Name)`t$($_.CommandLine -replace '[\\r\\n\\t]', ' ')\" }")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True,
                             text=True, timeout=60, encoding="utf-8", errors="replace", check=True).stdout
    except (OSError, subprocess.SubprocessError) as e:
        raise RuntimeError(f"Cannot verify merge checkout ownership: {e}") from e
    owners = []
    for line in out.splitlines():
        pid, _, rest = line.strip().partition("\t")
        name, _, command = rest.partition("\t")
        if not pid:
            continue
        if log_follower(name, command, normalized):
            continue  # a `tail -f` left on a round's log reads one file; it owns nothing (2026-11-07, 3 h stall)
        owners.append(f"{pid} {name}".strip())
    return owners


def stop_log_followers(path) -> list[str]:
    """End `tail -f` log followers left on this merge checkout's Saved/ logs when its round finishes. Only processes
    `log_follower` matches for this exact checkout are stopped, by PID; nothing else is touched."""
    import os
    import subprocess
    if os.name != "nt" or not Path(path).exists():
        return []
    normalized = str(Path(path).resolve()).replace("\\", "/")
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='tail.exe'\" | "
          "ForEach-Object { \"$($_.ProcessId)`t$($_.Name)`t$($_.CommandLine -replace '[\\r\\n\\t]', ' ')\" }")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True,
                             text=True, timeout=60, encoding="utf-8", errors="replace").stdout
    except (OSError, subprocess.SubprocessError):
        return []
    stopped = []
    for line in out.splitlines():
        pid, _, rest = line.strip().partition("\t")
        name, _, command = rest.partition("\t")
        if pid.isdigit() and log_follower(name, command, normalized):
            try:
                subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                                f"Stop-Process -Id {int(pid)} -ErrorAction SilentlyContinue"], timeout=30)
                stopped.append(f"{pid} {name}")
            except (OSError, subprocess.SubprocessError):
                pass
    return stopped


_FOLLOW = re.compile(r"(?:^|\s)(?:-[A-Za-z0-9+]*f[A-Za-z0-9]*|--follow(?:=\S+)?|-F)(?=\s|$)")


def log_follower(name: str, command: str, checkout: str) -> bool:
    """A `tail -f`/`tail -F` whose only paths in the checkout are under Saved/ (logs and reports): it holds no build,
    editor or git state, and checkouts keep Saved/ when they are reused, so it must not block a round."""
    if (name or "").lower() not in ("tail.exe", "tail") or not _FOLLOW.search(command or ""):
        return False
    norm = (command or "").replace("\\", "/")
    root = checkout.replace("\\", "/").rstrip("/")
    drive = re.match(r"([A-Za-z]):/(.*)", root)
    roots = [re.escape(root)] + ([f"/{drive[1].lower()}/{re.escape(drive[2])}"] if drive else [])
    found = list(re.finditer("(?i)(?:" + "|".join(roots) + r")(/[^\s\"']*)?", norm))
    return bool(found) and all((m.group(1) or "").lower().startswith("/saved/") for m in found)


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


# The round agent's session never started (the CLI did not answer its first control request, or the connection
# dropped): nothing ran, the next round simply tries again. Filing these made unfixable project tasks (LifeShare
# #1201, round 39, 2026-11-07; round 40 landed normally).
SESSION_START_FLAKE = re.compile(r"Control request timeout: initialize|ECONNRESET|socket hang up", re.IGNORECASE)


def report_failure(gh, cfg, result: dict, worktree: str = "") -> int:
    """One durable task per recurring validation or orchestration failure, with a deduplicated occurrence for each round.
    A round whose agent session never started files nothing and returns 0."""
    import graph
    summary = str(result.get("summary", ""))
    reason = str(result.get("stop_reason") or result.get("error") or result.get("gave_up") or "")
    cause = reason or summary
    if SESSION_START_FLAKE.search(cause.splitlines()[0] if cause else ""):
        return 0
    signature = re.sub(r"fletchery/merge-round-[\w-]+", "<round-branch>", cause.splitlines()[0] if cause else "unknown")
    signature = re.sub(r"\b[0-9a-f]{40}\b", "<sha>", signature)
    signature = re.sub(r"merge-round(?:-[0-9a-f]{12})?", "<merge-checkout>", signature)
    key = hashlib.sha256(signature.encode()).hexdigest()[:20]
    marker = f"<!-- fletchery-merge-failure:{key} -->"
    occurrence = f"<!-- fletchery-merge-occurrence:{result.get('round')} -->"
    evidence = (f"Round {result.get('round')} ({result.get('agent') or 'unknown agent'}), {result.get('at', '')}.\n"
                f"Worktree: `{worktree}`. Base: `{cfg.base_branch}`.\n\n"
                f"```text\n{summary[:7000]}\n```\n\n"
                + (f"Stop/failure reason:\n\n```text\n{reason[:7000]}\n```\n\n" if reason else "") + occurrence)
    found = gh.marked_issues(marker)
    if found:
        number = found[0]["number"]
        thread = gh.issue_thread(number)
        if occurrence not in thread.get("body", "") and not any(occurrence in c.get("body", "") for c in thread.get("comments", [])):
            gh.comment(number, "Fletchery: merge failure repeated\n\n" + evidence)
        return number
    body = ("Fletchery's merge round failed. Diagnose and fix the underlying build, integration, harness or checkout failure; "
            "add a regression proving the failure is handled and the next round can start. Preserve generated "
            "content and other worktrees; never bypass leases, weaken checks or kill processes by name. "
            "Do not run an extra build or reset the active merge worktree from a worker.\n\n" + evidence + "\n\n" + marker)
    return gh.create_issue("Fix merge-round failure: " + signature[:150], body, [graph.PREFIX, graph.label_for(graph.QUEUED)])


def without_failed_dependencies(ready: list, excluded: dict, tasks: dict, known_closed: set[int] | None = None,
                                repository_closed: set[tuple[str, int]] | None = None) -> list:
    """Keep failed heads and their issue/PR dependency closure out of the candidate.

    A PR with a changed head is not itself excluded. Existing open task dependencies
    still protect its descendants until that prerequisite actually lands.
    """
    prs = {p.number: p for p in ready}
    declarations = {n: Task(n, p.title, p.body) for n, p in prs.items()}
    repository_closed = repository_closed or set()
    known_closed = set(known_closed or ()) | {n for n, t in tasks.items() if t.closed}
    tasks = {n: t for n, t in tasks.items() if not t.closed}
    blocked_prs = {n for n, p in prs.items() if excluded.get(str(n), {}).get("head") == p.head_sha}
    blocked_prs.update(n for n, t in declarations.items() if t.repository_dependencies - repository_closed)
    issue_prs: dict[int, set[int]] = {t.number: {t.pr_number} for t in tasks.values() if t.pr_number is not None}
    blocked_prs.update(n for numbers in issue_prs.values() for n in numbers if n not in prs)
    for n, p in prs.items():
        for t in tasks.values():
            if t.pr_number == n or re.search(r"\bcloses\s+#" + str(t.number) + r"\b", p.body or "", re.I) or p.head == t.branch:
                issue_prs.setdefault(t.number, set()).add(n)
    # Every entry is an open issue. Its dependent cannot land unless its prerequisite
    # is already closed (absent here) or has a PR included in this exact candidate.
    blocked_issues: set[int] = {n for n in tasks if not (issue_prs.get(n, set()) & prs.keys())}
    blocked_issues.update(n for n, t in tasks.items() if t.repository_dependencies - repository_closed)
    refs = {n: set(t.depends_on) for n, t in declarations.items()}
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


# ---- the round's long tests on a test node (workers.machines.roundTests) ----

IMPORTS = re.compile(r"^[ \t>*_`-]*lane-imports[*_`]*\s*:[*_`]*\s*\S", re.IGNORECASE | re.MULTILINE)


def _needs_imports(pr, tasks) -> bool:
    """A PR imports content when its body, or its task's issue body, has a `Lane-Imports:` line (a list bullet or bold
    counts). Round 138 went to the node with #1679, whose `- Lane-Imports: ImportEnvironment` was only in issue #974."""
    if IMPORTS.search(getattr(pr, "body", "") or ""):
        return True
    num, body = getattr(pr, "number", None), getattr(pr, "body", "") or ""
    for t in (tasks or {}).values():
        if getattr(t, "pr_number", None) == num or re.search(r"\bcloses\s+#" + str(getattr(t, "number", "")) + r"\b", body, re.I):
            if IMPORTS.search(getattr(t, "body", "") or ""):
                return True
    return False


def node_round(cfg, batch, coord, tasks=None) -> str:
    """Why this round can run all its Unreal work on a test node and leave this PC alone, or "" when it can't.
    workers.machines.roundTests {"whole": true} turns it on (Alex, 2026-11-12: "Why is Node still not merging?"). A batch
    with a "Lane-Imports:" PR still imports on this PC, and so does a round when no node can take the tests now."""
    conf = (getattr(cfg, "machines", None) or {}).get("roundTests")
    if not isinstance(conf, dict) or not conf.get("whole") or coord is None:
        return ""
    labels = [str(x) for x in conf.get("labels") or []]
    if any(_needs_imports(p, tasks) for p in batch):
        # Imports run on a node only when the profile allows it (roundTests.imports) and a node has a write key
        # (label role:imports); otherwise they stay on this PC (Alex, 2026-11-12: let the node push imported content).
        if not conf.get("imports"):
            return ""
        labels.append("role:imports")
        if (getattr(cfg, "machines", None) or {}).get("assetRoots"):
            # The node mirrors the PC's asset folders first; an older worker that can't would import from nothing.
            labels.append("cap:asset-sync")
    try:
        import machines
        names = coord.machines.eligible(labels, machines.RECIPES["node-checks"]["min_ram_gb"], "node-checks")
    except Exception:
        return ""
    return f"the Unreal work runs on {', '.join(names)}" if names and not coord.machines.shadow else ""


def node_tests_text(cfg, round_no, base: str, whole: bool = False) -> str:
    """With workers.machines.roundTests set, the merger runs the round's long test suites on a test node, so the PC is
    free while they run (Alex, 2026-11-11: "give me usage back to my PC"). The build, content imports and any check the
    plan says to run here stay on this PC. Without a node (exit 2) the tests run here as before."""
    import sys
    from pathlib import Path
    conf = (getattr(cfg, "machines", None) or {}).get("roundTests")
    if not conf:
        return ""
    labels = [str(x) for x in (conf.get("labels") or [])] if isinstance(conf, dict) else []
    branch = f"fletchery/round-{round_no}"
    tool = Path(__file__).resolve().with_name("machines.py")
    cmd = (f'& "{sys.executable}" "{tool}" --state-dir "{cfg.state_dir}" run --recipe node-checks --sha <full SHA> '
           f'--only <Name1,Name2,...> --requested-by "round {round_no}"' + "".join(f" --label {x}" for x in labels))
    if whole:
        return ("\n\n# This round runs on the test node: no Unreal on this PC\n\n"
                "This overrides where every build and test runs; what to test is unchanged. The PC is kept free for "
                "coding: on this PC do only git (merging, conflict fixes, commits, pushes) and reading. Do not run "
                "Tools/Build.ps1, BuildContent, imports, RunAllTests (except -PlanOnly), NavCheck, packaging, the "
                "editor or the game here, and do not take the PC heavy lease.\n"
                f"1. Merge the PRs and commit the candidate, then push it to the throwaway branch `{branch}` only: "
                f"`git push --force origin HEAD:refs/heads/{branch}`. Never push the candidate anywhere else before it "
                "lands.\n"
                f"2. Work out the checks the plan needs: `Tools/RunAllTests.ps1 -Set Auto -BaseRef origin/{base} "
                "-PlanOnly` (it lists them and whether strict navigation is needed; it launches nothing).\n"
                f"3. Run, from the worktree: `{cmd}`, adding `--strict-slots` when the plan wants strict navigation. "
                "The node builds the editor, runs BuildContent and the checks, then prints the result and the failing "
                "output. It can take an hour; wait for it.\n"
                "   - Exit 0: the build and those checks passed on the node; record them as passed for this "
                "candidate.\n"
                "   - Exit 1: the build or a check failed on the node; treat it exactly as a failure here (fix within "
                "scope, or defer the PR that causes it), commit, push the branch again and rerun step 3.\n"
                "   - Exit 2: no result (no node free, node lost or out of memory): run step 3 once more. If it gives "
                "2 again, stop and return `waiting` with the reason; do not run the checks on this PC.\n"
                "   - Content imports: when a PR in the batch has a `Lane-Imports:` line, add `--imports <the steps, comma "
                f"separated, in the PR's order> --push-branch {branch}` to step 3. The node builds the editor, runs those "
                "imports, commits the content and pushes it to that branch, then tests that commit. On exit 0, "
                f"`git fetch origin {branch}` and `git merge --ff-only FETCH_HEAD`: that commit, with the imported content, "
                "is the candidate you land. Do not run the imports on this PC, with one exception: if that run gives exit 2 "
                "because no node has the `role:imports` label, run the PR's imports on this PC as a normal round would "
                "(build the editor, run the imports, commit the content), push the branch again and rerun step 3 without "
                "`--imports`. Never stop the round only because no node can import.\n"
                "4. A check the plan says must run on this PC (packaged checks, a second player) is left for the "
                "playtest list or the nightly; say so in the summary instead of running it here.\n"
                "5. Land only after a node pass on the exact commit you land.\n"
                f"6. When the round ends, landed or not, delete the branch: `git push origin --delete {branch}`.\n")
    return ("\n\n# Run the long tests on the test node\n\n"
            "This overrides where the test suites run; what to test is unchanged. Build the editor and import content on "
            "this PC as usual. Then, for the RunAllTests suites (specs, editor and game tests, Flow, smoke), do not run "
            "them on this PC. Instead:\n"
            f"1. Commit the candidate, then push it to the throwaway branch `{branch}` only: "
            f"`git push --force origin HEAD:refs/heads/{branch}`. Never push the candidate anywhere else before it lands.\n"
            f"2. Work out the test names the plan needs (for LifeShare: `Tools/RunAllTests.ps1 -Set Auto -BaseRef "
            f"origin/{base} -PlanOnly` lists them).\n"
            f"3. Run, from the worktree: `{cmd}`. It waits for the node and prints the result and the failing output.\n"
            "   - Exit 0: those tests passed on the node; record them as passed for this candidate.\n"
            "   - Exit 1: they failed on the node; treat it exactly as a failure here (fix within scope, or defer the PR "
            "that causes it), then push again and rerun.\n"
            "   - Exit 2: no result (no node free, node lost or out of memory): run the same tests on this PC as usual.\n"
            "4. Strict NavCheck, packaged checks and anything the project rules say must run on this PC still run here.\n"
            f"5. When the round ends, landed or not, delete the branch: `git push origin --delete {branch}`.\n")


# ---- playtest acceptance (workers.mergePlaytest) ----

def playtest_text(cfg) -> str:
    """The merger's instruction when the project validates people-only acceptance in playtests (Alex, 2026-11-07:
    "For two player items, we will validate them later via play tests. Ensure changes merge for them")."""
    if not getattr(cfg, "merge_playtest", False):
        return ""
    return ("\n\n# Acceptance that needs people: land it, list it for the playtest\n\n"
            "This overrides the deferral rule above and any project note that says to defer such PRs. When a PR's only "
            "unrun acceptance needs people or hardware this PC does not have (a second PC or player, a Steam invite/join, "
            "listen-host and clients, a packaged or foreground-window check that needs desktop observation, a manual "
            "visual or in-engine review with no harness), do not defer it. Land it once its build, content imports and "
            "every automated check that can run here pass. For each such check add "
            "`{\"pr\": N, \"check\": \"what a person must verify, in one line\"}` to the optional `playtest` array of your "
            "answer, and list them under \"Playtest checks\" in the round PR body. Fletchery puts them on the project's "
            "playtest checklist. This does not cover missing inputs (an absent delivery or input folder): those are still "
            "deferred with the reason. A failing automated check is still a failure.\n\n"
            "# Acceptance this PC can't run yet: land it, list what is still owed\n\n"
            "The same applies (Alex, 2026-11-09) when a PR's only unrun named acceptance has no spec, save fixture, "
            "scene or harness in the repo yet, or needs a run that does not fit the round's validation window (for "
            "example a before-and-after baseline of over 60 minutes). Do not write the missing fixture inside the round "
            "and do not defer: land the PR once its build, content imports and every automated check that can run here "
            "pass, and add a `playtest` item whose check starts with \"Follow-up:\" and names the spec, fixture or long "
            "run still owed, so it can be built and run later. Still defer a PR that waits on another PR that has not "
            "landed, or on a missing input.\n")


def playtest_lines(round_no, items: list, merged: list) -> list[str]:
    """Checklist lines for the playtest issue: only for PRs that actually landed."""
    landed = {int(x) for x in merged or []}
    out = []
    for item in items or []:
        if isinstance(item, dict) and str(item.get("pr", "")).isdigit() and int(item["pr"]) in landed:
            check = " ".join(str(item.get("check", "")).split())[:300]
            if check:
                out.append(f"- [ ] #{int(item['pr'])} {check} (round {round_no})")
    return out


_PEOPLE_ONLY = re.compile(r"two[- ]?(?:pc|player|machine)|second pc|steam|listen[- ]host|clients?\b|multiplayer|joined|"
                          r"manual|desktop observation|foreground|no harness|harness|in-engine review|screenshot|capture",
                          re.I)
_MISSING_INPUT = re.compile(r"missing input|input folder|is (?:still )?absent|delivery is absent|not on this pc", re.I)


_NOT_RUNNABLE_YET = re.compile(r"\bno (?:spec|fixture|save fixture|reliable [\w-]+ setup|harness)\b|has no (?:[\w-]+ )?"
                               r"(?:spec|fixture)|exceeds the round'?s? (?:\d+-minute )?validation window|"
                               r"up to \d+ ?min(?:utes)? each|baseline run", re.I)
_WAITS_ON_PR = re.compile(r"\b(?:depends on|waits? on|wait for|must not merge before)\b[^.]*#\d+", re.I)


def people_only(why: str) -> bool:
    """A deferral that only needs people, a harness or fixture that doesn't exist yet, or a run too long for a round;
    not missing inputs, a PR it waits on, or defects."""
    why = why or ""
    return (bool(_PEOPLE_ONLY.search(why) or _NOT_RUNNABLE_YET.search(why))
            and not _MISSING_INPUT.search(why) and not _WAITS_ON_PR.search(why))


def unpark_people_only(parked: dict) -> list[int]:
    """With playtest acceptance on, PRs parked only for people-only acceptance go back into rounds."""
    gone = [int(n) for n, v in parked.items() if str(n).isdigit() and v.get("parked") and people_only(v.get("why", ""))]
    for n in gone:
        parked.pop(str(n), None)
    return sorted(gone)


def is_parked(parked: dict, number: int, head: str, now: datetime | None = None) -> bool:
    """Parked at this head and still inside the park window."""
    entry = parked.get(str(number)) or {}
    if not entry.get("parked") or entry.get("head") != head:
        return False
    now = now or datetime.now(timezone.utc)
    try:
        since = datetime.fromisoformat(str(entry["parked"]).replace("Z", "+00:00"))
    except ValueError:
        return True
    return now - since < timedelta(hours=PARK_HOURS)


# ---- balance: value order and parking (loop.py) ----

PARK_AFTER = 2          # deferrals at one head before the PR stops taking a round slot
PARK_HOURS = 72         # a parked PR is tried again after this long even with no new commits
_UNTRIED = re.compile(r"\bnot\s+(?:yet\s+)?(?:attempted|reached|run)\b|\bunattempted\b|\binterrupt", re.I)


PRIORITY_LABEL = "merge-next"   # a person (or a thread) wants this PR in the next round, ahead of the queue


def by_value(ready: list, tasks: dict) -> list:
    """PRs labelled `merge-next` first, then most queued work unblocked, then oldest: a PR that sets 14 tasks moving goes
    before one that sets none."""
    import graph

    def value(p) -> int:
        linked = [t for t in tasks.values() if t.pr_number == p.number or p.head == t.branch
                  or re.search(r"\bcloses\s+#" + str(t.number) + r"\b", p.body or "", re.I)]
        return max((len(graph.unblocks(tasks, t.number)) for t in linked), default=0)
    def first(p) -> int:
        return 0 if PRIORITY_LABEL in [str(x).lower() for x in (getattr(p, "labels", None) or [])] else 1
    return sorted(ready, key=lambda p: (first(p), -value(p), p.number))


def record_deferrals(parked: dict, batch: list, deferred: list, round_no, at: str) -> list[int]:
    """Count deferrals per PR head; returns the PRs that just became parked. A deferral because the round never got
    to the PR (not attempted, interrupted) is not evidence that this PC cannot accept it, so it does not count."""
    heads = {p.number: p.head_sha for p in batch}
    newly = []
    for item in deferred or []:
        if not isinstance(item, dict) or not str(item.get("pr", "")).isdigit():
            continue
        number = int(item["pr"])
        why = str(item.get("why", ""))
        if number not in heads or _UNTRIED.search(why):
            continue
        old = parked.get(str(number)) or {}
        count = old.get("count", 0) + 1 if old.get("head") == heads[number] else 1
        parked[str(number)] = {"head": heads[number], "count": count, "why": why[:1000], "round": round_no,
                               "at": at, "parked": old.get("parked") or (at if count >= PARK_AFTER else "")}
        if count >= PARK_AFTER and not old.get("parked"):
            newly.append(number)
    return newly


def not_parked(ready: list, parked: dict, now: datetime | None = None) -> list:
    """Drop parked PRs whose head is unchanged; a new head or PARK_HOURS unparks (the entry is removed)."""
    now = now or datetime.now(timezone.utc)
    out = []
    for p in ready:
        entry = parked.get(str(p.number))
        if entry and entry.get("head") != p.head_sha:
            parked.pop(str(p.number), None)
            entry = None
        if entry and entry.get("parked"):
            try:
                since = datetime.fromisoformat(str(entry["parked"]).replace("Z", "+00:00"))
            except ValueError:
                since = now
            if now - since < timedelta(hours=PARK_HOURS):
                continue
            parked.pop(str(p.number), None)
        out.append(p)
    return out



# ---- a failed round splits itself: Fletchery finds the PR that broke it (Alex, 2026-11-12) ----
# Rounds 136-140 landed nothing: one bad PR (or one cancelled test) failed the whole batch and the merger named no
# culprit, so every healthy PR waited for the next full round. Now, when a round of several PRs fails without naming
# the PR at fault, the next rounds take that batch in halves: a half that passes lands, a half that fails is halved
# again, and a PR that fails on its own is left out (merge_excluded) and goes to its fixer. At most log2(n) extra
# rounds, each smaller and quicker than the one before.

# A stop reason that is no verdict on the PRs (a test cancelled or never started, a node lost, a timeout). A PR that
# fails alone with one of these is not left out; it goes back to normal rounds.
NO_VERDICT = re.compile(r"\bcancel(?:l?ed)?\b|\btimed?[- ]?out\b|\btimeout\b|without a result|\bno result\b|"
                        r"(?:had )?not (?:yet )?started|node (?:was )?(?:lost|offline|went away)|out of memory|"
                        r"no node|\bexit 2\b|control request timeout|econnreset|socket hang up|\bbase is red\b", re.I)


def split_halves(prs: list, tasks: dict, known_closed: set | None = None) -> list[list]:
    """Two groups from a failed batch, in its order. A PR whose prerequisite PR falls in the second half moves there
    too, so each half can land on its own. A batch of one, or one that can't be split, comes back whole."""
    if len(prs) < 2:
        return [list(prs)]
    half = (len(prs) + 1) // 2
    first = without_failed_dependencies(list(prs[:half]), {}, tasks, known_closed)
    keep = {p.number for p in first}
    second = [p for p in prs if p.number not in keep]
    if not first or not second:
        return [list(prs)]
    return [first, second]


def split_group(prs: list) -> list[dict]:
    return [{"pr": p.number, "head": p.head_sha} for p in prs]


def split_batch(split: dict, ready: list) -> list:
    """The PRs of the split's first group that are still ready at the same head. Groups whose PRs all changed or went
    away are dropped (a new head is a new candidate and goes back to normal rounds). [] when the split is done."""
    groups = split.get("groups") or []
    while groups:
        wanted = {(int(x["pr"]), str(x["head"])) for x in groups[0] if isinstance(x, dict) and str(x.get("pr", "")).isdigit()}
        members = [p for p in ready if (p.number, p.head_sha) in wanted]
        if members:
            groups[0] = split_group(members)
            return members
        groups.pop(0)
    split.clear()
    return []


def after_round(split: dict, batch: list, res: dict, in_split: bool, halves) -> tuple[dict, list[tuple[int, str]]]:
    """The split queue after a round, and the PRs to leave out [(pr, why)]. `halves(prs)` splits a batch in two.

    - A round of several PRs that failed, landed none and named no culprit: its batch is halved (a new split, or this
      group replaced by its halves).
    - A round of one PR that failed and named no culprit: that PR is left out, unless the reason is no verdict on it.
    - Any other result of a split round that ran (landed, nothing, failed with a culprit named): its group is done.
    - Not the PRs' fault (a limit, an unreachable model, a busy checkout, an interruption): the split is unchanged.
    """
    split = dict(split or {})
    groups = [g for g in split.get("groups") or []]
    result = str(res.get("result", ""))
    why = str(res.get("stop_reason") or res.get("summary") or "")
    named = bool(res.get("left_out")) or bool(res.get("merged"))
    culprits: list[tuple[int, str]] = []
    if result not in ("landed", "nothing", "failed"):
        return (split if groups else {}), culprits
    if in_split and groups:
        groups.pop(0)
    if result == "failed" and not named and batch:
        if len(batch) > 1:
            parts = [g for g in halves(batch) if g]
            if len(parts) > 1:
                groups = [split_group(g) for g in parts] + groups
            elif in_split and not NO_VERDICT.search(why):
                # Prerequisites tie the batch together: it fails as one, and its PRs wait for a change.
                culprits = [(p.number, f"Round {res.get('round')} failed with this PR and the PRs it depends on, which "
                                       f"cannot be tried apart: {why}") for p in batch]
        elif in_split and not NO_VERDICT.search(why):
            # Only inside a split: a lone round outside one names its culprit itself (left_out) or blames none.
            p = batch[0]
            culprits = [(p.number, f"Round {res.get('round')} tried this PR on its own and it failed: {why}")]
    if not groups:
        return {}, culprits
    split["groups"] = groups
    if result == "failed" and not named:  # the latest failure is the one the next part checks first
        split["from_round"], split["why"] = res.get("round"), why[:2000]
    return split, culprits


def split_text(split: dict, batch: list, base: str = "") -> str:
    """The merger's instruction in a round that tries part of a failed batch."""
    if not split:
        return ""
    return ("\n\n# This round tries part of a failed batch\n\n"
            f"Round {split.get('from_round')} failed with a larger batch and named no PR at fault. Fletchery is now "
            "trying that batch in parts to find the PR that broke it; this round has "
            + ", ".join(f"#{p.number}" for p in batch) + ". The failure was:\n\n"
            f"```text\n{str(split.get('why', ''))[:1500]}\n```\n\n"
            "Run the check that failed first, straight after the build (and content steps it needs), before any other "
            "check. If it fails again here, stop and return `failed` with that reason at once: do not repair and do not "
            "bisect, Fletchery splits this group again itself. If you can see which PR causes it, name it in `left_out`. "
            "If it passes, carry on with the rest of the validation plan and land this group as usual.\n"
            + ("This round has one PR, so a failure here leaves it out of rounds. Before you return `failed`, run that "
               f"same check once on `origin/{base or 'the base branch'}` alone, without the PR. If it fails "
               "there too, the PR is not at fault: start `stop_reason` with \"Base is red:\" and the failure.\n"
               if len(batch) == 1 else ""))
