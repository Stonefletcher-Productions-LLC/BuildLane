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
    if last and last.get("result") != "landed" and last.get("key") == key_of(ready) and last.get("at"):
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
    d["state"] = "done" if result == "landed" else "failed" if result in ("failed", "crashed") else "done"
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


def prepare_worktree(repo, branch: str) -> Path:
    """One worktree for every round (`<worktrees>/merge-round`), reset to origin/<base> on a new branch. Build output
    the project ignores (Binaries, Intermediate) stays, so the next round's build is incremental."""
    if not hasattr(repo, "git"):  # tests: FakeRepo
        return repo.worktree(0, "merge-round", branch)
    path = Path(repo.worktree_root) / "merge-round"
    repo.fetch_base()
    base = f"{repo.remote}/{repo.base}"
    if path.exists() and (path / ".git").exists():
        repo.git("merge", "--abort", cwd=path, check=False)
        repo.git("checkout", "--force", "-B", branch, base, cwd=path)
        repo.git("reset", "--hard", base, cwd=path)
        repo.git("clean", "-fd", cwd=path, check=False)
    else:
        Path(repo.worktree_root).mkdir(parents=True, exist_ok=True)
        if f"worktree {path}" in repo.git("worktree", "list", "--porcelain"):  # registered, folder gone: repair, never prune
            repo.git("worktree", "repair", str(path), check=False)
        repo.git("worktree", "add", "--force", "-B", branch, str(path), base)
    return path
