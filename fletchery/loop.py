"""One pass of Fletchery: sync, feedback, dispatch, status. `Fletchery.once()` is what `foreman.py once` and `run` call."""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import Config
import graph
from graph import BLOCKED, PR, QUEUED, RUNNING, Task, readiness, pick
from github import PullRequest, Feedback
from pipeline import Context, Outcome, fix_pr, run_task


@dataclass
class State:
    last_pass: str = ""
    feedback_cursor: int = 0                               # fair, bounded PR feedback polling before worker dispatch
    seen: dict[str, str] = field(default_factory=dict)        # pr number -> ISO time of the newest feedback acted on
    fix_attempts: dict[str, int] = field(default_factory=dict)  # pr number -> attempts so far
    spent: dict[str, float] = field(default_factory=dict)     # YYYY-MM-DD -> USD
    running: dict[str, str] = field(default_factory=dict)     # task number -> started ISO (for the status issue)
    paused_until: str = ""                                    # ISO: a usage limit was hit; dispatch nothing before this
    paused_reason: str = ""
    held: bool = False                                        # Alex pressed Pause: start nothing new until Resume
    signed_out: str = ""                                      # why sessions cannot sign in; cleared once they can
    watch_seen: list = field(default_factory=list)            # build runs the lane watcher has handled
    watch_last: str = ""                                      # ISO: when the lane watcher last looked
    agent_paused: dict = field(default_factory=dict)          # agent -> {"until", "reason"}: one agent's limit or sign-in
    task_agent: dict = field(default_factory=dict)            # task number -> the agent that wrote its branch
    accounts_checked: str = ""                                # ISO: when every agent's sign-in was last checked
    spent_by_agent: dict = field(default_factory=dict)        # YYYY-MM-DD -> {agent: USD}
    merge_round_no: int = 0                                   # the last merge round Fletchery started (rounds.py)
    merge_last: dict = field(default_factory=dict)            # {"round", "result", "at", "key", "summary", "pr"}
    merge_excluded: dict = field(default_factory=dict)         # PR -> {head, why, round, at}: unchanged failed heads wait for repair
    merge_held: bool = False                                  # Pause merging in the Merge section
    blocker_checked: dict = field(default_factory=dict)       # task -> last planner dispatch; fair across deferred blocks

    @staticmethod
    def load(path: Path) -> "State":
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            known = set(State.__dataclass_fields__)
            return State(**{k: v for k, v in data.items() if k in known})
        return State()

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.__dict__, indent=2), encoding="utf-8")


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class _StatusTap:
    """Passes everything through to the GitHub client; a status label change also updates the dashboard's board."""
    def __init__(self, inner, fm: "Engine"):
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_fm", fm)

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def __setattr__(self, name, value):
        setattr(self._inner, name, value)

    def __delattr__(self, name):
        # Match assignment forwarding (including temporary mock/spies used by checks).
        delattr(self._inner, name)

    def set_status(self, number: int, status: str) -> None:
        self._inner.set_status(number, status)
        task = getattr(self._fm, "_tasks", {}).get(number)
        if task is not None:
            task.labels = [l for l in task.labels if not graph.is_status_label(l)] + [graph.label_for(status)]
        for row in (self._fm.snapshot or {}).get("tasks", []):
            if row["number"] == number:
                row["status"] = status.split(":")[-1]
                row["why"] = "ready" if status == QUEUED else ""


class Engine:
    def __init__(self, ctx: Context, state_dir: Path, dry_run: bool = False):
        if not isinstance(ctx.gh, _StatusTap):
            ctx.gh = _StatusTap(ctx.gh, self)
        self.ctx = ctx
        self.cfg = ctx.cfg
        self.state_dir = state_dir
        self.state = State.load(state_dir / "state.json")
        self.dry_run = dry_run
        self.last_plan: list[str] = []  # what a dry run would have done
        self.snapshot: dict = {}        # the last pass, structured, for the dashboard
        self.next_pass_at: str = ""
        self.passing = False
        self._wake: asyncio.Event | None = None
        self.active: set[int] = set()   # tasks a session of THIS process is working on right now
        self.jobs: dict = {}            # asyncio job -> (task number, PR number or None, the PR's last seen feedback)
        self.recent: list[Outcome] = []
        self._tasks: dict = {}
        self._prs: dict = {}
        self._waiting: dict = {}
        self._aloop: asyncio.AbstractEventLoop | None = None
        self.watcher = None
        self.round_job = None           # the merge round running now (rounds.py), an asyncio task
        self.round_info: dict = {}      # {"round", "agent", "prs", "started"} of that round
        self.merge_why = ""             # why no round is starting, for the Merge section
        self._stats_cache: tuple[float, object] | None = None
        if ctx.agents is not None:  # one agent's pause and who wrote which branch survive a restart
            ctx.agents.paused = self.state.agent_paused
            ctx.task_agents = self.state.task_agent
            ctx.stats = self.stats
            ctx.agents.catalog_override = self.state_dir / "model_scores.json"
        if not dry_run and ctx.cfg.workers_enabled:
            try:
                import watcher
                self.watcher = watcher.make(ctx.cfg, ctx.gh, ctx.log)
            except Exception as e:  # never let the watcher keep the workers from starting
                ctx.log(f"lane watcher off: {type(e).__name__}: {e}")

    # ---- agents ----
    def stats(self):
        """Per-agent success rates from the ledger, re-read at most once a minute."""
        import time
        from agents import Stats
        if self._stats_cache and time.time() - self._stats_cache[0] < 60:
            return self._stats_cache[1]
        st = Stats.from_ledger(self.state_dir / "ledger.jsonl")
        self._stats_cache = (time.time(), st)
        return st

    def check_accounts(self, force: bool = False) -> None:
        """Is each switched-on agent signed in? Every 30 minutes (or on request), on a thread: a CLI that hangs must
        not stop the loop. An agent whose check fails is skipped by the router until a later check passes."""
        pool = self.ctx.agents
        if pool is None or self.dry_run or getattr(self, "_checking", False):
            return
        st = self.state
        if not force and st.accounts_checked and (datetime.now(timezone.utc) - datetime.fromisoformat(st.accounts_checked)
                                                  < timedelta(minutes=30)):
            return
        st.accounts_checked = now()
        import threading

        def run():
            self._checking = True
            try:
                import clis
                for spec in list(pool.specs.values()):
                    if not spec.enabled:
                        continue
                    try:
                        ok, msg = clis.check(spec, self.cfg.auth)
                    except Exception as e:
                        ok, msg = False, f"{type(e).__name__}: {e}"
                    was = pool.accounts.get(spec.name, {}).get("ok")
                    pool.accounts[spec.name] = {"ok": ok, "message": msg, "at": now()}
                    if was is not ok:
                        self.ctx.log(f"agent {spec.name}: {'ready' if ok else 'not available'} ({msg})")
            finally:
                self._checking = False
        threading.Thread(target=run, name="fletchery-accounts", daemon=True).start()

    # ---- the pass ----
    async def once(self) -> list[Outcome]:
        """One batch: dispatch what fits, wait for all of it, write the status. `foreman once` and the tests use this;
        the running app uses `run_forever`, which refills a slot the moment a session ends."""
        self.dispatch()
        outcomes: list[Outcome] = []
        while self.jobs:
            outcomes += await self._wait_for_jobs(None)
        if self.round_job is not None:
            await self.round_job
            self.round_job, self.round_info = None, {}
        self.publish(outcomes)
        return outcomes

    # ---- dispatch: sync with GitHub and start sessions in the free slots (never waits for them) ----
    def dispatch(self, draining: bool = False) -> None:
        gh, cfg, st = self.ctx.gh, self.cfg, self.state
        self.last_plan = []
        self.passing = True
        try:
            self._watch()
            tasks = gh.open_tasks()
            prs = {p.number: p for p in gh.open_prs()}
        finally:
            self.passing = False
        # A task labelled running that this process is not running was orphaned by a stop, crash or reboot (one loop
        # per PC, so no other Fletchery has it). Back to the queue; the next worker continues from its commits.
        if not self.dry_run:
            for t in tasks.values():
                if t.status == RUNNING and t.number not in self.active:
                    gh.set_status(t.number, QUEUED)
                    t.labels = [l for l in t.labels if not graph.is_status_label(l)] + [graph.label_for(QUEUED)]
                    st.running.pop(str(t.number), None)
                    self.ctx.log(f"#{t.number} was left running by an earlier Fletchery; back in the queue")
        by_pr = _tasks_by_pr(tasks, prs)
        # A task whose PR merged into a non-default branch (Test-Staging) stays open: GitHub only acts on "Closes #n"
        # for the default branch. Close it here, or everything depending on it waits forever (round 7, 2026-11-03).
        for t in [t for t in tasks.values() if t.status == PR and not t.pr_number]:
            try:
                m = gh.merged_pr(t.branch)
            except Exception as e:
                self.ctx.log(f"#{t.number}: could not look up its merged PR: {type(e).__name__}: {e}")
                continue
            if not m:
                continue
            self.last_plan.append(f"close #{t.number}: PR #{m[0]} merged into {m[1]}")
            if self.dry_run:
                continue
            gh.comment(t.number, f"Fletchery: PR #{m[0]} merged into {m[1]} at {m[2]}. GitHub closes an issue from a PR's "
                                 f"`Closes #{t.number}` only for merges into the default branch, so Fletchery closes it, "
                                 "and the tasks that depend on it can start.")
            gh.close_issue(t.number)
            tasks.pop(t.number, None)
            self.ctx.log(f"#{t.number} closed: PR #{m[0]} merged into {m[1]}")
        limit_paused = bool(st.paused_until) and now() < st.paused_until
        if not limit_paused and st.paused_until:
            st.paused_until, st.paused_reason = "", ""
        if st.signed_out and not self.dry_run:
            import auth
            ok, msg = auth.check(self.cfg.auth, getattr(self.ctx.runtime, "saved_key", None))
            if ok:
                self.ctx.log("signed in again; work resumes")
                st.signed_out = ""
            else:
                st.signed_out = msg
        pool = self.ctx.agents
        if pool is not None:
            pool.clear_expired()
            self.check_accounts()
            if pool.multi and st.signed_out and pool.any_available():
                st.signed_out = ""  # Claude's sign-in is one agent's problem now: the others carry on (agents panel)
        paused = limit_paused or st.held or bool(st.signed_out) or draining

        # 1. feedback on open PRs -> fixers (one per task at a time)
        ordered_feedback = sorted(by_pr.items())
        ordered_feedback = [x for x in ordered_feedback if x[0] > st.feedback_cursor] + [x for x in ordered_feedback if x[0] <= st.feedback_cursor]
        inspected_feedback = 0
        for pr_num, task in ordered_feedback:
            if paused or self.free_slots() <= 0 or inspected_feedback >= 6:
                break
            if task.number in self.active:
                continue
            if any(d in tasks or not gh.issue_closed(d) for d in task.depends_on):
                continue
            pr = prs[pr_num]
            inspected_feedback += 1
            st.feedback_cursor = pr_num
            try:
                fb = gh.feedback_since(pr, st.seen.get(str(pr_num)))
            except Exception as e:
                self.ctx.log(f"PR #{pr_num}: feedback poll deferred: {e}")
                fb = []
            excluded = st.merge_excluded.get(str(pr_num), {})
            if excluded.get("head") == pr.head_sha and st.fix_attempts.get(str(pr_num), 0) < cfg.fixer_attempts:
                if not st.seen.get(str(pr_num)) or excluded.get("at", "") > st.seen[str(pr_num)]:
                    fb.append(Feedback(pr_num, "merge", "Merge round left this PR out after its repair budget: " + excluded.get("why", ""), excluded["at"]))
            if not fb or paused or self.free_slots() <= 0:
                continue
            if self.watcher:  # a failure on the build PC: give the fixer the failing step's log
                for f in fb:
                    if f.kind == "status":
                        f.text = self.watcher.enrich(f.text, f.text.split("`")[1] if f.text.count("`") >= 2 else "")
            attempt = st.fix_attempts.get(str(pr_num), 0) + 1
            self.last_plan.append(f"fix PR #{pr_num} (task #{task.number}, attempt {attempt}): {fb[0].text[:80]}")
            if self.dry_run:
                continue
            prior = st.seen.get(str(pr_num))
            st.seen[str(pr_num)] = max(f.at for f in fb)
            st.fix_attempts[str(pr_num)] = attempt
            self._start(fix_pr(task, pr, fb, attempt, self.ctx), task.number, pr_num, prior)

        # 2. ready tasks -> workers, within slots and budget
        # Existing Needs you tasks get the same bounded prerequisite planning as new blocks.
        # One planner per pass, counted against worker slots and the daily cap.
        blocker_busy = any(t.status == BLOCKED and t.number in self.active for t in tasks.values())
        if cfg.auto_prerequisites and not blocker_busy and not paused and st.spent.get(today(), 0.0) < cfg.daily_budget_usd and self.free_slots() > 0:
            import blockers
            import rules as rules_mod
            for t in sorted(tasks.values(), key=lambda t: (st.blocker_checked.get(str(t.number), ""), t.created_at, t.number)):
                if t.status != BLOCKED or t.number in self.active:
                    continue
                try:
                    if not blockers.pending(gh.issue_thread(t.number)):
                        continue
                except Exception as e:
                    self.ctx.log(f"#{t.number}: could not inspect blocker: {e}")
                    continue
                self.last_plan.append(f"plan prerequisites for #{t.number}")
                if not self.dry_run:
                    st.blocker_checked[str(t.number)] = now()
                    st.save(self.state_dir / "state.json")
                    t.rules = rules_mod.load(Path(cfg.project_root), cfg.project_root)
                    self._start(blockers.resolve(t, self.ctx, Outcome(t.number, "blocked")), t.number, None, None)
                break
        closed_deps = {d for t in tasks.values() for d in t.depends_on if d not in tasks and gh.issue_closed(d)}
        rd = readiness(tasks, closed_deps)
        fixing = {t.number for t in by_pr.values()}
        rd.ready = [t for t in rd.ready if t.number not in self.active and t.number not in fixing]
        if pool is not None:  # a task whose agents are all off, paused or busy waits instead of starting and stopping
            stats = self.stats()
            free = []
            for t in rd.ready:
                if pool.choose(t.title, t.body, t.labels, len(t.depends_on), stats) is not None:
                    free.append(t)
                else:
                    from agents import override
                    want = override(t.body, t.labels)
                    rd.waiting[t.number] = (f"waits for agent {want} (off, paused or busy)" if want
                                            else "waits for a free agent: every suitable one is off, paused or busy")
            rd.ready = free
        spent = st.spent.get(today(), 0.0)
        budget_left = spent < cfg.daily_budget_usd
        chosen = pick(rd.ready, self.free_slots()) if budget_left and not paused else []
        for t in rd.ready:
            if t in chosen:
                self.last_plan.append(f"run #{t.number} {t.title} on {t.branch}")
            elif draining:
                rd.waiting[t.number] = "on hold: Fletchery is restarting on new code"
            elif st.signed_out:
                rd.waiting[t.number] = "on hold: Claude needs signing in again"
            elif st.held:
                rd.waiting[t.number] = "on hold: Fletchery is paused"
            elif paused:
                rd.waiting[t.number] = f"paused for the usage limit until {st.paused_until}"
            else:
                rd.waiting[t.number] = ("daily budget spent" if not budget_left else
                                        "writing paused: workers set to 0" if cfg.max_parallel <= 0 else
                                        f"no free slot ({cfg.max_parallel})")
        if not self.dry_run:
            for t in chosen:
                st.running[str(t.number)] = now()
                t.labels = [l for l in t.labels if not graph.is_status_label(l)] + [graph.label_for(RUNNING)]
                self._start(run_task(t, self.ctx), t.number, None, None)
            st.save(self.state_dir / "state.json")
        self._merge_round(draining)
        self._tasks, self._prs, self._waiting = tasks, prs, rd.waiting
        self.snapshot = self.make_snapshot(tasks, prs, rd.waiting, [])

    # ---- merge rounds: keep ready PRs merging without anyone (rounds.py) ----
    def _merge_round(self, draining: bool) -> None:
        import rounds
        cfg, st = self.cfg, self.state
        if not cfg.merge_rounds:
            self.merge_why = ""
            return
        if not self.dry_run and st.merge_last.get("result") in ("crashed", "checkout_failed") and not st.merge_last.get("failure_issue"):
            try:
                st.merge_last["failure_issue"] = rounds.report_failure(
                    self.ctx.gh, cfg, st.merge_last, st.merge_last.get("worktree") or str(rounds.checkout_path(self.ctx.repo)))
                st.save(self.state_dir / "state.json")
            except Exception as e:
                self.ctx.log(f"merge rounds: could not file prior crash: {type(e).__name__}: {e}")
        if self.round_job is not None:
            self.merge_why = "a round is running"
            return
        try:
            ready = rounds.ready_prs(self.ctx.gh.labelled_prs(cfg.merge_label or "ready", cfg.base_branch), cfg)
            for p in ready:
                old = st.merge_excluded.get(str(p.number))
                if old and old.get("head") == p.head_sha and not old.get("repair_task"):
                    linked = next((t for t in self._tasks.values() if t.pr_number == p.number or t.branch == p.head or re.search(r"\bcloses\s+#" + str(t.number) + r"\b", p.body or "", re.I)), None)
                    try:
                        if linked:
                            old["repair_task"] = linked.number
                        else:
                            marker = f"<!-- fletchery-repair-pr:{p.number} -->"
                            existing = self.ctx.gh.marked_issues(marker)
                            old["repair_task"] = existing[0]["number"] if existing else self.ctx.gh.create_issue(
                                f"Repair PR #{p.number}: {p.title}"[:200],
                                f"Repair the existing PR on its own branch; it was left out of a merge round. "
                                f"Keep its acceptance checks and send the fix to the same PR.\n\nExisting PR: #{p.number}\n"
                                f"Repair branch: {p.head}\n\nFailed head: `{p.head_sha}`\n\n{old.get('why', '')}\n\n{marker}",
                                [graph.PREFIX, graph.label_for(PR)])
                        st.save(self.state_dir / "state.json")
                    except Exception as e:
                        self.ctx.log(f"PR #{p.number}: repair task creation deferred: {e}")
                if old and old.get("head") != p.head_sha:
                    st.merge_excluded.pop(str(p.number), None)
            ready = rounds.without_failed_dependencies(ready, st.merge_excluded, self._tasks)
        except Exception as e:  # GitHub down: try again next pass
            self.merge_why = f"couldn't list the PRs: {type(e).__name__}"
            self.ctx.log(f"merge rounds: {type(e).__name__}: {e}")
            return
        ok, why = rounds.should_start(cfg, st.merge_last, st.merge_held, draining, False, ready)
        self.merge_why = why
        if not ok:
            return
        pool = self.ctx.agents
        agent = pool.merger(cfg.merge_agent) if pool is not None else ""
        if pool is not None and not agent:
            self.merge_why = "no agent can run a round now (enable a signed-in agent with the merger role that is not at a limit)"
            return
        wt_root = getattr(self.ctx.repo, "worktree_root", None)
        if wt_root and not self.dry_run:
            try:
                rounds.checkout_guard(rounds.checkout_path(self.ctx.repo), cfg.heavy_lease_file)
            except RuntimeError as e:
                self.merge_why = str(e)
                return
        limit = max(1, int(cfg.merge_round_max or 12))
        batch = []
        for offset in range(0, len(ready), limit):
            batch = rounds.without_failed_dependencies(ready[offset:offset + limit], st.merge_excluded, self._tasks)
            if batch:
                break
        if not batch:
            self.merge_why = "ready PRs wait for prerequisite PRs to become eligible"
            return
        round_file = cfg.merge_round_file or str(self.state_dir / "merge-round.json")
        try:
            last_no = int(str(rounds.read_file(round_file).get("round", "0")).strip() or 0)
        except ValueError:
            last_no = 0
        n = max(st.merge_round_no, last_no) + 1
        self.last_plan.append(f"merge round {n}: " + ", ".join(f"#{p.number}" for p in batch) + (f" on {agent}" if agent else ""))
        if self.dry_run:
            return
        st.merge_round_no = n
        st.save(self.state_dir / "state.json")
        self.round_info = {"round": n, "agent": agent or "claude", "prs": [p.number for p in batch], "started": now()}
        self.merge_why = "a round is running"
        self.round_job = asyncio.ensure_future(self._run_round(n, batch, agent, rounds.key_of(ready), round_file))
        self.ctx.log(f"merge round {n} started on {agent or 'claude'}: " + ", ".join(f"#{p.number}" for p in batch))

    async def _run_round(self, n: int, batch: list, agent: str, key: str, round_file: str) -> dict:
        import clis
        import rounds
        import rules as rules_mod
        from pipeline import _LimitGuard, Limited
        from runtime import role_prompt
        cfg, gh, pool = self.cfg, self.ctx.gh, self.ctx.agents
        branch = f"fletchery/merge-round-{n}-{datetime.now(timezone.utc).strftime('%m%d%H%M')}"
        res = {"round": n, "result": "failed", "at": now(), "key": key, "summary": "", "pr": 0, "agent": agent}
        if pool is not None and agent:
            pool.acquire(agent)
        try:
            rounds.start_file(round_file, n, branch, str(rounds.checkout_path(self.ctx.repo)), agent or "claude", batch, cfg.base_branch)
            wt = await asyncio.to_thread(rounds.prepare_worktree, self.ctx.repo, branch, cfg.heavy_lease_file)
            rounds.start_file(round_file, n, branch, str(wt), agent or "claude", batch, cfg.base_branch)
            rules = rules_mod.load(wt, cfg.project_root)
            flags_cfg = [f for f in (cfg.merge_flags or []) if f.get("paths") and f.get("flag")]

            def flags_for(p):
                if not flags_cfg:
                    return []
                try:
                    files = gh.pr_files(p.number)
                    self.__dict__.setdefault("_merge_files", {})[(p.number, p.head_sha)] = files
                except Exception:
                    return []
                return [f["flag"] for f in flags_cfg if any(re.search(f["paths"], x) for x in files)]
            changed = sorted({f for p in batch for f in gh.pr_files(p.number)})
            validation = rounds.validation_plan(changed, cfg.base_branch)
            prompt = role_prompt("merger", rules, project=cfg.name, base=cfg.base_branch, branch=branch, round=str(n),
                                 label=cfg.merge_label or "ready", prs=rounds.pr_list_text(batch, flags_for),
                                 round_file=round_file, validation=validation, additions=rounds.notes_text(rules, cfg) or "(none: use the "
                                 "repository's own docs to find how it builds and tests)",
                                 freeze="# Code freeze\n\nThe project is in a code freeze: merge fixes only." if cfg.code_freeze else "")
            rt = pool.runtime(agent) if pool is not None and agent else self.ctx.runtime
            guard = _LimitGuard(rt, self.ctx, 0, agent or "")
            r = await guard.run("merger", prompt, wt, cfg.merge_budget_usd, cfg.merge_max_turns)
            self._spend(agent or "claude", r.cost_usd)
            for _ in range(2):  # stopped part way with a progress note: carry on in the same session
                if r.ok or not r.session_id or "no JSON answer" not in (r.error or "") or not hasattr(rt, "command"):
                    break
                self.ctx.log(f"merge round {n}: {agent} stopped before the end; asking it to carry on")
                rt.resume_session = r.session_id
                try:
                    r = await guard.run("merger", prompt, wt, cfg.merge_budget_usd, cfg.merge_max_turns)
                finally:
                    rt.resume_session = ""
                self._spend(agent or "claude", r.cost_usd)
            out = r.output or {}
            res["result"] = rounds.result_of(out) if r.ok else "failed"
            res["pr"] = int(out.get("landed_pr") or 0)
            res["summary"] = (out.get("summary") or out.get("gave_up") or r.error or "")[:600]
            res["merged"] = [int(x) for x in out.get("merged") or [] if str(x).isdigit()]
            res["left_out"] = out.get("left_out") or []
            if not r.ok and clis.unreachable(r.error or "") and pool is not None and agent:
                # the model itself can't be reached: not this round's fault. Take the agent out until its next
                # sign-in check and run the round again on the next agent, without waiting out mergeRetryMinutes.
                res["result"] = "unreachable"
                pool.pause(agent, 360, f"model unreachable: {(r.error or '')[:160]}", shared=False)
                pool.accounts.setdefault(agent, {})["ok"] = False
        except rounds.CheckoutRecovery as e:
            rounds.start_file(round_file, n, branch, str(e.path), agent or "claude", batch, cfg.base_branch)
            res.update(result="checkout_failed", summary=str(e), key="", worktree=str(e.path), next_worktree=str(e.next_path))
        except Limited as e:
            res["result"], res["summary"] = "limited", f"{agent} hit a limit: {e}"
            if pool is not None and agent:
                pool.pause(agent, self.cfg.limit_backoff_minutes, str(e))
        except Exception as e:  # a crash in a round must never stop the loop
            res["result"], res["summary"] = "crashed", f"{type(e).__name__}: {e}"
        finally:
            if pool is not None and agent:
                pool.release(agent)
        rounds.finish_file(round_file, res["result"], (f"Landed as #{res['pr']}. " if res["pr"] else "") + res["summary"])
        if res["result"] in ("unreachable", "checkout_failed"):
            res["key"] = ""  # try the same PRs again at once on another agent
            self.wake()
        for item in res.get("left_out", []):
            if not isinstance(item, dict):
                continue
            p = next((p for p in batch if str(p.number) == str(item.get("pr"))), None)
            if p is not None:
                self.state.merge_excluded[str(p.number)] = {"head": p.head_sha, "why": str(item.get("why", ""))[:4000], "round": n, "at": now()}
        self.state.merge_last = res
        self.state.save(self.state_dir / "state.json")
        if res["result"] in ("crashed", "checkout_failed"):
            try:
                res["failure_issue"] = rounds.report_failure(gh, cfg, res, res.get("worktree") or str(rounds.checkout_path(self.ctx.repo)))
                self.state.save(self.state_dir / "state.json")
            except Exception as e:
                self.ctx.log(f"merge round {n}: could not file crash issue: {type(e).__name__}: {e}")
        self.ctx.log(f"merge round {n}: {res['result']}" + (f", landed as PR #{res['pr']}" if res["pr"] else "")
                     + (f" ({res['summary'][:200]})" if res["summary"] else ""))
        return res

    def _spend(self, agent: str, usd: float) -> None:
        if not usd:
            return
        st = self.state
        st.spent[today()] = round(st.spent.get(today(), 0.0) + usd, 4)
        day = st.spent_by_agent.setdefault(today(), {})
        day[agent] = round(day.get(agent, 0.0) + usd, 4)

    def merge_now(self) -> None:
        """Try the same PRs again now, without waiting out mergeRetryMinutes."""
        if self.state.merge_last:
            self.state.merge_last["key"] = ""
        self.wake()

    def hold_merging(self, on: bool) -> None:
        self.state.merge_held = bool(on)
        self.merge_why = "merging is paused" if on else ""
        self.state.save(self.state_dir / "state.json")
        self.ctx.log("merging paused from the app" if on else "merging resumed from the app")
        self.wake()

    def _watch(self) -> None:
        """Every `watch_minutes`: failed builds outside workers' PRs become tasks (see watcher.py)."""
        st = self.state
        if not self.watcher or (st.watch_last and (datetime.now(timezone.utc) - datetime.fromisoformat(st.watch_last))
                                < timedelta(minutes=self.cfg.watch_minutes)):
            return
        st.watch_last = now()
        try:
            self.watcher.watch(st.watch_seen)
        except Exception as e:  # GitHub down, a run file half-written: try again next time
            self.ctx.log(f"lane watcher: {type(e).__name__}: {e}")

    def free_slots(self) -> int:
        """0 workers: no new worker or fixer starts; merging, the Build Lane and issue closing are not slots."""
        return max(0, self.cfg.max_parallel) - len(self.jobs)

    def _start(self, coro, task_no: int, pr_no: int | None, prior_seen) -> None:
        self.active.add(task_no)
        job = asyncio.ensure_future(coro)
        self.jobs[job] = (task_no, pr_no, prior_seen)

    async def _wait_for_jobs(self, timeout: float | None) -> list[Outcome]:
        """Wait until at least one session ends (or `timeout`), and account for every one that has."""
        if not self.jobs:
            return []
        done, _ = await asyncio.wait(list(self.jobs), timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        return self._settle(done)

    def _settle(self, done) -> list[Outcome]:
        gh, cfg, st = self.ctx.gh, self.cfg, self.state
        outcomes = []
        for job in done:
            n, pr_n, prior = self.jobs.pop(job)
            self.active.discard(n)
            exc = job.exception() if not job.cancelled() else asyncio.CancelledError()
            if exc is not None:  # a bug or a crash in one job must not lose the others' results
                self.ctx.log(f"#{n} crashed: {type(exc).__name__}: {exc}")
                try:
                    gh.set_status(n, QUEUED if pr_n is None else PR)
                except Exception:
                    pass
                o = Outcome(n, "requeue", f"crashed: {type(exc).__name__}: {exc}", pr_number=pr_n)
            else:
                o = job.result()
            outcomes.append(o)
            st.running.pop(str(o.task), None)
            st.spent[today()] = st.spent.get(today(), 0.0) + o.cost_usd
            if self.ctx.agents is not None:
                day = st.spent_by_agent.setdefault(today(), {})
                for who, usd in o.costs.items():
                    day[who] = round(day.get(who, 0.0) + usd, 4)
                for old in [d for d in st.spent_by_agent if d < (datetime.now(timezone.utc) - timedelta(days=14)).strftime("%Y-%m-%d")]:
                    del st.spent_by_agent[old]
            self._ledger(o)
            if o.result == "fixed" and o.pr_number is not None:
                st.seen[str(o.pr_number)] = now()
            if o.result in ("signed_out", "limited", "agent_paused", "waiting_dependencies", "retry_fix") and pr_n is not None:
                # A new concrete repair plan or resolved prerequisite needs a fresh fixer opportunity,
                # including when the previous block was caused by exhausted fixer attempts.
                st.fix_attempts[str(pr_n)] = (0 if o.result in ("waiting_dependencies", "retry_fix") else
                                             max(0, st.fix_attempts.get(str(pr_n), 1) - 1))
                if prior is None:
                    st.seen.pop(str(pr_n), None)
                else:
                    st.seen[str(pr_n)] = prior
            if o.result == "signed_out":
                import auth
                st.signed_out = auth.check(cfg.auth, getattr(self.ctx.runtime, "saved_key", None))[1] or o.detail
            if o.result == "limited":
                until = datetime.now(timezone.utc) + timedelta(minutes=cfg.limit_backoff_minutes)
                st.paused_until = until.isoformat(timespec="seconds")
                st.paused_reason = o.detail
        self.recent = (outcomes + self.recent)[:20]
        if not self.dry_run:
            st.save(self.state_dir / "state.json")
        return outcomes

    def publish(self, outcomes: list[Outcome]) -> None:
        """Publish the last dispatch snapshot and completed outcomes without a blocking GitHub refetch."""
        gh, st = self.ctx.gh, self.state
        st.last_pass = now()
        tasks, prs = self._tasks, self._prs
        # Dispatch already fetched this snapshot. Rendering must never refetch the
        # entire queue or every PR's files before newly-created worker tasks can run.
        terminal = {"pr": PR, "fixed": PR, "blocked": BLOCKED,
                    "waiting_dependencies": QUEUED, "retry_fix": QUEUED}
        temporary = {"requeue", "limited", "signed_out", "agent_paused", "waiting_agent"}
        for o in outcomes:
            task = tasks.get(o.task)
            if task is None:
                continue
            if o.pr_number:
                task.pr_number = o.pr_number
            status = terminal.get(o.result)
            if o.result in temporary:
                status = PR if o.pr_number else QUEUED
            elif o.result == "gave_up":
                # A failed fixer keeps its PR for another bounded attempt; only its
                # explicit set_status(BLOCKED) (attempts exhausted) makes it blocked.
                status = BLOCKED if task.status == BLOCKED or not o.pr_number else PR
            if status is not None:
                task.labels = [l for l in task.labels if not graph.is_status_label(l)] + [graph.label_for(status)]
        for t in tasks.values():
            if t.number in self.active:
                t.labels = [l for l in t.labels if not graph.is_status_label(l)] + [graph.label_for(RUNNING)]
        waiting = graph.refresh_waits(tasks, {n: w for n, w in self._waiting.items() if n in tasks})
        text = self.status_text(tasks, prs, waiting, outcomes)
        self.snapshot = self.make_snapshot(tasks, prs, waiting, outcomes)
        if not self.dry_run:
            # Publishing the optional GitHub status issue cannot block local dispatch.
            try:
                aloop = asyncio.get_running_loop()
            except RuntimeError:
                gh.write_status(text)
            else:
                previous = getattr(self, "_status_job", None)
                if previous is None or previous.done():
                    async def send_status():
                        try:
                            await asyncio.to_thread(gh.write_status, text)
                        except Exception as e:
                            self.ctx.log(f"status issue update deferred: {e}")
                    self._status_job = aloop.create_task(send_status())
            (self.state_dir / "status.md").parent.mkdir(parents=True, exist_ok=True)
            (self.state_dir / "status.md").write_text(text, encoding="utf-8")
            st.save(self.state_dir / "state.json")

    async def run_forever(self, code_changed=None) -> None:
        """Keep workers dispatching during an active merge, even when a code update is pending.
        Once that round ends, drain workers and reload. Explicit restart/project switches
        still drain immediately; hot-read role/UI updates do not require a reload."""
        self._aloop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        loop = asyncio.get_running_loop()
        self._reload_draining = False
        self.update_pending = False
        while True:
            if not self._reload_draining:
                changed = bool(code_changed and code_changed())
                if changed and not self.update_pending and not self.restart_reason:
                    self.ctx.log("Updated code is installed; workers continue until the active merge round finishes"
                                 if self.round_job is not None else "Updated code is installed; preparing to reload")
                self.update_pending = changed
                if self.restart_reason or (changed and self.round_job is None):
                    self._reload_draining = True
                    self.ctx.log(f"{self.restart_reason or 'Fletchery' + chr(39) + 's code changed on disk'}; "
                                 "restarting once running sessions finish")
            try:
                self.dispatch(draining=self._reload_draining)
                await asyncio.sleep(0)  # let new worker coroutines start before optional status publishing
                self.publish([])
                self.sync_error = ""
            except Exception as e:
                self.passing = False
                self.sync_error = f"{type(e).__name__}: {e}"
                self.ctx.log(f"check failed: {self.sync_error}")
            if self._reload_draining and not self.jobs and self.round_job is None:
                return
            wait = min(30, self.cfg.poll_minutes * 60) if self.sync_error else self.cfg.poll_minutes * 60
            self.next_pass_at = datetime.fromtimestamp(datetime.now(timezone.utc).timestamp() + wait,
                                                       timezone.utc).isoformat(timespec="seconds")
            deadline = loop.time() + wait
            while True:
                left = deadline - loop.time()
                if left <= 0:
                    break
                waits = [asyncio.ensure_future(self._wake.wait())] + list(self.jobs) + \
                    ([self.round_job] if self.round_job is not None else [])
                done, _ = await asyncio.wait(waits, timeout=min(left, 60), return_when=asyncio.FIRST_COMPLETED)
                if waits[0] not in done:
                    waits[0].cancel()
                if self.round_job is not None and self.round_job in done:
                    self.round_job, self.round_info = None, {}
                    break
                ended = [j for j in done if j in self.jobs]
                if ended:
                    outcomes = self._settle(ended)
                    try:
                        self.publish(outcomes)
                    except Exception as e:
                        self.ctx.log(f"status update failed: {type(e).__name__}: {e}")
                    break
                if self._wake.is_set():
                    break
                if not self._reload_draining and (self.restart_reason or
                        (not self.update_pending and code_changed and code_changed())):
                    break
            self._wake.clear()

    update_pending: bool = False
    _reload_draining: bool = False

    def restart_status(self) -> str:
        if self.restart_reason:
            return self.restart_reason
        return "reloading updated code once running workers finish" if self._reload_draining else ""

    def pending_update_status(self) -> str:
        return ("Updated code is installed. Workers continue while the active merge round finishes; "
                "then Fletchery drains and reloads." if self.update_pending and not self._reload_draining else "")

    # ---- controls (called from the dashboard's thread) ----
    sync_error: str = ""
    restart_reason: str = ""  # set by request_restart: drain like a code update, then the launcher starts us again

    def request_restart(self, reason: str) -> None:
        """Finish running sessions, start nothing new, then restart (a project switch, a setting that needs it)."""
        self.restart_reason = reason
        self.wake()

    def wake(self) -> None:
        """Run a pass now instead of at the next poll."""
        if self._aloop and self._wake:
            self._aloop.call_soon_threadsafe(self._wake.set)

    def hold(self, on: bool) -> None:
        """Pause: start no new worker or fixer (running ones finish). Resume: back to normal, with a pass right away."""
        self.state.held = on
        self.ctx.log("paused by the dashboard" if on else "resumed by the dashboard")
        if not self.passing:
            self.state.save(self.state_dir / "state.json")
        if self.snapshot:
            self.snapshot["held"] = on
        self.wake()

    # ---- the Merge section: finished PRs waiting to merge, what each one sets moving ----
    def merge_rows(self, tasks: dict[int, Task], prs: dict[int, PullRequest]) -> list[dict]:
        """One row per task whose PR is open, most-unblocking first: merge order for the release round. A task only
        counts as done when its issue closes (its PR merges), so a long chain waits on its first PR."""
        cache: dict = self.__dict__.setdefault("_merge_files", {})
        import merge as merge_mod
        round_file = self.cfg.merge_round_file or str(self.state_dir / "merge-round.json")
        mv = merge_mod.view(round_file, self.cfg.heavy_lease_file, {n: p.head_sha for n, p in prs.items()})
        self.merge_view = mv
        flags_cfg = [f for f in (self.cfg.merge_flags or []) if f.get("paths") and f.get("flag")]
        url = f"https://github.com/{self.cfg.repo}"
        rows = []
        for t in tasks.values():
            if t.status != PR or not t.pr_number:
                continue
            pr = prs.get(t.pr_number)
            downstream = graph.unblocks(tasks, t.number)
            direct = sorted(n for n, o in tasks.items() if t.number in o.depends_on)
            flags: list[str] = []
            if pr and flags_cfg:
                key = (pr.number, pr.head_sha)
                files = cache.get(key) or []
                if key not in cache:
                    flags.append("Path checks pending; the merge round verifies them")
                for f in flags_cfg:
                    rx = re.compile(f["paths"])
                    if any(rx.search(p) for p in files) and f["flag"] not in flags:
                        flags.append(f["flag"])
            imports = ""
            if pr:
                m = re.search(r"^\s*lane-imports\s*:\s*(.*)$", pr.body or "", re.IGNORECASE | re.MULTILINE)
                imports = m.group(1).strip() if m else ""
            rows.append({"task": t.number, "title": t.title, "pr": t.pr_number, "pr_url": f"{url}/pull/{t.pr_number}",
                         "head": pr.head_sha[:8] if pr else "", "unblocks": downstream, "direct": direct,
                         "round": (mv["round"] or "?") if t.pr_number in mv["prs"] else graph.merge_round(pr.labels) if pr else "",
                         "in_round_branch": mv["in_branch"].get(t.pr_number),
                         "ready": bool(pr and "ready" in pr.labels), "conflict": bool(pr and pr.mergeable is False),
                         "imports": imports, "flags": flags,
                         "fix_attempts": self.state.fix_attempts.get(str(t.pr_number), 0),
                         "repair_wait": self.state.merge_excluded.get(str(t.pr_number), {}).get("why", "")})
        rows.sort(key=lambda r: (-len(r["unblocks"]), r["pr"]))
        return rows

    @staticmethod
    def merge_line(r: dict) -> str:
        bits = [f"PR #{r['pr']} (#{r['task']} {r['title']})"]
        if r["head"]:
            bits.append(f"head `{r['head']}`")
        bits.append(f"unblocks {len(r['unblocks'])}" + (": " + ", ".join(f"#{n}" for n in r["unblocks"][:12])
                                                        + (" …" if len(r["unblocks"]) > 12 else "") if r["unblocks"] else ""))
        if r["round"]:
            bits.append(f"round {r['round']}" + (", merged into the round branch" if r.get("in_round_branch") else ""))
        if r["imports"]:
            bits.append(f"imports: {r['imports']}")
        bits += [f"**{f}**" for f in r["flags"]]
        if r["conflict"]:
            bits.append("**conflicts**")
        if not r["ready"]:
            bits.append("not `ready`")
        if r["fix_attempts"]:
            bits.append(f"fixed {r['fix_attempts']}×")
        return "- " + " · ".join(bits)

    # ---- the dashboard's view of the last pass ----
    def make_snapshot(self, tasks: dict[int, Task], prs: dict[int, PullRequest], waiting: dict[int, str],
                      outcomes: list[Outcome]) -> dict:
        st, cfg = self.state, self.cfg
        url = f"https://github.com/{cfg.repo}"
        rows = []
        for t in sorted(tasks.values(), key=lambda t: (t.created_at, t.number)):
            rows.append({"number": t.number, "title": t.title, "status": t.status.split(":")[-1],
                         "why": waiting.get(t.number, "ready" if t.status == QUEUED else ""),
                         "depends": t.depends_on, "pr": t.pr_number, "branch": t.branch,
                         "fix_attempts": st.fix_attempts.get(str(t.pr_number), 0) if t.pr_number else 0,
                         "since": st.running.get(str(t.number), ""), "url": f"{url}/issues/{t.number}",
                         "agent": st.task_agent.get(str(t.number), ""),
                         "pr_url": f"{url}/pull/{t.pr_number}" if t.pr_number else ""})
        return {"repo": cfg.repo, "repo_url": url, "project": cfg.name, "label": graph.PREFIX,
                "workers_enabled": cfg.workers_enabled, "last_pass": st.last_pass, "auth": cfg.auth,
                "max_parallel": cfg.max_parallel, "poll_minutes": cfg.poll_minutes,
                "spent_today": round(st.spent.get(today(), 0.0), 2), "daily_budget": cfg.daily_budget_usd,
                "usage_week": self.usage_week(),
                "restarting": self.restart_status(), "update_pending": self.update_pending,
                "update_note": self.pending_update_status(),
                "held": st.held, "signed_out": st.signed_out, "limit_until": st.paused_until if st.paused_until and now() < st.paused_until else "",
                "limit_reason": st.paused_reason, "tasks": rows, "merge": self.merge_rows(tasks, prs),
                "merge_view": self.merge_view,
                "merge_auto": {"on": bool(cfg.merge_rounds), "held": st.merge_held, "running": dict(self.round_info),
                               "why": self.merge_why, "last": dict(st.merge_last or {}), "label": cfg.merge_label or "ready"},
                "this_pass": [{"task": o.task, "result": o.result, "detail": (o.detail or "").splitlines()[0] if o.detail else "",
                               "pr": o.pr_number, "cost": round(o.cost_usd, 2)} for o in outcomes],
                "watch": {"on": bool(self.watcher), "source": getattr(getattr(self.watcher, "source", None), "label", ""),
                          "last": st.watch_last, "recent": list(getattr(self.watcher, "recent", []))[:8]},
                "dry_run": self.dry_run, "agents": self.agents_view()}

    def agents_view(self) -> dict | None:
        pool = self.ctx.agents
        if pool is None:
            return None
        import keys
        used = sorted({k for s in pool.specs.values() for k in (s.key_env or [])})
        return {"multi": pool.multi, "list": pool.view(self.stats()), "keys": keys.listing(used),
                "spent_today": {k: round(v, 2) for k, v in self.state.spent_by_agent.get(today(), {}).items()},
                "escalate": self.ctx.escalate, "routing": {k: v for k, v in (pool.routing or {}).items()
                                                            if k in ("easy", "medium", "hard", "reviewers")},
                "mode": pool.mode, "bars": {lv: pool.bar(lv) for lv in ("easy", "medium", "hard")},
                "catalog": self._catalog_info(pool)}

    def _catalog_info(self, pool) -> dict:
        cat = pool.catalog()
        return {"updated": cat.get("updated", ""), "sources": list((cat.get("sources") or {}).values()),
                "models": len(cat.get("models") or {})}

    # ---- status issue ----
    def status_text(self, tasks: dict[int, Task], prs: dict[int, PullRequest], waiting: dict[int, str],
                    outcomes: list[Outcome]) -> str:
        st, cfg = self.state, self.cfg
        spent = st.spent.get(today(), 0.0)
        L = [f"# Fletchery status", "",
             f"Last pass: {st.last_pass or '(never)'} · today ${spent:.2f} of ${cfg.daily_budget_usd:.0f} · "
             f"{cfg.max_parallel} slots · {cfg.auth}" + (" (cost is an estimate; the plan's limits are what count)"
                                                         if cfg.auth == "subscription" else "")
             + (" · DRY RUN" if self.dry_run else ""), ""]
        if st.signed_out:
            L += [f"**Paused: Claude needs signing in again.** {st.signed_out}", ""]
        if st.held:
            L += ["**Paused from the dashboard.** Running work finishes; nothing new starts until Resume.", ""]
        if st.paused_until and now() < st.paused_until:
            L += [f"**Paused until {st.paused_until}: usage limit.** {st.paused_reason}", ""]
        def section(title: str, rows: list[str], empty: str) -> list[str]:
            return [f"## {title}", *(rows or [f"- {empty}"]), ""]

        running = [t for t in tasks.values() if t.status == RUNNING]
        pool = self.ctx.agents
        multi = pool is not None and pool.multi
        by = lambda t: f" · {st.task_agent[str(t.number)]}" if multi and str(t.number) in st.task_agent else ""  # noqa: E731
        L += section("Running", [f"- #{t.number} {t.title} (since {st.running.get(str(t.number), '?')}{by(t)})" for t in running], "(nothing)")
        queued = [t for t in tasks.values() if t.status == QUEUED]
        L += section("Queue", [f"- #{t.number} {t.title}: {waiting.get(t.number, 'ready')}" for t in queued], "(empty)")
        merge = self.merge_rows(tasks, prs)
        mv = self.merge_view
        L += ["## Merge", f"**Now:** {mv['headline']}", ""]
        if mv["steps"]:
            mark = {"done": "✓", "running": "▶", "waiting": "⏸", "failed": "✗", "todo": "○"}
            L += ["Steps: " + " → ".join(f"{mark[s['state']]} {s['name']}" for s in mv["steps"]), ""]
        in_round = [r for r in merge if r["pr"] in mv["prs"]]
        rest = [r for r in merge if r["pr"] not in mv["prs"]]
        if in_round:
            L += [f"**In round {mv['round'] or '?'}** ({len(in_round)}):", *[self.merge_line(r) for r in in_round], ""]
        L += [("**Waiting for a round**" if mv["prs"] else "**Waiting to merge**") + f" ({len(rest)}), most-unblocking "
              "first. A task that depends on one starts only once its PR merges:"]
        L += [self.merge_line(r) for r in rest] or ["- (nothing)"]
        L += [""]
        no_pr = [t for t in tasks.values() if t.status == PR and not t.pr_number]
        if no_pr:
            L += section("In the lane, PR not found", [f"- #{t.number} {t.title}" for t in no_pr], "")
        blocked = [t for t in tasks.values() if t.status == BLOCKED]
        L += section("Blocked (needs a person)", [f"- #{t.number} {t.title}" for t in blocked], "(none)")
        if multi:
            rows = []
            for a in pool.view(self.stats()):
                state = ("off" if not a["enabled"] else f"paused until {a['paused_until']}: {a['paused_reason']}"
                         if a["paused_until"] else "not signed in" if a["signed_in"] is False else "on")
                rates = ", ".join(f"{lv} {a[lv]['rate']:.0%} of {a[lv]['tried']}" for lv in ("easy", "medium", "hard")
                                  if a.get(lv, {}).get("tried"))
                rows.append(f"- **{a['name']}** ({a['model'] or a['kind']}, tier {a['tier']}): {state}"
                            + (f" · passed {rates}" if rates else ""))
            L += section("Agents", rows, "")
        if outcomes:
            L += section("This pass", [f"- #{o.task}: {o.result} {o.detail.splitlines()[0] if o.detail else ''} "
                                       f"(${o.cost_usd:.2f})" for o in outcomes], "")
        if self.dry_run and self.last_plan:
            L += ["## Would do", *[f"- {p}" for p in self.last_plan], ""]
        p = graph.PREFIX
        L += [f"_Rewritten by Fletchery every pass. Labels: `{p}:queued`, `{p}:running`, `{p}:pr`, `{p}:blocked`._"]
        return "\n".join(L)

    def usage_week(self) -> list[dict]:
        """The last 7 days (UTC dates, oldest first): estimated cost, and sessions from the ledger."""
        days = [(datetime.now(timezone.utc) - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(6, -1, -1)]
        sessions = {d: 0 for d in days}
        tasks = {d: 0 for d in days}
        led = self.state_dir / "ledger.jsonl"
        if led.exists():
            for line in led.read_text(encoding="utf-8").splitlines()[-5000:]:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                d = str(e.get("at", ""))[:10]
                if d in sessions:
                    sessions[d] += len(e.get("sessions") or [])
                    tasks[d] += 1
        return [{"date": d, "cost": round(self.state.spent.get(d, 0.0), 2), "sessions": sessions[d], "tasks": tasks[d]}
                for d in days]

    def _ledger(self, o: Outcome) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with (self.state_dir / "ledger.jsonl").open("a", encoding="utf-8") as f:
            row = {"at": now(), "task": o.task, "result": o.result, "pr": o.pr_number,
                   "cost_usd": round(o.cost_usd, 4), "sessions": o.sessions,
                   "detail": o.detail.splitlines()[0] if o.detail else ""}
            if o.agent:
                row.update(agent=o.agent, difficulty=o.difficulty, reviewer=o.reviewer, attempts=o.attempts)
            f.write(json.dumps(row) + "\n")
        self._stats_cache = None

    # ---- planning ----
    async def plan(self, goal: str, apply: bool = False) -> list[dict]:
        from runtime import role_prompt
        tasks = self.ctx.gh.open_tasks()
        existing = "\n".join(f"- #{t.number} {t.title}" for t in tasks.values()) or "- (none)"
        import rules as rules_mod
        prompt = role_prompt("planner", rules_mod.load(self.cfg.project_root), project=self.cfg.name, goal=goal,
                             existing=existing, base=self.cfg.base_branch)
        r = await self.ctx.runtime.run("planner", prompt, self.cfg.resolve("."), self.cfg.planner_budget_usd,
                                       self.cfg.planner_max_turns)
        if not r.ok:
            raise RuntimeError(f"planner failed: {r.error}")
        issues = r.output.get("issues", [])
        if apply:
            created: dict[int, int] = {}  # index in the plan -> issue number
            for i, it in enumerate(issues):
                deps = [created[d] for d in it.get("depends_on", []) if d in created] + \
                       [d for d in it.get("depends_on", []) if d not in range(len(issues))]
                body = it["body"].rstrip() + ("\n\nDepends on: " + ", ".join(f"#{d}" for d in deps) if deps else "")
                created[i] = self.ctx.gh.create_issue(it["title"], body, [graph.PREFIX, graph.label_for(QUEUED)])
        else:
            draft = "\n".join(f"- [{i}] {it['title']} (depends on {it.get('depends_on', [])})" for i, it in enumerate(issues))
            self.ctx.gh.comment(self.ctx.gh.status_issue(), f"Fletchery plan for: {goal}\n\n{draft}\n\n"
                                                             f"Run `foreman.py plan --apply` to create these.")
        return issues


def _tasks_by_pr(tasks: dict[int, Task], prs: dict[int, PullRequest]) -> dict[int, Task]:
    """Match open Fletchery PRs to their tasks by the `Closes #n` line (and the branch name as a fallback)."""
    out: dict[int, Task] = {}
    for pr in prs.values():
        for t in tasks.values():
            if re.search(r"\bcloses\s+#" + str(t.number) + r"\b", pr.body or "", re.I) or pr.head == t.branch:
                t.pr_number = pr.number
                if t.status == PR:
                    out[pr.number] = t
                break
    return out
