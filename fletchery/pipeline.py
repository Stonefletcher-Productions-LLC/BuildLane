"""The task pipeline (worker -> gates -> reviewer -> PR) and the fixer. One task at a time per call; the loop runs
several calls concurrently. Every outcome is a plain record the loop writes to the ledger and the status issue."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import gates as gates_mod
import rules as rules_mod
from config import Config
import graph
from graph import BLOCKED, PR, QUEUED, RUNNING, Task
from github import Feedback, PullRequest
from runtime import RunResult, Runtime, role_prompt


class Limited(Exception):
    """A session hit a usage or rate limit. Not a verdict on the task: it goes back to the queue untouched."""


class _LimitGuard(Runtime):
    """Every session in the pipeline goes through this: limits become an exception, tool calls go to the live view."""
    def __init__(self, inner: Runtime, ctx: "Context | None" = None, task: int = 0):
        self.inner = inner
        self.ctx = ctx
        self.task = task

    async def run(self, role, prompt, cwd, budget_usd, max_turns, on_action=None):
        live = self.ctx.live if self.ctx else None
        if live and self.task and role != "fixer":  # a fixer keeps "Fixing PR #n" from fix_pr
            live.stage(self.task, {"worker": "Writing", "reviewer": "Reviewing"}.get(role, role))
        cb = on_action or ((lambda text: live.action(self.task, text)) if live and self.task else None)
        r = await self.inner.run(role, prompt, cwd, budget_usd, max_turns, on_action=cb)
        if r.signed_out:
            raise Limited(r.error, r.cost_usd, "signed_out")
        if r.limited:
            raise Limited(r.error, r.cost_usd)
        return r


@dataclass
class Outcome:
    task: int
    result: str              # "pr" | "blocked" | "requeue" | "fixed" | "gave_up" | "limited"
    detail: str = ""
    pr_number: int | None = None
    cost_usd: float = 0.0
    sessions: list[str] = field(default_factory=list)


@dataclass
class Context:
    cfg: Config
    gh: object               # GitHub | FakeGitHub
    repo: object             # Repo | FakeRepo
    runtime: Runtime
    run_gates: Callable[[list[str], Path], list[gates_mod.GateResult]] = gates_mod.run
    log: Callable[[str], None] = print
    live: object | None = None  # live.Live: the dashboard's view of running work


async def run_task(task: Task, ctx: Context) -> Outcome:
    out = Outcome(task.number, "requeue")
    if ctx.live:
        ctx.live.stage(task.number, "Starting", task.title)
    try:
        return await _run_task(task, ctx, out)
    finally:
        if ctx.live:
            ctx.live.done(task.number)


async def _run_task(task: Task, ctx: Context, out: Outcome) -> Outcome:
    try:
        return await _run_task_body(task, ctx, out)
    except Limited as e:
        # The worktree and any commits stay; the next run continues from them (the worker prompt says so).
        ctx.gh.set_status(task.number, QUEUED)
        signed_out = len(e.args) > 2 and e.args[2] == "signed_out"
        out.result, out.detail = ("signed_out" if signed_out else "limited"), str(e.args[0])[:200]
        out.cost_usd += e.args[1] if len(e.args) > 1 else 0.0
        ctx.log(f"#{task.number} paused: {'Claude sign-in expired' if signed_out else 'usage limit'}")
        return out


async def _run_task_body(task: Task, ctx: Context, out: Outcome) -> Outcome:
    cfg, gh, repo = ctx.cfg, ctx.gh, ctx.repo
    rt = _LimitGuard(ctx.runtime, ctx, task.number)
    ctx.log(f"#{task.number} start: {task.title}")
    gh.set_status(task.number, RUNNING)
    _load_notes(ctx, task)
    try:
        repo.fetch_base()
        wt = repo.worktree(task.number, task.slug, task.branch)
    except Exception as e:
        return _block(ctx, out, f"could not make the worktree: {e}")
    task.rules = rules_mod.load(wt, cfg.project_root)

    # 1. worker
    freeze = _freeze_text(cfg)
    prompt = role_prompt("worker", task.rules, project=cfg.name, issue=_issue_text(task), branch=task.branch, base=cfg.base_branch, freeze=freeze)
    r = await rt.run("worker", prompt, wt, cfg.worker_budget_usd, cfg.worker_max_turns)
    _account(out, r)
    if not r.ok:
        return _requeue_or_block(ctx, task, out, f"worker session failed: {r.error}")
    if r.output.get("gave_up"):
        return _block(ctx, out, f"worker gave up: {r.output['gave_up']}")
    if repo.dirty(wt):
        repo.commit_all(wt, f"Fletchery: #{task.number} {task.title}\n\nCloses #{task.number}")
    if not repo.has_commits(wt):
        return _block(ctx, out, "worker made no commits")
    worker_report = r.output

    # 2. gates (a failure goes back to the worker once)
    results = _gates(ctx, task, wt)
    if not all(g.passed for g in results):
        fix_prompt = role_prompt("worker", task.rules, project=cfg.name, issue=_issue_text(task), branch=task.branch, base=cfg.base_branch,
                                 freeze=freeze) + "\n\n## Gate failures to fix\n" + _gate_output(results)
        r2 = await rt.run("worker", fix_prompt, wt, cfg.worker_budget_usd, cfg.worker_max_turns)
        _account(out, r2)
        if repo.dirty(wt):
            repo.commit_all(wt, f"Fletchery: #{task.number} gate fixes")
        results = _gates(ctx, task, wt)
        if not all(g.passed for g in results):
            return _block(ctx, out, "gates still fail after one fix pass:\n" + gates_mod.summary(results))

    # 3. reviewer (fresh context, read-only)
    review = await _review(ctx, task, wt, out)
    if review.get("verdict") == "needs_work":
        back = role_prompt("worker", task.rules, project=cfg.name, issue=_issue_text(task), branch=task.branch, base=cfg.base_branch, freeze=freeze) \
            + "\n\n## Review findings to address\n" + _findings_text(review)
        r3 = await rt.run("worker", back, wt, cfg.worker_budget_usd, cfg.worker_max_turns)
        _account(out, r3)
        if repo.dirty(wt):
            repo.commit_all(wt, f"Fletchery: #{task.number} review fixes")
        results = _gates(ctx, task, wt)
        if not all(g.passed for g in results):
            return _block(ctx, out, "gates fail after the review fixes:\n" + gates_mod.summary(results))
        review = await _review(ctx, task, wt, out)
    if review.get("verdict") == "escalate":
        return _block(ctx, out, "reviewer escalated:\n" + _findings_text(review))
    if review.get("verdict") != "approve":
        return _block(ctx, out, "reviewer still asks for changes after one fix pass:\n" + _findings_text(review))

    # 4. PR
    if ctx.live:
        ctx.live.stage(task.number, "Opening PR")
    try:
        repo.push(wt, task.branch)
        body = pr_body(task, worker_report, results, review, cfg)
        pr = gh.create_pr(task.branch, cfg.base_branch, f"#{task.number} {task.title}", body, [graph.PREFIX, "ready"],
                          cfg.assignee or None)
    except Exception as e:
        return _requeue_or_block(ctx, task, out, f"push or PR failed: {e}")
    gh.set_status(task.number, PR)
    gh.comment(task.number, f"Fletchery opened PR #{pr} from `{task.branch}` (cost ${out.cost_usd:.2f}). "
                            f"The Build Lane builds it; it still needs review before it merges.")
    out.result, out.pr_number, out.detail = "pr", pr, f"PR #{pr}"
    ctx.log(f"#{task.number} -> PR #{pr}")
    return out


async def fix_pr(task: Task, pr: PullRequest, feedback: list[Feedback], attempt: int, ctx: Context) -> Outcome:
    out = Outcome(task.number, "gave_up", pr_number=pr.number)
    if ctx.live:
        ctx.live.stage(task.number, f"Fixing PR #{pr.number}", task.title, kind="fix")
    try:
        return await _fix_pr(task, pr, feedback, attempt, ctx, out)
    except Limited as e:
        signed_out = len(e.args) > 2 and e.args[2] == "signed_out"
        out.result, out.detail = ("signed_out" if signed_out else "limited"), str(e.args[0])[:200]
        ctx.log(f"PR #{pr.number} fix paused: {'Claude sign-in expired' if signed_out else 'usage limit'}")
        return out  # the loop un-counts the attempt and forgets the feedback was seen, so it is retried
    finally:
        if ctx.live:
            ctx.live.done(task.number)


async def _fix_pr(task: Task, pr: PullRequest, feedback: list[Feedback], attempt: int, ctx: Context,
                  out: Outcome) -> Outcome:
    cfg, gh, repo = ctx.cfg, ctx.gh, ctx.repo
    rt = _LimitGuard(ctx.runtime, ctx, task.number)
    _load_notes(ctx, task)
    if attempt > cfg.fixer_attempts:
        gh.set_status(task.number, BLOCKED)
        gh.comment(task.number, f"Fletchery: PR #{pr.number} came back {attempt - 1} times; stopping. Last feedback:\n\n"
                                + "\n".join(f"> {f.text[:300]}" for f in feedback))
        out.detail = "fixer attempts exhausted"
        return out
    wt = repo.worktree(task.number, task.slug, task.branch)
    repo.fetch_base()
    task.rules = rules_mod.load(wt, cfg.project_root)
    fb = "\n\n".join(f"### {f.kind} ({f.at})\n{f.text}" for f in feedback)
    prompt = role_prompt("fixer", task.rules, project=cfg.name, issue=_issue_text(task), branch=task.branch, base=cfg.base_branch, pr=str(pr.number),
                         feedback=fb, freeze=_freeze_text(cfg))
    r = await rt.run("fixer", prompt, wt, cfg.fixer_budget_usd, cfg.fixer_max_turns)
    _account(out, r)
    if not r.ok or r.output.get("gave_up"):
        out.detail = r.error or r.output.get("gave_up", "")
        gh.comment(task.number, f"Fletchery fixer (attempt {attempt}) could not fix PR #{pr.number}: {out.detail}")
        return out
    if repo.dirty(wt):
        repo.commit_all(wt, f"Fletchery: #{task.number} fix after lane feedback")
    results = _gates(ctx, task, wt)
    if not all(g.passed for g in results):
        out.detail = "gates fail after the fix:\n" + gates_mod.summary(results)
        gh.comment(task.number, f"Fletchery fixer (attempt {attempt}): {out.detail}")
        return out
    repo.push(wt, task.branch)
    gh.comment(pr.number, f"Fletchery fixer (attempt {attempt}): {r.output.get('summary', '')}\n\n{gates_mod.summary(results)}\n\n"
                          "Pushed; the lane will take it again when it is labelled `ready`.")
    out.result, out.detail = "fixed", r.output.get("summary", "")
    return out


# ---- helpers ----

def _issue_text(task: Task) -> str:
    text = f"Issue #{task.number}: {task.title}\n\n{task.body or '(no body)'}"
    if task.notes:
        text += ("\n\n## Comments on the issue (oldest first; a later comment overrides an earlier one and the body)\n\n"
                 + task.notes)
    return text


def _load_notes(ctx: Context, task: Task) -> None:
    """People steer a task by commenting on its issue: answers to a block, changed scope, hints. Read them fresh
    before every session. A failure to read them is logged, not fatal."""
    try:
        notes = ctx.gh.issue_notes(task.number)
    except Exception as e:
        ctx.log(f"#{task.number}: could not read issue comments: {e}")
        return
    task.notes = "\n\n".join(f"**{who}** ({at}):\n{body.strip()}" for who, at, body in notes)


def _freeze_text(cfg: Config) -> str:
    if not cfg.code_freeze:
        return ""
    return ("The project is under a code freeze and this issue is an approved exception. Make the bounded change it asks "
            "for and nothing else: no new features, no scope growth, no opportunistic cleanup or refactors.")


def _account(out: Outcome, r: RunResult) -> None:
    out.cost_usd += r.cost_usd
    if r.session_id:
        out.sessions.append(f"{r.role}:{r.session_id}")


def _gates(ctx: Context, task: Task, wt: Path) -> list[gates_mod.GateResult]:
    cmds = gates_mod.select(ctx.repo.changed_files(wt), task.gates, task.rules)
    if ctx.live:
        ctx.live.stage(task.number, "Checking")
        for c in cmds:
            ctx.live.action(task.number, f"$ {c}")
    return ctx.run_gates(cmds, wt, fallback_root=ctx.cfg.project_root or None)


def _gate_output(results: list[gates_mod.GateResult]) -> str:
    return "\n".join(f"### `{g.command}`\n```\n{g.output}\n```" for g in results if not g.passed)


async def _review(ctx: Context, task: Task, wt: Path, out: Outcome) -> dict:
    cfg = ctx.cfg
    prompt = role_prompt("reviewer", task.rules, project=cfg.name, issue=_issue_text(task), branch=task.branch, base=cfg.base_branch)
    r = await _LimitGuard(ctx.runtime, ctx, task.number).run("reviewer", prompt, wt, cfg.reviewer_budget_usd, cfg.reviewer_max_turns)
    _account(out, r)
    if not r.ok:
        return {"verdict": "escalate", "findings": [{"file": "-", "line": 0, "rule": "review", "note": r.error}]}
    return r.output


def _findings_text(review: dict) -> str:
    fs = review.get("findings") or []
    return "\n".join(f"- `{f.get('file')}:{f.get('line')}` [{f.get('rule')}] {f.get('note')}" for f in fs) or "- (none)"


def _block(ctx: Context, out: Outcome, why: str) -> Outcome:
    ctx.gh.set_status(out.task, BLOCKED)
    ctx.gh.comment(out.task, f"Fletchery blocked this task (cost so far ${out.cost_usd:.2f}):\n\n{why}")
    out.result, out.detail = "blocked", why
    ctx.log(f"#{out.task} blocked: {why.splitlines()[0]}")
    return out


def _requeue_or_block(ctx: Context, task: Task, out: Outcome, why: str) -> Outcome:
    """Infrastructure failures (a crashed session, a push that bounced) are not verdicts on the task: back to the
    queue once, blocked the second time so a broken setup cannot burn budget in a loop."""
    marker = "Fletchery requeued"
    prior = sum(1 for c in getattr(ctx.gh, "comments", {}).get(task.number, []) if c.startswith(marker)) \
        if hasattr(ctx.gh, "comments") else 0
    if prior >= 1:
        return _block(ctx, out, why + "\n\n(second infrastructure failure; not retrying)")
    ctx.gh.set_status(task.number, QUEUED)
    ctx.gh.comment(task.number, f"{marker} this task: {why}")
    out.result, out.detail = "requeue", why
    return out


def pr_body(task: Task, report: dict, results: list[gates_mod.GateResult], review: dict, cfg: Config) -> str:
    steps = [f"- {s}" for s in report.get("build_steps", [])] or ["- Tools\\Build.ps1"]
    checks = [f"- {c}" for c in report.get("checks", [])] or ["- (none named)"]
    lines = [f"Closes #{task.number}", "", report.get("summary", ""), "",
             "## Build steps", *steps, "", "## Checks that prove it", *checks]
    if report.get("save_version"):
        lines += ["", f"**Save version:** {report['save_version']}"]
    if report.get("likely_conflicts"):
        lines += ["", "**Likely conflicts:** " + ", ".join(report["likely_conflicts"])]
    if task.lane_imports:
        lines += ["", f"Lane-Imports: {task.lane_imports}"]
    lines += ["", "## Checks (run by Fletchery, not the worker)", gates_mod.summary(results),
              "", f"## Review: {review.get('verdict', '?')}", _findings_text(review)]
    if report.get("unverified"):
        lines += ["", "## Not verified here", *[f"- {u}" for u in report["unverified"]]]
    lines += ["", "This PR was written by a Fletchery worker and has not been through the full build; the Build Lane "
                  "builds it. A `ready` label is not review.", "", "---",
              "_Generated by Fletchery with [Claude Code](https://claude.ai/code)_"]
    return "\n".join(lines)
