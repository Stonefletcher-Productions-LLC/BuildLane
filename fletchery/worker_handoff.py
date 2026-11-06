"""Cooperative handoff at a task boundary; no agent kill, reset, commit or push."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from graph import PR, QUEUED


class WorkerHandoffs:
    def __init__(self, engine):
        self.engine = engine
        interrupted = [r for r in self.records.values() if r.get("state") == "requested"]
        for record in interrupted:
            record.update(state="interrupted", note="Fletchery restarted before the worker reached a checkpoint. Existing task files were preserved.")
        if interrupted:
            self.save()

    @property
    def records(self):
        return self.engine.state.worker_handoffs

    def save(self):
        if not self.engine.dry_run:
            self.engine.state.save(self.engine.state_dir / "state.json")

    def view(self):
        return deepcopy(self.records)

    def request(self):
        active = sorted(self.engine.active)
        requested = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for task in active:
            self.records[str(task)] = {"task": task, "state": "requested", "requested": requested}
        self.engine.hold(True)
        return {"tasks": active, "message": "New work paused. Active workers will preserve their checkout at the next safe phase boundary."}

    def wanted(self, task):
        return self.records.get(str(task), {}).get("state") == "requested"

    def settled(self, outcome):
        record = self.records.get(str(outcome.task))
        if not record:
            return
        if record["state"] == "requested":
            record.update(state="finished", note="Session ended before another checkpoint boundary; its existing checkout is preserved.")
        elif record["state"] == "checkpointed" and outcome.result in ("pr", "fixed"):
            record.update(state="resumed", note="Later work continued and completed the ordinary checks.")

    def note(self, task, worktree):
        record = self.records.get(str(task), {})
        if record.get("state") != "checkpointed":
            return ""
        if Path(record["worktree"]).resolve() != Path(worktree).resolve():
            return ("\n\nA prior paused attempt is preserved at `" + record["worktree"]
                    + "`. Do not overwrite or reset it. Reconcile its changes explicitly if they are still applicable.")
        return ("\n\n## Resume saved worker handoff\n"
                f"Continue in this owned checkout. Previous phase: {record['phase']}; checkpoint HEAD `{record.get('head', 'unknown')}`.\n"
                f"Previous worker summary: {record.get('summary', '') or 'No summary returned.'}\n"
                f"Staged task files: {', '.join(record.get('staged', [])) or 'none reported'}.\n"
                "Inspect the current staged, unstaged and untracked changes before editing. Preserve useful work and rerun required checks; this handoff does not grant acceptance.")

    def checkpoint(self, task, out, worktree, phase, report=None, pr=None):
        if not self.wanted(task.number):
            return False
        report = report if isinstance(report, dict) else {}
        repo, cfg, gh = self.engine.ctx.repo, self.engine.cfg, self.engine.ctx.gh
        record = dict(self.records[str(task.number)], state="checkpointed", worktree=str(worktree),
                      phase=phase, agent=out.agent, summary=str(report.get("summary") or report.get("gave_up") or "")[:1500],
                      head="", staged=[], status="", note="Checkout preserved; no commit or push was made for the handoff.")
        try:
            if pr:
                repo.ensure_repair_checkout(worktree, pr.head, pr.head_sha, cfg.heavy_lease_file)
            else:
                repo.ensure_task_checkout(task.number, task.slug, task.branch, worktree, cfg.heavy_lease_file)
            record["head"] = repo.git("rev-parse", "HEAD", cwd=worktree)
            changed = set(repo.git("diff", "--name-only", "-z", cwd=worktree).split("\0")) - {""}
            # Stage enumerated tracked changes and explicitly reported new files
            # only. Unknown/ignored/generated files stay exactly where they are.
            untracked = set(repo.git("ls-files", "--others", "--exclude-standard", "-z", cwd=worktree).split("\0")) - {""}
            root = Path(worktree).resolve()
            for name in report.get("files") or []:
                candidate = (root / str(name)).resolve()
                if candidate.is_relative_to(root):
                    relative = candidate.relative_to(root).as_posix()
                    if relative in untracked:
                        changed.add(relative)
            paths = sorted(name for name in changed if (root / name).resolve().is_relative_to(root))
            if paths:
                repo.git("--literal-pathspecs", "add", "--", *paths, cwd=worktree)
            record["staged"] = sorted(set(repo.git("diff", "--cached", "--name-only", "-z", cwd=worktree).split("\0")) - {""})[:100]
            record["status"] = repo.git("status", "--short", cwd=worktree)[:4000]
        except Exception as exc:
            record["note"] = f"Checkout preserved. Staging could not be verified: {str(exc)[:500]}"
        self.records[str(task.number)] = record
        self.save()
        try:
            gh.set_status(task.number, PR if pr else QUEUED)
        except Exception as exc:
            self.engine.ctx.log(f"#{task.number}: handoff saved locally; issue status update deferred ({type(exc).__name__})")
        out.result, out.detail = "handed_off", f"Paused after {phase}; {record['note']}"
        return True
