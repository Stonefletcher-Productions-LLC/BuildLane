"""Read-only ownership checks for an explicitly authorized saved merge candidate."""
import re
from pathlib import Path

import rounds


def reconcile_landed_round(engine, data: dict) -> dict:
    """Clear a saved round only after GitHub confirms its exact PRs landed."""
    state, cfg = engine.state, engine.cfg
    resume, last = state.merge_resume, state.merge_last
    number, landing = data.get("round"), data.get("landing_pr")
    if (type(number) is not int or type(landing) is not int or landing <= 0
            or engine.round_job is not None or number != resume.get("round") or number != last.get("round")):
        raise ValueError("Name the current saved round and its landing PR.")
    if not (state.planned_integration or state.merge_held):
        raise ValueError("Enable planned windows or pause merging before clearing recovery.")
    heads = resume.get("prs") or {}
    branch = (resume.get("checkpoint") or {}).get("branch")
    if not heads or not branch or not branch.startswith(f"fletchery/merge-round-{number}-"):
        raise ValueError("Saved recovery has no verifiable round branch or PR heads.")

    def merged_pr(pr):
        result = engine.ctx.gh.api(f"repos/{cfg.repo}/pulls/{pr}")
        if result.get("merged") is not True or result.get("base", {}).get("ref") != cfg.base_branch:
            raise ValueError(f"PR #{pr} has not merged into {cfg.base_branch}.")
        return result

    landed = merged_pr(landing)
    commit = landed.get("merge_commit_sha") or ""
    if landed.get("head", {}).get("ref") != branch or not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise ValueError("The landing PR does not match the saved round branch.")
    for pr, head in heads.items():
        if not pr.isdigit() or merged_pr(pr).get("head", {}).get("sha") != head:
            raise ValueError(f"PR #{pr} does not match the saved head.")
    if (state.merge_resume is not resume or state.merge_last is not last or engine.round_job is not None
            or not (state.planned_integration or state.merge_held)):
        raise ValueError("Merge state changed while verifying the landing.")
    state.merge_last = {**last, "result": "landed", "pr": landing, "merged": sorted(map(int, heads)),
                        "landed_commit": commit, "stop_reason": "",
                        "reconciled_stop_reason": last.get("stop_reason", ""),
                        "summary": f"GitHub verified PR #{landing} and all saved PR heads merged into {cfg.base_branch}."}
    state.merge_resume = {}
    try:
        state.save(engine.state_dir / "state.json")
    except Exception:
        state.merge_last, state.merge_resume = last, resume
        raise
    engine.wake()
    return {"round": number, "landing_pr": landing, "commit": commit, "reconciled": True}


def resume_worktree(repo, checkpoint: dict, lease_file: str = "") -> Path:
    path = Path(checkpoint.get("worktree") or "").resolve()
    if not Path(checkpoint.get("worktree") or "").is_absolute() or path.parent != Path(repo.worktree_root).resolve():
        raise RuntimeError("Saved candidate must be an immediate checkout under the configured worktree root; preserved")
    branch, head = checkpoint.get("branch", ""), checkpoint.get("head", "")
    if not branch.startswith("fletchery/merge-round-") or not re.fullmatch(r"[a-f0-9]{40}", head):
        raise RuntimeError("Saved candidate needs its exact branch and full commit before resuming; preserved")
    rounds.checkout_guard(path, lease_file)
    if not (path / ".git").exists() or path not in rounds._registered_checkouts(repo):
        raise RuntimeError("Saved candidate is not a registered checkout of this repository; preserved")
    if repo.git("symbolic-ref", "--quiet", "--short", "HEAD", cwd=path) != branch:
        raise RuntimeError("Saved candidate branch changed; reconcile before resuming; preserved")
    if repo.git("rev-parse", "HEAD", cwd=path) != head:
        raise RuntimeError("Saved candidate commit changed; reconcile before reusing validation; preserved")
    rounds.checkout_guard(path, lease_file)
    # Dirty, staged, untracked and ignored generated assets are deliberately kept.
    # Fingerprints of all validation inputs must still be checked by the merger.
    return path


def ready_signal(state_dir: Path, wait_for: dict) -> bool:
    """A unique completion receipt prevents a short lease gap or stale receipt resuming acceptance."""
    request = str(wait_for.get("request_id", ""))
    if not re.fullmatch(r"[a-f0-9]{32}", request):
        return False
    receipt = rounds.read_file(str(Path(state_dir) / ("resume-ready-" + request + ".json")))
    return receipt.get("request_id") == request and receipt.get("complete") is True


def handoff_status(state_dir: Path, wait_for: dict, now=None) -> tuple[bool, str]:
    """Bound chat handoffs without treating missing external acceptance as a pass."""
    from datetime import datetime, timezone
    import math
    now = now or datetime.now(timezone.utc)
    if ready_signal(state_dir, wait_for):
        return True, ""
    try:
        started = datetime.fromisoformat(wait_for.get("started_at", "").replace("Z", "+00:00"))
        if started.tzinfo is None or started > now:
            raise ValueError("Invalid handoff timestamp")
    except (ValueError, TypeError, AttributeError):
        started = now
        wait_for["started_at"] = now.isoformat()
    remaining = 900 - (now - started).total_seconds()
    name = str(wait_for.get("name") or "external acceptance")
    if remaining > 0:
        return False, f"Saved round waits for {name}; merger will verify directly in {math.ceil(remaining / 60)} min if no completion signal arrives"
    return True, f"External handoff timed out: {name}. Completion is UNCONFIRMED. Verify affected inputs and required acceptance directly; do not treat this timeout as a passing check."


def release_handoff(resume: dict, reason: str) -> None:
    """Keep the missing evidence visible to the merger after releasing scheduling."""
    wait_for = resume.pop("wait_for", None)
    if wait_for and reason:
        progress = resume.setdefault("progress", {})
        progress["note"] = str(progress.get("note", "")) + "\n" + reason
        resume["handoff_verification"] = {"wait_for": wait_for, "reason": reason, "verified": False}


def reconcile_runtime_resume(engine, data: dict) -> dict:
    """Explicitly adopt a verified current checkpoint after a transport-lost merger session."""
    import copy
    last = engine.state.merge_last
    reason = str(last.get("stop_reason") or last.get("summary") or "")
    if engine.round_job is not None or int(data.get("round", -1)) != int(last.get("round", -2)):
        raise ValueError("Runtime recovery must name the current stopped round")
    if last.get("result") not in ("failed", "crashed", "unreachable", "interrupted") or not re.search(
            r"workspace routing discovery timed out|Reconnecting\.\.\.|connection reset|transport error", reason, re.I):
        raise ValueError("Runtime recovery requires a recorded transport interruption, not a validation failure")
    if data.get("stop_reason") != reason:
        raise ValueError("Runtime recovery must preserve the actual recorded interruption")
    checkpoint = dict(data.get("checkpoint") or {})
    previous = engine.state.merge_resume
    old_path = (previous.get("checkpoint") or {}).get("worktree") or last.get("worktree")
    if not old_path or Path(checkpoint.get("worktree") or "").resolve() != Path(old_path).resolve():
        raise ValueError("Runtime recovery cannot replace the preserved checkout")
    path = resume_worktree(engine.ctx.repo, checkpoint)
    progress = rounds.read_file(engine.cfg.merge_round_file or str(engine.state_dir / "merge-round.json"))
    heads = {str(n): str(h) for n, h in (data.get("prs") or {}).items()}
    if (str(progress.get("round")) != str(last["round"]) or progress.get("branch") != checkpoint["branch"]
            or not heads or set(heads) != {str(n) for n in progress.get("prs", [])}
            or not set(heads).issubset(previous.get("prs") or {})
            or any(not n.isdigit() or not re.fullmatch(r"[a-f0-9]{40}", h) for n, h in heads.items())):
        raise ValueError("Runtime recovery heads must match the preserved round's current progress")
    excluded = list(data.get("left_out") or [])
    removed = set(previous.get("prs") or {}) - set(heads)
    if {str(item.get("pr")) for item in excluded} != removed or any(
            not item.get("why") or item.get("disposition") != "failed" for item in excluded):
        raise ValueError("Every removed PR requires explicit confirmed-failure evidence")
    ready = {str(p.number): p for p in engine.ctx.gh.labelled_prs(engine.cfg.merge_label or "ready", engine.cfg.base_branch)}
    for item in excluded:
        if not re.fullmatch(r"[a-f0-9]{40}", str(item.get("head", ""))):
            raise ValueError("Removed PR evidence requires an exact head")
    for head in list(heads.values()) + [item["head"] for item in excluded]:
        engine.ctx.repo.git("cat-file", "-e", head + "^{commit}", cwd=path)
    for number, head in heads.items():
        if number not in ready or ready[number].head_sha != head:
            raise ValueError("Runtime recovery head changed or is no longer ready")
        engine.ctx.repo.git("merge-base", "--is-ancestor", head, "HEAD", cwd=path)
    for item in excluded:
        number = str(item["pr"])
        if number not in ready or item.get("head") != ready[number].head_sha:
            raise ValueError("Removed PR evidence must bind its current ready head")
        try:
            engine.ctx.repo.git("merge-base", "--is-ancestor", item["head"], "HEAD", cwd=path)
        except RuntimeError:
            pass
        else:
            raise ValueError("Removed PR head is still included in the saved candidate")
    deferred = [{"pr": int(n), "head": h, "why": "Transport interrupted; continue matching-input checks and remaining acceptance",
                 "disposition": "deferred"} for n, h in heads.items()]
    # No state or checkout writes precede these ownership, head and inclusion checks.
    for item in excluded:
        engine.state.merge_excluded[str(item["pr"])] = {"head": item["head"], "why": str(item["why"])[:4000],
                                                       "round": last["round"], "at": rounds.now_iso()}
    last.update(result="interrupted", resume_authorized=True, key="", checkpoint=checkpoint,
                deferred=deferred, left_out=excluded)
    progress = copy.deepcopy(progress)
    progress["note"] = str(progress.get("note", "")) + "\nRuntime checkpoint metadata verified only. Verify source/content/external-input/binary fingerprints before reusing completed checks; remaining acceptance is pending."
    engine.state.merge_resume = {"round": last["round"], "authorized": True, "stop_reason": reason,
                                "checkpoint": checkpoint, "prs": heads, "progress": progress}
    engine.state.save(engine.state_dir / "state.json")
    engine.wake()
    return {"round": last["round"], "resume_authorized": True,
            "message": "Current saved candidate verified; remaining acceptance queued without resetting assets."}
