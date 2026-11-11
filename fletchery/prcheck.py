"""PC check: build and quick-test a worker's PR on the PC before it is labelled for merging.

Workers cannot compile or run the project's native tests, so without this the first build of their code is a merge
round, where one compile error or failing spec costs a full candidate build, a test run and a bisect (LifeShare,
2026-11-06/07: #1039, #1030, #1032, #1034, #1049 were each found broken inside a round). With `workers.prCheckCommand`
set, a worker's PR opens without the merge label. Between rounds, in the same incremental build checkout the rounds
use, Fletchery merges the PR onto the base branch and runs the project's command (for LifeShare: build, then scans and
native specs). Exit 0 labels the PR for merging. Any other failure is posted on the PR as lane feedback, which sends it
back to its worker's fixer. Exit 2 means "no result" (the PC was busy): nothing is recorded and it runs again later.
"""
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

NO_RESULT = 2
TAIL_LINES = 80


@dataclass
class CheckResult:
    code: int          # 0 pass, NO_RESULT busy/unavailable, anything else a failure
    output: str        # tail of the command's output, or the reason


def candidates(prs, cfg, checked: dict, label: str, prefix: str) -> list:
    """Worker PRs into the base branch that are not labelled for merging yet and have not been checked at this head,
    oldest first."""
    out = []
    for p in prs:
        labels = [x.lower() for x in p.labels]
        if p.base != cfg.base_branch or getattr(p, "draft", False) or prefix.lower() not in labels:
            continue
        if label.lower() in labels:
            continue
        if (checked.get(str(p.number)) or {}).get("head") == p.head_sha:
            continue
        out.append(p)
    return sorted(out, key=lambda p: p.number)


def _tail(text: str) -> str:
    lines = (text or "").replace("\r\n", "\n").rstrip().split("\n")
    return "\n".join(lines[-TAIL_LINES:])


def run(repo, pr, cfg, timeout_s: int | None = None) -> CheckResult:
    """Merge the PR head onto the base branch in the shared build checkout and run the project's PC check."""
    import rounds
    branch = f"fletchery/pr-check-{pr.number}"
    try:
        path = rounds.prepare_worktree(repo, branch, cfg.heavy_lease_file)
    except rounds.CheckoutBusy as e:
        return CheckResult(NO_RESULT, f"build checkout busy: {e}")
    except (rounds.CheckoutRecovery, RuntimeError) as e:
        return CheckResult(NO_RESULT, f"build checkout unavailable: {e}")
    try:
        repo.git("fetch", repo.remote, pr.head_sha, cwd=path)
    except RuntimeError as e:
        return CheckResult(NO_RESULT, f"could not fetch {pr.head_sha[:10]}: {e}")
    try:
        repo.git("merge", "--no-ff", "--no-edit", "-m", f"PC check: merge PR #{pr.number} ({pr.head_sha[:10]})",
                 pr.head_sha, cwd=path)
    except RuntimeError as e:
        try:
            repo.git("merge", "--abort", cwd=path, check=False)
        except Exception:
            pass
        return CheckResult(1, f"PR #{pr.number} does not merge cleanly onto {cfg.base_branch}:\n{e}")
    env = dict(os.environ, FLETCHERY_PR=str(pr.number), FLETCHERY_PR_HEAD=pr.head_sha,
               FLETCHERY_BASE_REF=f"{repo.remote}/{repo.base}")
    flags = 0x00004000 if sys.platform == "win32" else 0  # BELOW_NORMAL_PRIORITY_CLASS: the PC is shared
    timeout = timeout_s or int(float(cfg.pr_check_timeout_minutes or 60) * 60)
    try:
        p = subprocess.run(cfg.pr_check_command, shell=True, cwd=str(path), env=env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout, creationflags=flags)
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") if isinstance(e.stdout, str) else ""
        return CheckResult(NO_RESULT, f"PC check timed out after {timeout // 60} min; it runs again later\n{_tail(out)}")
    except OSError as e:
        return CheckResult(NO_RESULT, f"PC check could not start: {e}")
    return CheckResult(p.returncode, _tail((p.stdout or "") + ("\n" + p.stderr if p.stderr else "")))


def failure_comment(pr, result: CheckResult, base: str, where: str = "the build PC") -> str:
    """Starts with a lane-feedback marker ("failed") so the worker's fixer picks it up."""
    return (f"PC check failed on {where} for head `{pr.head_sha[:10]}` (merged onto `{base}`, then built and "
            f"quick-tested). Fix this on the branch; the PC check runs again on the new head, and the PR is labelled "
            f"for merging once it passes.\n\n```\n{result.output[-6000:]}\n```")
