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
from github import PullRequest
from pipeline import Context, Outcome, fix_pr, run_task


@dataclass
class State:
    last_pass: str = ""
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

    def set_status(self, number: int, status: str) -> None:
        self._inner.set_status(number, status)
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
        for pr_num, task in by_pr.items():
            if task.number in self.active:
                continue
            pr = prs[pr_num]
            fb = gh.feedback_since(pr, st.seen.get(str(pr_num)))
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
        closed_deps = {d for t in tasks.values() for d in t.depends_on if d not in tasks and gh.issue_closed(d)}
        rd = readiness(tasks, closed_deps)
        rd.ready = [t for t in rd.ready if t.number not in self.active]
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
        self._tasks, self._prs, self._waiting = tasks, prs, rd.waiting
        self.snapshot = self.make_snapshot(tasks, prs, rd.waiting, [])

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
            if o.result in ("signed_out", "limited", "agent_paused") and pr_n is not None:  # retry this fix later, uncounted
                st.fix_attempts[str(pr_n)] = max(0, st.fix_attempts.get(str(pr_n), 1) - 1)
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
        """Rewrite the status issue and status.md, and refresh the board, from GitHub as it is now."""
        gh, st = self.ctx.gh, self.state
        st.last_pass = now()
        tasks, prs = self._tasks, self._prs
        if not self.dry_run:
            tasks = gh.open_tasks()
            prs = {p.number: p for p in gh.open_prs()}
            _tasks_by_pr(tasks, prs)
            for t in tasks.values():  # what this process runs right now shows as running, whatever GitHub says yet
                if t.number in self.active:
                    t.labels = [l for l in t.labels if not graph.is_status_label(l)] + [graph.label_for(RUNNING)]
        waiting = {n: w for n, w in self._waiting.items() if n in tasks}
        text = self.status_text(tasks, prs, waiting, outcomes)
        self.snapshot = self.make_snapshot(tasks, prs, waiting, outcomes)
        if not self.dry_run:
            gh.write_status(text)
            (self.state_dir / "status.md").parent.mkdir(parents=True, exist_ok=True)
            (self.state_dir / "status.md").write_text(text, encoding="utf-8")
            st.save(self.state_dir / "state.json")

    async def run_forever(self, code_changed=None) -> None:
        """Keep every slot busy: dispatch, then wait for a session to end, a wake (Check now, Resume, a new task) or the
        poll interval, and dispatch again. When `code_changed()` says yes, start nothing new, let running sessions
        finish, and return, so the launcher restarts Fletchery on the new code without cutting a session off."""
        self._aloop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        loop = asyncio.get_running_loop()
        draining = False
        while True:
            if not draining and (self.restart_reason or (code_changed and code_changed())):
                draining = True
                self.ctx.log(f"{self.restart_reason or 'Fletchery' + chr(39) + 's code changed on disk'}; "
                             "restarting once running sessions finish")
            try:
                self.dispatch(draining=draining)
                self.publish([])
            except Exception as e:  # one bad sync (GitHub down, say) must not end the loop
                self.passing = False
                self.ctx.log(f"check failed: {type(e).__name__}: {e}")
            if draining and not self.jobs:
                return
            wait = self.cfg.poll_minutes * 60
            self.next_pass_at = datetime.fromtimestamp(datetime.now(timezone.utc).timestamp() + wait,
                                                       timezone.utc).isoformat(timespec="seconds")
            deadline = loop.time() + wait
            while True:
                left = deadline - loop.time()
                if left <= 0:
                    break
                waits = [asyncio.ensure_future(self._wake.wait())] + list(self.jobs)
                done, _ = await asyncio.wait(waits, timeout=min(left, 60), return_when=asyncio.FIRST_COMPLETED)
                if waits[0] not in done:
                    waits[0].cancel()
                ended = [j for j in done if j in self.jobs]
                if ended:
                    outcomes = self._settle(ended)
                    try:
                        self.publish(outcomes)
                    except Exception as e:
                        self.ctx.log(f"status update failed: {type(e).__name__}: {e}")
                    break  # a slot is free: dispatch again now
                if self._wake.is_set():
                    break
                if not draining and (self.restart_reason or (code_changed and code_changed())):
                    break
            self._wake.clear()

    # ---- controls (called from the dashboard's thread) ----
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
                if key not in cache:
                    try:
                        cache[key] = self.ctx.gh.pr_files(pr.number)
                    except Exception as e:  # a file list is a nicety; never fail the pass for it
                        self.ctx.log(f"merge flags: PR #{pr.number} files: {type(e).__name__}: {e}")
                        cache[key] = None
                files = cache[key] or []
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
                         "fix_attempts": self.state.fix_attempts.get(str(t.pr_number), 0)})
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
                "held": st.held, "signed_out": st.signed_out, "limit_until": st.paused_until if st.paused_until and now() < st.paused_until else "",
                "limit_reason": st.paused_reason, "tasks": rows, "merge": self.merge_rows(tasks, prs),
                "merge_view": self.merge_view,
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
            if f"Closes #{t.number}" in (pr.body or "") or pr.head == t.branch:
                t.pr_number = pr.number
                if t.status == PR:
                    out[pr.number] = t
                break
    return out
