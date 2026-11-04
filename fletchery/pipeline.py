"""The task pipeline (worker -> gates -> reviewer -> PR) and the fixer. One task at a time per call; the loop runs
several calls concurrently. Every outcome is a plain record the loop writes to the ledger and the status issue."""
from __future__ import annotations

import asyncio

import json
import re
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
    def __init__(self, inner: Runtime, ctx: "Context | None" = None, task: int = 0, agent: str = ""):
        self.inner = inner
        self.ctx = ctx
        self.task = task
        self.agent = agent

    async def run(self, role, prompt, cwd, budget_usd, max_turns, on_action=None):
        live = self.ctx.live if self.ctx else None
        if live and self.task and role != "fixer":  # a fixer keeps "Fixing PR #n" from fix_pr
            stage = {"worker": "Writing", "reviewer": "Reviewing"}.get(role, role)
            multi = self.ctx.agents is not None and self.ctx.agents.multi
            live.stage(self.task, f"{stage} ({self.agent})" if self.agent and multi else stage)
        if live and self.task and self.agent:
            live.meta(self.task, agent=self.agent, role=role)
        cb = on_action or ((lambda text: live.action(self.task, text)) if live and self.task else None)
        r = await self.inner.run(role, prompt, cwd, budget_usd, max_turns, on_action=cb)
        r.agent = self.agent
        if r.signed_out:
            raise Limited(r.error, r.cost_usd, "signed_out", self.agent)
        if r.limited:
            raise Limited(r.error, r.cost_usd, "limited", self.agent)
        return r


class _Escalate:
    """An attempt failed in a way a stronger agent may fix: the worktree is reset and `agent` tries next."""
    def __init__(self, agent: str, note: str):
        self.agent, self.note = agent, note


@dataclass
class Outcome:
    task: int
    result: str              # "pr" | "blocked" | "requeue" | "fixed" | "gave_up" | "limited" | "agent_paused" | "waiting_agent"
    detail: str = ""
    pr_number: int | None = None
    cost_usd: float = 0.0
    sessions: list[str] = field(default_factory=list)
    agent: str = ""          # the agent whose work this outcome is (the last one tried)
    difficulty: str = ""
    skill: str = ""          # the kind of work (scores.SKILLS) the router saw
    route: str = ""          # why the router picked the first agent
    reviewer: str = ""
    attempts: list[dict] = field(default_factory=list)  # [{"agent", "difficulty", "role", "ok", "why"}]: what Stats learns from
    costs: dict = field(default_factory=dict)  # agent -> USD this outcome spent on it (writer and reviewer apart)


@dataclass
class Context:
    cfg: Config
    gh: object               # GitHub | FakeGitHub
    repo: object             # Repo | FakeRepo
    runtime: Runtime
    run_gates: Callable[[list[str], Path], list[gates_mod.GateResult]] = gates_mod.run
    log: Callable[[str], None] = print
    live: object | None = None  # live.Live: the dashboard's view of running work
    agents: object | None = None  # agents.AgentPool; None = every session on `runtime` (one agent, as before)
    stats: Callable[[], object] | None = None  # -> agents.Stats, for the router
    task_agents: dict = field(default_factory=dict)  # task number (str) -> agent that wrote its branch (for fixers)
    escalate: bool = True     # a failed attempt goes to a stronger agent before the task is blocked


def _stats(ctx: "Context"):
    try:
        return ctx.stats() if ctx.stats else None
    except Exception:
        return None


def _paused(ctx: "Context", out: Outcome, e: Limited, what: str) -> bool:
    """A limit or lost sign-in on one agent of several: pause that agent only, and let the task go to another. False
    when there is no other agent to go to (the old whole-Fletchery pause applies)."""
    pool = ctx.agents
    kind = e.args[2] if len(e.args) > 2 else "limited"
    agent = e.args[3] if len(e.args) > 3 else ""
    if not pool or not pool.multi or not agent:
        return False
    minutes = ctx.cfg.limit_backoff_minutes if kind == "limited" else max(120, ctx.cfg.limit_backoff_minutes)
    reason = ("usage limit: " if kind == "limited" else "needs signing in again: ") + str(e.args[0])[:160]
    who = pool.pause(agent, minutes, reason, shared=kind == "limited")
    if not pool.any_available(besides=agent):
        return False
    out.result, out.detail, out.agent = "agent_paused", f"{agent} paused ({reason})", agent
    also = f" (and {', '.join(who[1:])} on the same account)" if len(who) > 1 else ""
    ctx.log(f"{what}: {agent}{also} paused for {minutes} min ({reason}); other agents carry on")
    return True


async def run_task(task: Task, ctx: Context) -> Outcome:
    out = Outcome(task.number, "requeue")
    if ctx.live:
        ctx.live.stage(task.number, "Starting", task.title)
    try:
        result = await _run_task(task, ctx, out)
        if result.result == "blocked":
            import blockers
            return await blockers.resolve(task, ctx, result)
        return result
    finally:
        if ctx.live:
            ctx.live.done(task.number)


async def _run_task(task: Task, ctx: Context, out: Outcome) -> Outcome:
    agent, history = None, []
    try:
        while True:
            res = await _run_task_body(task, ctx, out, agent, history)
            if isinstance(res, _Escalate):
                agent = res.agent
                history.append(res.note)
                continue
            return res
    except Limited as e:
        # The worktree and any commits stay; the next run continues from them (the worker prompt says so).
        ctx.gh.set_status(task.number, QUEUED)
        out.cost_usd += e.args[1] if len(e.args) > 1 else 0.0
        if _paused(ctx, out, e, f"#{task.number}"):
            return out
        out.cost_usd -= e.args[1] if len(e.args) > 1 else 0.0
        signed_out = len(e.args) > 2 and e.args[2] == "signed_out"
        out.result, out.detail = ("signed_out" if signed_out else "limited"), str(e.args[0])[:200]
        out.cost_usd += e.args[1] if len(e.args) > 1 else 0.0
        ctx.log(f"#{task.number} paused: {'Claude sign-in expired' if signed_out else 'usage limit'}")
        return out


async def _run_task_body(task: Task, ctx: Context, out: Outcome, agent: str | None = None,
                         history: list[str] | None = None) -> "Outcome | _Escalate":
    cfg, gh, repo, pool = ctx.cfg, ctx.gh, ctx.repo, ctx.agents
    first = not history
    if pool is not None and agent is None:
        choice = pool.choose(task.title, task.body, task.labels, len(task.depends_on), _stats(ctx))
        if choice is None:  # every agent that could take it is paused or busy: back to the queue, untouched
            gh.set_status(task.number, QUEUED)
            out.result, out.detail = "waiting_agent", "no agent free for it"
            return out
        agent, out.difficulty, out.route, out.skill = choice.agent, choice.difficulty, choice.why, choice.skill
    if pool is not None:
        pool.acquire(agent)
    try:
        return await _attempt(task, ctx, out, agent, history or [], first)
    finally:
        if pool is not None:
            pool.release(agent)


async def _attempt(task: Task, ctx: Context, out: Outcome, agent: str | None, history: list[str],
                   first: bool) -> "Outcome | _Escalate":
    cfg, gh, repo, pool = ctx.cfg, ctx.gh, ctx.repo, ctx.agents
    rt = _LimitGuard(pool.runtime(agent) if pool is not None else ctx.runtime, ctx, task.number, agent or "")
    out.agent = agent or ""
    if agent:
        ctx.task_agents[str(task.number)] = agent
    if first:
        ctx.log(f"#{task.number} start: {task.title}" + (f" [{agent}: {out.route}]" if pool is not None and pool.multi else ""))
        gh.set_status(task.number, RUNNING)
    else:
        ctx.log(f"#{task.number} escalated to {agent}")
    _load_notes(ctx, task)
    try:
        await asyncio.to_thread(repo.fetch_base)
        wt = await asyncio.to_thread(repo.worktree, task.number, task.slug, task.branch, cfg.heavy_lease_file)
        await asyncio.to_thread(repo.ensure_task_checkout, task.number, task.slug, task.branch, wt, cfg.heavy_lease_file)
    except Exception as e:
        return _block(ctx, out, f"could not make the worktree: {e}")
    local_branch = _branch_at(repo, wt, task.branch)
    task.rules = rules_mod.load(wt, cfg.project_root)
    async def fail(why, infra=False):
        return await asyncio.to_thread(_fail, ctx, task, out, wt, agent, why, infra)
    issue = _issue_text(task) + _history_text(history)

    # 1. worker
    freeze = _freeze_text(cfg)
    prompt = role_prompt("worker", task.rules, project=cfg.name, issue=issue, branch=local_branch, base=cfg.base_branch, freeze=freeze)
    r = await rt.run("worker", prompt, wt, cfg.worker_budget_usd, cfg.worker_max_turns)
    _account(out, r)
    if not r.ok:
        return await fail(f"worker session failed: {r.error}", infra=True)
    if r.output.get("gave_up"):
        return await fail(f"worker gave up: {r.output['gave_up']}")
    await asyncio.to_thread(repo.ensure_task_checkout, task.number, task.slug, task.branch, wt, cfg.heavy_lease_file)
    if repo.dirty(wt):
        repo.commit_all(wt, f"Fletchery: #{task.number} {task.title}\n\nCloses #{task.number}")
    if not repo.has_commits(wt):
        return await fail("worker made no commits")
    worker_report = r.output

    # 2. gates (a failure goes back to the worker once)
    results = _gates(ctx, task, wt)
    if not all(g.passed for g in results):
        fix_prompt = role_prompt("worker", task.rules, project=cfg.name, issue=issue, branch=local_branch, base=cfg.base_branch,
                                 freeze=freeze) + "\n\n## Gate failures to fix\n" + _gate_output(results)
        r2 = await rt.run("worker", fix_prompt, wt, cfg.worker_budget_usd, cfg.worker_max_turns)
        _account(out, r2)
        await asyncio.to_thread(repo.ensure_task_checkout, task.number, task.slug, task.branch, wt, cfg.heavy_lease_file)
        if repo.dirty(wt):
            repo.commit_all(wt, f"Fletchery: #{task.number} gate fixes")
        results = _gates(ctx, task, wt)
        if not all(g.passed for g in results):
            return await fail("gates still fail after one fix pass:\n" + gates_mod.summary(results))

    # 3. reviewer (fresh context, read-only)
    review = await _review(ctx, task, wt, out, agent, issue)
    if review.get("verdict") == "needs_work":
        back = role_prompt("worker", task.rules, project=cfg.name, issue=issue, branch=local_branch, base=cfg.base_branch, freeze=freeze) \
            + "\n\n## Review findings to address\n" + _findings_text(review)
        r3 = await rt.run("worker", back, wt, cfg.worker_budget_usd, cfg.worker_max_turns)
        _account(out, r3)
        await asyncio.to_thread(repo.ensure_task_checkout, task.number, task.slug, task.branch, wt, cfg.heavy_lease_file)
        if repo.dirty(wt):
            repo.commit_all(wt, f"Fletchery: #{task.number} review fixes")
        results = _gates(ctx, task, wt)
        if not all(g.passed for g in results):
            return await fail("gates fail after the review fixes:\n" + gates_mod.summary(results))
        review = await _review(ctx, task, wt, out, agent, issue)
    if review.get("verdict") == "escalate":
        return _block(ctx, out, "reviewer escalated:\n" + _findings_text(review))
    if review.get("verdict") != "approve":
        return await fail("reviewer still asks for changes after one fix pass:\n" + _findings_text(review))

    # 4. PR
    if ctx.live:
        ctx.live.stage(task.number, "Opening PR")
    try:
        await asyncio.to_thread(repo.ensure_task_checkout, task.number, task.slug, task.branch, wt, cfg.heavy_lease_file, True)
        await asyncio.to_thread(repo.push, wt, task.branch)
        body = pr_body(task, worker_report, results, review, cfg, out if pool is not None and pool.multi else None)
        pr = gh.create_pr(task.branch, cfg.base_branch, f"#{task.number} {task.title}", body, [graph.PREFIX, "ready"],
                          cfg.assignee or None)
    except Exception as e:
        return _requeue_or_block(ctx, task, out, f"push or PR failed: {e}")
    gh.set_status(task.number, PR)
    out.attempts.append({"agent": agent or "", "difficulty": out.difficulty, "skill": out.skill, "role": "worker", "ok": True})
    by = f" Written by {agent}, reviewed by {out.reviewer}." if pool is not None and pool.multi else ""
    gh.comment(task.number, f"Fletchery opened PR #{pr} from `{task.branch}` (cost ${out.cost_usd:.2f}).{by} "
                            f"The Build Lane builds it; it still needs review before it merges.")
    out.result, out.pr_number, out.detail = "pr", pr, f"PR #{pr}"
    ctx.log(f"#{task.number} -> PR #{pr}")
    return out


async def fix_pr(task: Task, pr: PullRequest, feedback: list[Feedback], attempt: int, ctx: Context) -> Outcome:
    out = Outcome(task.number, "gave_up", pr_number=pr.number)
    if ctx.live:
        ctx.live.stage(task.number, f"Fixing PR #{pr.number}", task.title, kind="fix")
    try:
        result = await _fix_pr(task, pr, feedback, attempt, ctx, out)
        if result.result == "gave_up":
            import blockers
            return await blockers.resolve(task, ctx, result)
        return result
    except Limited as e:
        if _paused(ctx, out, e, f"PR #{pr.number} fix"):
            return out  # the loop un-counts the attempt like a limit, so another agent retries it
        signed_out = len(e.args) > 2 and e.args[2] == "signed_out"
        out.result, out.detail = ("signed_out" if signed_out else "limited"), str(e.args[0])[:200]
        ctx.log(f"PR #{pr.number} fix paused: {'Claude sign-in expired' if signed_out else 'usage limit'}")
        return out  # the loop un-counts the attempt and forgets the feedback was seen, so it is retried
    finally:
        if ctx.live:
            ctx.live.done(task.number)


async def _fix_pr(task: Task, pr: PullRequest, feedback: list[Feedback], attempt: int, ctx: Context,
                  out: Outcome) -> Outcome:
    cfg, gh, repo, pool = ctx.cfg, ctx.gh, ctx.repo, ctx.agents
    agent = fixer_agent(ctx, task, attempt)
    if pool is not None and agent is None:
        out.result, out.detail = "agent_paused", "no agent free to fix it"
        return out
    _load_notes(ctx, task)
    if attempt > cfg.fixer_attempts:
        gh.set_status(task.number, BLOCKED)
        gh.comment(task.number, f"Fletchery: PR #{pr.number} came back {attempt - 1} times; stopping. Last feedback:\n\n"
                                + "\n".join(f"> {f.text[:300]}" for f in feedback))
        out.detail = "fixer attempts exhausted"
        return out
    await asyncio.to_thread(repo.fetch_base)
    wt = await asyncio.to_thread(repo.repair_worktree, task.number, task.slug, pr.head, pr.head_sha)
    await asyncio.to_thread(repo.ensure_repair_checkout, wt, pr.head, pr.head_sha, cfg.heavy_lease_file)
    task.rules = rules_mod.load(wt, cfg.project_root)
    fb = "\n\n".join(f"### {f.kind} ({f.at})\n{f.text}" for f in feedback)
    rt = _LimitGuard(pool.runtime(agent) if pool is not None else ctx.runtime, ctx, task.number, agent or "")
    out.agent = agent or ""
    if pool is not None:
        pool.acquire(agent)
        ctx.task_agents[str(task.number)] = agent
        if pool.multi:
            ctx.log(f"PR #{pr.number} fix attempt {attempt} by {agent}")
    try:
        return await _fix_session(task, pr, attempt, ctx, out, rt, wt, fb, agent)
    finally:
        if pool is not None:
            pool.release(agent)


def fixer_agent(ctx: Context, task: Task, attempt: int) -> str | None:
    """The agent that wrote the branch fixes it; a second attempt goes one tier up. A paused or switched-off agent is
    replaced by the router's choice for the task."""
    pool = ctx.agents
    if pool is None:
        return None
    from agents import classify
    import scores
    level, _ = classify(task.title, task.body, task.labels, pool.routing.get("hardPatterns"),
                        pool.routing.get("easyPatterns"), len(task.depends_on), pool.routing.get("hardScore"))
    skill, _ = scores.classify_skill(task.title, task.body, task.labels, pool.routing.get("defaultSkill", "cpp"))
    agent = ctx.task_agents.get(str(task.number))
    if agent and attempt >= 2:
        agent = pool.escalate(agent, "fixer", skill, level) or agent
    if (agent and pool.available(agent, "fixer")
            and (pool.specs[agent].levels is None or level in pool.specs[agent].levels)):
        return agent
    c = pool.choose(task.title, task.body, task.labels, len(task.depends_on), _stats(ctx), role="fixer")
    return c.agent if c else None


async def _fix_session(task, pr, attempt, ctx, out, rt, wt, fb, agent) -> Outcome:
    cfg, gh, repo = ctx.cfg, ctx.gh, ctx.repo
    local_branch = _branch_at(repo, wt, task.branch)
    fb += f"\n\nThe local repair branch is `{local_branch}`. The controller alone pushes the checked fix to existing PR branch `{pr.head}`; do not push or recreate its branch."
    prompt = role_prompt("fixer", task.rules, project=cfg.name, issue=_issue_text(task), branch=local_branch, base=cfg.base_branch, pr=str(pr.number),
                         feedback=fb, freeze=_freeze_text(cfg))
    r = await rt.run("fixer", prompt, wt, cfg.fixer_budget_usd, cfg.fixer_max_turns)
    _account(out, r)
    if not r.ok or r.output.get("gave_up"):
        out.detail = r.error or r.output.get("gave_up", "")
        out.attempts.append({"agent": agent or "", "difficulty": "", "role": "fixer", "ok": False, "why": out.detail[:200]})
        gh.comment(task.number, f"Fletchery fixer (attempt {attempt}) could not fix PR #{pr.number}: {out.detail}")
        return out
    await asyncio.to_thread(repo.ensure_repair_checkout, wt, pr.head, pr.head_sha, cfg.heavy_lease_file)
    if repo.dirty(wt):
        repo.commit_all(wt, f"Fletchery: #{task.number} fix after lane feedback")
    results = _gates(ctx, task, wt)
    if not all(g.passed for g in results):
        out.detail = "gates fail after the fix:\n" + gates_mod.summary(results)
        gh.comment(task.number, f"Fletchery fixer (attempt {attempt}): {out.detail}")
        return out
    await asyncio.to_thread(repo.push_repair, wt, pr.head, pr.head_sha, cfg.heavy_lease_file)
    gh.comment(pr.number, f"Fletchery fixer (attempt {attempt}): {r.output.get('summary', '')}\n\n{gates_mod.summary(results)}\n\n"
                          "Pushed; the lane will take it again when it is labelled `ready`.")
    out.result, out.detail = "fixed", r.output.get("summary", "")
    out.attempts.append({"agent": agent or "", "difficulty": "", "role": "fixer", "ok": True})
    return out


# ---- helpers ----

def _fail(ctx: Context, task: Task, out: Outcome, wt: Path, agent: str | None, why: str,
          infra: bool = False) -> "Outcome | _Escalate":
    """A failed attempt. With several agents, the next stronger one available starts again from a clean branch, told
    what went wrong; the task is blocked only when the strongest has failed too. With one agent: blocked as before
    (an infrastructure failure is requeued once first)."""
    pool = ctx.agents
    first_line = why.splitlines()[0] if why else ""
    if agent:
        out.attempts.append({"agent": agent, "difficulty": out.difficulty, "skill": out.skill, "role": "worker", "ok": False,
                             "why": first_line[:200]})
    nxt = pool.escalate(agent, "worker", out.skill, out.difficulty) if pool is not None and pool.multi and ctx.escalate and agent else None
    if not nxt:
        tried = [a["agent"] for a in out.attempts if a.get("role") == "worker"]
        if len(tried) > 1:
            why += "\n\nAgents tried, in order: " + ", ".join(tried) + "."
        return _requeue_or_block(ctx, task, out, why) if infra and len(tried) <= 1 else _block(ctx, out, why)
    try:
        fresh = ctx.repo.restart_task_worktree(task.number, task.slug, task.branch, wt, why, ctx.cfg.heavy_lease_file)
    except Exception as e:
        return _block(ctx, out, f"{why}\n\n(could not preserve this attempt and prepare {nxt}'s checkout: {e})")
    ctx.gh.comment(task.number, f"Fletchery: {agent} could not finish this ({first_line[:300]}). "
                                f"Its work is preserved at `{wt}`; {nxt} starts in `{fresh}` from `{ctx.cfg.base_branch}`.")
    ctx.log(f"#{task.number} {agent} failed ({first_line[:80]}); escalating to {nxt}")
    return _Escalate(nxt, f"- {agent} tried and failed: {why[:1500]}\n  Preserved attempt: `{wt}`. Read it only if useful; do not modify it.")


def _branch_at(repo, wt: Path, fallback: str) -> str:
    return repo.git("branch", "--show-current", cwd=wt) if hasattr(repo, "git") else fallback


def _history_text(history: list[str]) -> str:
    if not history:
        return ""
    return ("\n\n## Earlier attempts (preserved separately; this attempt has a fresh checkout)\n\n" + "\n".join(history)
            + "\n\nAvoid what made them fail.")


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
    who = getattr(r, "agent", "") or "claude"
    out.costs[who] = round(out.costs.get(who, 0.0) + r.cost_usd, 4)
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


async def _review(ctx: Context, task: Task, wt: Path, out: Outcome, worker: str | None = None,
                  issue: str | None = None) -> dict:
    """A fresh, read-only session. With several agents the reviewer comes from another vendor than the worker
    (agents.AgentPool.reviewer_for), so no model marks its own homework."""
    cfg, pool = ctx.cfg, ctx.agents
    prompt = role_prompt("reviewer", task.rules, project=cfg.name, issue=issue or _issue_text(task), branch=_branch_at(ctx.repo, wt, task.branch), base=cfg.base_branch)
    failed: dict[str, str] = {}  # reviewers whose session failed to run on this task -> why
    while True:
        name = ""
        if pool is not None:
            name = pool.reviewer_for(worker or "", besides=set(failed)) or ""
            if not name:
                if failed:  # every reviewer there is failed to run: that's Fletchery's setup, not the work
                    why = "; ".join(f"{n}: {e[:300]}" for n, e in failed.items())
                    return {"verdict": "escalate", "findings": [{"file": "-", "line": 0, "rule": "review",
                                                                 "note": f"no reviewer could run ({why})"}]}
                raise Limited("no agent is free to review", 0.0, "limited", "")
            out.reviewer = name
        rt = pool.runtime(name) if pool is not None else ctx.runtime
        r = await _LimitGuard(rt, ctx, task.number, name).run("reviewer", prompt, wt, cfg.reviewer_budget_usd, cfg.reviewer_max_turns)
        _account(out, r)
        if r.ok:
            return r.output
        err = (r.error or "the session failed").strip()
        if pool is None or not pool.multi or not name:
            return {"verdict": "escalate", "findings": [{"file": "-", "line": 0, "rule": "review", "note": err}]}
        # The review never happened (a model the CLI refuses, a crash): that says nothing about the work, so another
        # reviewer takes it instead of the task being blocked. LifeShare 2026-11-03: a Codex agent saved with an
        # OpenCode-style model name failed every review and blocked four finished tasks in half an hour.
        failed[name] = err
        if _CANT_RUN.search(err):  # it will fail the same way next time: keep it off reviews until someone looks
            pool.pause(name, max(120, cfg.limit_backoff_minutes), "can't run: " + err[:160], shared=False)
            ctx.log(f"#{task.number}: reviewer {name} can't run ({err[:120]}); paused, another reviewer takes it")
        else:
            ctx.log(f"#{task.number}: reviewer {name} failed to run ({err[:120]}); another reviewer takes it")


# Errors that mean the agent can't run at all (wrong model name, refused by the account), not a one-off failure.
_CANT_RUN = re.compile(r"not supported|unsupported model|unknown model|model[^.\n]{0,60}(not found|does not exist|"
                       r"not available|invalid)|invalid model|no such model|not have access to (the )?model", re.IGNORECASE)


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


def pr_body(task: Task, report: dict, results: list[gates_mod.GateResult], review: dict, cfg: Config,
            out: Outcome | None = None) -> str:
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
    if out is not None and out.agent:
        before = [a["agent"] for a in out.attempts if a.get("role") == "worker" and not a.get("ok")]
        lines += ["", f"**Written by:** {out.agent}" + (f" (after {', '.join(before)})" if before else "")
                  + f" · **Reviewed by:** {out.reviewer or '?'} · **Difficulty:** {out.difficulty or '?'}"]
    lines += ["", "## Checks (run by Fletchery, not the worker)", gates_mod.summary(results),
              "", f"## Review: {review.get('verdict', '?')}", _findings_text(review)]
    if report.get("unverified"):
        lines += ["", "## Not verified here", *[f"- {u}" for u in report["unverified"]]]
    lines += ["", "This PR was written by a Fletchery worker and has not been through the full build; the Build Lane "
                  "builds it. A `ready` label is not review.", "", "---",
              "_Generated by Fletchery with [Claude Code](https://claude.ai/code)_" if out is None or not out.agent
              or out.agent.startswith("claude") else f"_Generated by Fletchery ({out.agent})_"]
    return "\n".join(lines)
