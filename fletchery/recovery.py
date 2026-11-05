"""Read-only ownership checks for an explicitly authorized saved merge candidate."""
import re
from pathlib import Path

import rounds


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
