"""One pass of Fletchery: sync, feedback, dispatch, status. `Fletchery.once()` is what `foreman.py once` and `run` call."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import time
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from config import Config
import graph
from graph import BLOCKED, PR, QUEUED, RUNNING, Task, readiness, pick
from github import PullRequest, Feedback
from pipeline import Context, Outcome, fix_pr, run_task
from runtime import limit_pause_minutes


_STATE_SAVE_LOCK = threading.RLock()


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
    task_worker_rank: dict = field(default_factory=dict)
    task_agent: dict = field(default_factory=dict)            # task number -> the agent that wrote its branch
    accounts_checked: str = ""                                # ISO: when every agent's sign-in was last checked
    spent_by_agent: dict = field(default_factory=dict)        # YYYY-MM-DD -> {agent: USD}
    merge_round_no: int = 0                                   # the last merge round Fletchery started (rounds.py)
    merge_last: dict = field(default_factory=dict)            # {"round", "result", "at", "key", "summary", "pr"}
    merge_excluded: dict = field(default_factory=dict)         # PR -> {head, why, round, at}: unchanged failed heads wait for repair
    pr_checks: dict = field(default_factory=dict)             # PR -> {head, passed, at, output}: PC checks (prcheck.py)
    node_pr_checks: dict = field(default_factory=dict)        # PR -> {head, passed, at, job, machine, summary}: nodecheck.py
    playtest_issue: int = 0                                  # the playtest checklist issue (mergePlaytest)
    merge_parked: dict = field(default_factory=dict)           # PR -> {head, count, why, parked}: deferred again and again at one head
    merge_split: dict = field(default_factory=dict)            # {from_round, why, groups: [[{pr, head}]]}: a failed batch tried in parts
    merge_base_red: dict = field(default_factory=dict)         # {round, why, at}: the base branch itself fails; only fixes go in
    merge_starved: dict = field(default_factory=dict)          # {since, blocked, alerted, retried}: ready PRs but none eligible (watchdog.py)
    storage_last: dict = field(default_factory=dict)          # latest bounded cleanup result
    storage_requested: bool = False
    merge_resume: dict = field(default_factory=dict)          # explicit interrupted candidate + user authorization
    worker_handoffs: dict = field(default_factory=dict)     # task -> cooperative pause and checkpoint receipt
    integration_queue: list = field(default_factory=list)     # durable merge windows and package requests
    builds: list = field(default_factory=list)                # scheduled versioned builds (builds.py)
    planned_integration: bool = False                        # opt-in: feature work until a queued window is due
    merge_held: bool = False                                  # Pause merging in the Merge section
    pc_paused: list = field(default_factory=list)             # kinds of work paused on this PC: coding, checks, builds
    repair_continuations: dict = field(default_factory=dict)  # PR -> last applied bounded blocker plan
    emergency_repairs: dict = field(default_factory=dict)      # task -> build/merge evidence; repair classification survives restart
    resource_waits: dict = field(default_factory=dict)        # task -> earliest readiness check (ISO); survives restart
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
        with _STATE_SAVE_LOCK:
            # Serialize before touching the previous checkpoint. Unique same-directory
            # files also keep overlapping UI/engine saves from sharing a temp name.
            payload = json.dumps(self.__dict__, indent=2)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                                 prefix=path.name + ".", suffix=".tmp", delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


BLOCKER_RECHECK_MINUTES = 30  # how soon the Needs you backlog planner may look at the same task again
# A PR a merge round left out, whose repair attempts are used up, gets one fresh attempt once the base has had this long
# to move on (conflicts and flaky acceptance often clear). Before, it sat forever and its task showed as "ready"
# while nothing could start it (LifeShare, 2026-11-09: 11 such tasks, a queue that looked stopped).
EXHAUSTED_RETRY_HOURS = 12


def repairs_spent(pr: PullRequest | None, excluded: dict, attempts: int, limit: int, seen: str | None) -> bool:
    """A PR left out of a merge round at its current head that no fixer will pick up again: its attempts are used up,
    or its one round report was already handed to a fixer (seen) and that fixer ended without a new head."""
    if pr is None or not excluded or excluded.get("head") != pr.head_sha:
        return False
    return attempts >= limit or bool(seen and str(excluded.get("at", "")) <= seen)


def exhausted_wait(task_no: int, pr: PullRequest | None, excluded: dict, attempts: int, limit: int,
                   seen: str | None = None) -> str:
    """Why a queued task whose PR was left out of a merge round is not moving, or "" when it can be repaired."""
    if not repairs_spent(pr, excluded, attempts, limit, seen):
        return ""
    why = " ".join(str(excluded.get("why", "")).split())[:240]
    rnd = f" of merge round {excluded['round']}" if excluded.get("round") else ""
    if excluded.get("retried"):
        return (f"needs a person: PR #{pr.number} was left out{rnd} at its current head and its repair attempts "
                f"(including the fresh one) are used up. {why}").strip()
    try:
        at = datetime.fromisoformat(str(excluded.get("at", "")).replace("Z", "+00:00"))
        when = (at + timedelta(hours=EXHAUSTED_RETRY_HOURS)).isoformat(timespec="minutes")[:16].replace("T", " ") + "Z"
    except ValueError:
        when = "soon"
    return (f"PR #{pr.number} was left out{rnd} and its repairs ended without a fix; "
            f"one fresh repair starts at {when}. {why}").strip()


def _recent(stamp: str, minutes: float) -> bool:
    if not stamp:
        return False
    try:
        at = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return False
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - at).total_seconds() < minutes * 60


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
        if not dry_run and getattr(ctx, "usage_log", None) is None:
            ctx.usage_log = state_dir / "usage.jsonl"
        if ctx.agents is not None and ctx.usage_log is not None and hasattr(ctx.agents, "seed_runs"):
            import usage
            ctx.agents.seed_runs(usage.runs_today(ctx.usage_log))
        # Migrate old interrupted/incomplete-only exclusions without touching
        # confirmed failed heads or spending another repair attempt.
        import rounds
        corrected = []
        for number, entry in list(self.state.merge_excluded.items()):
            if rounds.deferred(entry):
                corrected.append({"pr": int(number), **entry})
                del self.state.merge_excluded[number]
        if corrected:
            current = [item for item in corrected if item.get("round") == self.state.merge_last.get("round")]
            if current:
                self.state.merge_last.setdefault("deferred", []).extend(current)
            if not dry_run:
                self.state.save(state_dir / "state.json")
        self.last_plan: list[str] = []  # what a dry run would have done
        self.snapshot: dict = {}        # the last pass, structured, for the dashboard
        self.next_pass_at: str = ""
        self.passing = False
        self._wake: asyncio.Event | None = None
        self.active: set[int] = set()   # tasks a session of THIS process is working on right now
        self.emergency_active: set[int] = set()
        self.emergency_why = "No build or merge repairs waiting."
        self.jobs: dict = {}            # asyncio job -> (task number, PR number or None, the PR's last seen feedback)
        self.recent: list[Outcome] = []
        self._tasks: dict = {}
        self._prs: dict = {}
        self._waiting: dict = {}
        self._aloop: asyncio.AbstractEventLoop | None = None
        self.watcher = None
        self._storage_job = None
        self.disk_why, self.disk_free_gb, self._disk_cleanup_at = "", None, 0.0
        self.task_retire_last: dict = {}
        self.disk_report: dict = {}
        try:
            self.disk_report = json.loads((Path(state_dir) / "disk-report.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        self._storage_after_round = False  # a round just landed: clean now, even while workers run
        self.round_job = None           # the merge round running now (rounds.py), an asyncio task
        self.live_round = None          # that round's number while it runs; a round file naming another is left over
        self.booted = now()             # a round file started before this is left over from an earlier Fletchery
        self.ended_rounds: set = set()  # rounds this Fletchery ran that have ended (a cancel can leave their file running)
        self.round_info: dict = {}      # {"round", "agent", "prs", "started"} of that round
        self.merge_priority = False     # reserve new-job capacity for an eligible or active merge
        self.merge_backlog = 0          # the workers' PRs labelled for merging, last pass
        self.backlog_paused = False     # backpressure: new feature tasks wait until the backlog falls to the low mark
        self.merge_why = ""             # why no round is starting, for the Merge section
        self._checkout_stall = None     # {since, why, minutes, loud}: the merge checkout has been owned by a process
        from plans import Plans
        self.plans = Plans(self)
        from worker_handoff import WorkerHandoffs
        self.handoffs = self.ctx.handoffs = WorkerHandoffs(self)
        self._package_poll_job = None
        self._package_poll_at = 0.0
        from builds import Builds
        self.builds = Builds(self)
        self._builds_job = None
        self._builds_at = 0.0
        self._stats_cache: tuple[float, object] | None = None
        if ctx.agents is not None:  # one agent's pause and who wrote which branch survive a restart
            ctx.agents.paused = self.state.agent_paused
            ctx.task_agents = self.state.task_agent
            ctx.task_worker_ranks = self.state.task_worker_rank
            ctx.stats = self.stats
            ctx.agents.catalog_override = self.state_dir / "model_scores.json"
        if not dry_run and ctx.cfg.workers_enabled:
            try:
                import watcher
                self.watcher = watcher.make(ctx.cfg, ctx.gh, ctx.log)
            except Exception as e:  # never let the watcher keep the workers from starting
                ctx.log(f"lane watcher off: {type(e).__name__}: {e}")

    def resource_wait_reason(self, task: int) -> str:
        from resource_waits import wait_reason
        key = str(task)
        if key not in self.state.resource_waits:
            return ""
        reason = wait_reason(self.cfg.heavy_lease_file, self.state.resource_waits[key], now())
        if not reason:
            del self.state.resource_waits[key]
        return reason

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
        if self._storage_job is not None:
            await self._storage_job
        if self._package_poll_job is not None:
            await self._package_poll_job
        self.publish(outcomes)
        # The one-shot command owns its event loop and exits next. Finish its
        # final status write before asyncio.run cancels pending tasks on shutdown.
        status_job = getattr(self, "_status_job", None)
        if status_job is not None:
            await status_job
        return outcomes

    # ---- dispatch: sync with GitHub and start sessions in the free slots (never waits for them) ----
    def dispatch(self, draining: bool = False) -> None:
        gh, cfg, st = self.ctx.gh, self.cfg, self.state
        self.last_plan = []
        self.passing = True
        self.dispatch_warning = ""
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
        # A round file left saying "running" by a restart, crash or cancelled round: history now, so Merging reads Idle.
        try:
            left = __import__("rounds").stop_orphan(self.cfg.merge_round_file or str(self.state_dir / "merge-round.json"),
                                                    self.live_round, "Stopped: Fletchery restarted or the round was cancelled. "
                                                    "Its PRs go into the next round.", self.booted, self.ended_rounds)
            if left:
                self.ctx.log(f"merge round {left} was left running by an earlier Fletchery or a cancel; marked stopped")
        except Exception as e:
            self.ctx.log(f"left-over round check: {type(e).__name__}: {e}")
        # Ready-only external PRs also need their existing-branch repair task.
        repair_prs = {int(n) for n in st.merge_excluded if str(n).isdigit()}
        repair_prs.update(int(n) for t in tasks.values() for n in
                         re.findall(r"<!-- fletchery-repair-pr:(\d+) -->", t.body or ""))
        if repair_prs - prs.keys():
            try:
                prs.update({p.number: p for p in gh.labelled_prs(cfg.merge_label or "ready", cfg.base_branch)
                            if p.number in repair_prs})
            except Exception as e:
                self.ctx.log(f"merge repair PR lookup deferred: {e}")
        by_pr = _tasks_by_pr(tasks, prs)
        # A task whose PR merged into a non-default branch (Test-Staging) stays open: GitHub only acts on "Closes #n"
        # for the default branch. Close it here, or everything depending on it waits forever (round 7, 2026-11-03).
        # Blocked tasks too: a PR landed by hand or by a later round while its task sat in Needs you, and the blocker
        # planner can only say "nothing left to do" (LifeShare #1400, #1448, #940, #1549 on 2026-11-14).
        for t in [t for t in tasks.values() if (t.status == PR and not t.pr_number) or t.status == BLOCKED]:
            try:
                m = gh.merged_pr(t.branch)  # a "Repair PR #n" task's branch is that PR's own head
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

        def close_landed(number: int) -> bool:
            # An open prerequisite that is not a task here (a thread's own issue) whose "Closes #n" PR merged into the
            # base branch is done: GitHub only closes it for default-branch merges. Close it, as for our own tasks.
            m = gh.closed_by_merged_pr(number, cfg.base_branch)
            if not m:
                return False
            self.last_plan.append(f"close #{number}: PR #{m[0]} merged into {m[1]}")
            if not self.dry_run:
                gh.comment(number, f"Fletchery: PR #{m[0]} merged into {m[1]} at {m[2]}. GitHub closes an issue from a "
                                   f"PR's `Closes #{number}` only for merges into the default branch, so Fletchery closes "
                                   "it, and the tasks that depend on it can start.")
                gh.close_issue(number)
            self.ctx.log(f"#{number} closed: PR #{m[0]} merged into {m[1]} (a prerequisite other tasks wait on)")
            return True

        # A shared prerequisite is queried only once per pass. A lookup failure
        # keeps dependents waiting, without blocking unrelated work.
        dependency_closed: dict[int, bool] = {}
        dependency_errors: set[int] = set()

        def is_closed(number: int) -> bool:
            if number not in dependency_closed:
                try:
                    dependency_closed[number] = gh.issue_closed(number) or close_landed(number)
                except Exception as e:
                    dependency_closed[number] = False
                    dependency_errors.add(number)
                    self.dispatch_warning = "GitHub could not verify prerequisites: " + ", ".join(f"#{d}" for d in sorted(dependency_errors))
                    self.ctx.log(f"#{number}: prerequisite lookup deferred: {e}")
            return dependency_closed[number]

        external_dependencies = {d for t in tasks.values() for d in t.depends_on if d not in tasks}
        external_dependencies.update(d for p in prs.values() for d in Task(0, "", p.body).depends_on
                                     if d not in tasks and d not in prs)
        closed_deps = {d for d in sorted(external_dependencies) if is_closed(d)}
        repository_results: dict[tuple[str, int], bool] = {}

        def verify_repository_dependencies(declarations) -> set[tuple[str, int]]:
            refs = {ref for task in declarations for ref in task.repository_dependencies}
            for repo, number in sorted(refs - repository_results.keys()):
                try:
                    repository_results[repo, number] = gh.issue_closed(number, repository=repo)
                except Exception as e:
                    repository_results[repo, number] = False
                    self.dispatch_warning = f"GitHub could not verify prerequisite {repo}#{number}"
                    self.ctx.log(f"{repo}#{number}: prerequisite lookup deferred: {e}")
            return {ref for ref in refs if repository_results[ref]}

        pr_declarations = {n: Task(n, p.title, p.body) for n, p in prs.items()}
        repository_closed = verify_repository_dependencies([*tasks.values(), *pr_declarations.values()])
        # Merge scheduling runs first using this pass's verified task snapshot.
        # Existing sessions continue, but new workers/fixers/planners must not
        # claim the next free agent/PC opportunity ahead of a ready merge.
        self._tasks, self._prs = tasks, prs
        self._known_closed_dependencies = closed_deps
        self._dependency_lookup = is_closed
        self._verify_repository_dependencies = verify_repository_dependencies
        self.disk_why = self._disk_guard()
        if st.storage_requested:
            resume = st.merge_resume
            wait_for = resume.get("wait_for") if resume else None
            from recovery import ready_signal
            ready_resume = resume.get("authorized") and (not wait_for or ready_signal(self.state_dir, wait_for))
            if not ready_resume:
                self._maybe_storage(tasks, draining)
        if not self.dry_run and not draining:
            import time
            if (self._package_poll_job is None or self._package_poll_job.done()) and time.monotonic() >= self._package_poll_at:
                self._package_poll_at = time.monotonic() + 30
                self._package_poll_job = asyncio.ensure_future(asyncio.to_thread(self.plans.package_step))
            if (self._builds_job is None or self._builds_job.done()) and time.monotonic() >= self._builds_at:
                self._builds_at = time.monotonic() + 30
                self._builds_job = asyncio.ensure_future(asyncio.to_thread(self.builds.tick))
        hold_new = draining or bool(self.disk_why)  # a running round finishes; no new round, build or check starts
        self._merge_round(hold_new)
        self._maybe_node_pr_checks(hold_new)
        if self.plans.active_merge and self.plans.active_merge["state"] == "draining" and not self.merge_priority:
            self.plans.update(self.plans.active_merge, state="queued", reason=self.merge_why)
        plan_draining = bool(self.merge_priority and self.plans.active_merge and self.plans.active_merge["state"] == "draining")
        coding_off = "coding" in st.pc_paused  # Building -> Machines: coding paused on this PC; running sessions finish
        paused = paused or plan_draining or coding_off or bool(self.disk_why)
        # A running round reserves its own account, not the whole PC: other accounts keep writing and fixing, and the
        # round's account starts at most `round_shared_sessions` new ones. Without an agent pool (one Claude for
        # everything) or with -1, the round still holds every new session, as before.
        round_shares = self._round_shares(pool)
        worker_paused = paused or (self.merge_priority and not round_shares)
        backlog_full = self._backlog_full(prs)
        budget_left = st.spent.get(today(), 0.0) < cfg.daily_budget_usd
        emergency_numbers = self.repair_numbers(tasks)
        self.emergency_why = ("Emergency workers are set to 0; running repairs finish." if not cfg.emergency_workers else
                              self.disk_why if self.disk_why else
                              "Coding is paused on this PC." if coding_off else
                              "Fletchery is paused or restarting." if paused else
                              "Daily budget spent." if not budget_left else "No build or merge repairs waiting.")
        # 1. feedback on open PRs -> fixers (one per task at a time)
        ordered_feedback = sorted(by_pr.items())
        ordered_feedback = [x for x in ordered_feedback if x[0] > st.feedback_cursor] + [x for x in ordered_feedback if x[0] <= st.feedback_cursor]
        inspected_feedback = 0
        for pr_num, task in ordered_feedback:
            if paused or not budget_left or inspected_feedback >= 6:
                break
            if (worker_paused or self.free_slots() <= 0) and self.emergency_free() <= 0:
                break
            resource_reason = self.resource_wait_reason(task.number)
            if resource_reason and task.number in emergency_numbers and cfg.emergency_workers and not paused and budget_left:
                self.emergency_why = resource_reason
            if task.number in self.active or resource_reason:
                continue
            if any(d in tasks or not is_closed(d) for d in task.depends_on):
                continue
            if (task.repository_dependencies | pr_declarations[pr_num].repository_dependencies) - repository_closed:
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
            elif (repairs_spent(pr, excluded, st.fix_attempts.get(str(pr_num), 0), cfg.fixer_attempts, st.seen.get(str(pr_num)))
                  and not excluded.get("retried") and excluded.get("at")
                  and not _recent(excluded["at"], EXHAUSTED_RETRY_HOURS * 60)):
                # Repairs spent at this head: one fresh attempt, now that the base has moved on. It is the last one:
                # if it ends without a new head, the task says it needs a person.
                if not self.dry_run:
                    excluded["retried"] = now()
                    st.fix_attempts[str(pr_num)] = max(0, cfg.fixer_attempts - 1)
                fb.append(Feedback(pr_num, "merge", f"Fresh repair: a merge round left this PR out and its repair attempts "
                                   f"ran out more than {EXHAUSTED_RETRY_HOURS} hours ago. The base branch has moved since: "
                                   f"merge the latest {cfg.base_branch} into this branch, resolve any conflicts, and fix "
                                   "what the round reported, then re-run the issue's named checks. Round report: "
                                   + excluded.get("why", ""), now()))
            plans = re.findall(r"<!-- fletchery-blocker-applied:([a-f0-9]+) -->", task.body or "")
            if task.status == QUEUED and plans and st.repair_continuations.get(str(pr_num)) != plans[-1]:
                # The planner explicitly authorized a bounded continuation on an
                # existing PR. Reuse its branch and reset this plan's attempt once.
                fb.append(Feedback(pr_num, "merge", "Apply the saved automatic continuation/prerequisites on the existing PR branch. Read the current issue and comments.", now()))
            if not fb:
                continue
            repair = task.number in emergency_numbers or (bool(excluded) and excluded.get("head") == pr.head_sha) or any(
                f.kind in ("check", "status", "conflict", "merge") for f in fb)
            emergency = repair and self.emergency_free() > 0
            if not emergency and (worker_paused or self.free_slots() <= 0):
                continue
            if repair and not self.dry_run:
                st.emergency_repairs[str(task.number)] = pr_num
                emergency_numbers.add(task.number)
            if self.watcher:  # a failure on the build PC: give the fixer the failing step's log
                for f in fb:
                    if f.kind == "status":
                        f.text = self.watcher.enrich(f.text, f.text.split("`")[1] if f.text.count("`") >= 2 else "")
            continuation = task.status == QUEUED and plans and st.repair_continuations.get(str(pr_num)) != plans[-1]
            if continuation and not self.dry_run:
                st.repair_continuations[str(pr_num)] = plans[-1]
                st.fix_attempts[str(pr_num)] = 0
            attempt = (0 if continuation else st.fix_attempts.get(str(pr_num), 0)) + 1
            self.last_plan.append(f"fix PR #{pr_num} (task #{task.number}, attempt {attempt}): {fb[0].text[:80]}")
            if self.dry_run:
                continue
            prior = st.seen.get(str(pr_num))
            st.seen[str(pr_num)] = max(f.at for f in fb)
            st.fix_attempts[str(pr_num)] = attempt
            self._start(fix_pr(task, pr, fb, attempt, self.ctx), task.number, pr_num, prior, emergency=emergency)

        # 2. ready tasks -> workers, within slots and budget
        # Existing Needs you tasks get the same bounded prerequisite planning as new blocks.
        # One planner per pass, counted against worker slots and the daily cap.
        blocker_busy = any(t.status == BLOCKED and t.number in self.active for t in tasks.values())
        if cfg.auto_prerequisites and not blocker_busy and not paused and budget_left and (self.emergency_free() > 0 or (not worker_paused and self.free_slots() > 0)):
            import blockers
            import rules as rules_mod
            for t in sorted(tasks.values(), key=lambda t: (st.blocker_checked.get(str(t.number), ""), t.created_at, t.number)):
                if t.status != BLOCKED or t.number in self.active or self.resource_wait_reason(t.number):
                    continue
                if _recent(st.blocker_checked.get(str(t.number), ""), BLOCKER_RECHECK_MINUTES):
                    # A planner that deferred (an exception, no agent free) is not retried every pass: #929 ran 788
                    # times in 13 hours on 2026-11-07, two GitHub reads each, while the REST limit was the bottleneck.
                    # New blocks are planned straight away by the pipeline; a person's answer re-queues at once.
                    continue
                if t.repository_dependencies - repository_closed:
                    continue
                emergency = t.number in emergency_numbers and self.emergency_free() > 0
                if not emergency and (worker_paused or self.free_slots() <= 0):
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
                    self._start(blockers.resolve(t, self.ctx, Outcome(t.number, "blocked")), t.number, None, None, emergency=emergency)
                break
        rd = readiness(tasks, closed_deps, repository_closed)
        for number in rd.waiting:
            unknown = dependency_errors.intersection(tasks[number].depends_on)
            if unknown:
                rd.waiting[number] = "prerequisite status unavailable; retrying GitHub for " + ", ".join(f"#{d}" for d in sorted(unknown))
        fixing = {t.number for t in by_pr.values()}
        for pr_num, t in by_pr.items():
            # A queued task with an open PR moves only through fixers; say why when none started (it showed "ready").
            if t.status != QUEUED or t.number in self.active:
                continue
            parked = st.merge_parked.get(str(pr_num)) or {}
            pr = prs.get(pr_num)
            if parked and pr is not None and parked.get("head") == pr.head_sha:
                rd.waiting[t.number] = ("needs a person: PR #%d is parked (deferred twice at this head): %s"
                                        % (pr_num, " ".join(str(parked.get("why", "")).split())[:240])).strip()
                continue
            stuck = exhausted_wait(t.number, pr, st.merge_excluded.get(str(pr_num), {}),
                                   st.fix_attempts.get(str(pr_num), 0), cfg.fixer_attempts, st.seen.get(str(pr_num)))
            rd.waiting[t.number] = stuck or f"repairing PR #{pr_num}: waits for new feedback or a free repair slot"
        rd.ready = [t for t in rd.ready if t.number not in self.active and t.number not in fixing]
        free_resources = []
        for t in rd.ready:
            reason = self.resource_wait_reason(t.number)
            if reason:
                rd.waiting[t.number] = reason
            else:
                free_resources.append(t)
        rd.ready = free_resources
        for number in tasks:
            reason = self.resource_wait_reason(number)
            if reason:
                rd.waiting[number] = reason
        from worker_ranks import configure as configure_worker_ranks
        self.worker_rank_view = configure_worker_ranks(self, rd.ready)
        if pool is not None:  # a task whose agents are all off, paused or busy waits instead of starting and stopping
            stats = self.stats()
            free = []
            for t in rd.ready:
                choose = pool.choose_without_rank_limits if t.number in emergency_numbers and cfg.emergency_workers else pool.choose
                if choose(t.title, t.body, t.labels, len(t.depends_on), stats) is not None:
                    free.append(t)
                else:
                    from agents import override
                    want = override(t.body, t.labels)
                    rd.waiting[t.number] = (f"waits for agent {want} (off, paused or busy)" if want
                                            else "waits for a free agent: every suitable one is off, paused or busy")
            rd.ready = free
        spent = st.spent.get(today(), 0.0)
        budget_left = spent < cfg.daily_budget_usd
        pending_repairs = [t for t in tasks.values() if t.number in emergency_numbers and t.status in (QUEUED, BLOCKED)]
        if pending_repairs and cfg.emergency_workers and not paused and budget_left:
            first = pending_repairs[0]
            self.emergency_why = rd.waiting.get(first.number) or ("Repair needs bounded blocker planning or human input." if first.status == BLOCKED else "Waiting for a suitable repair agent.")
        emergency_ready = [t for t in rd.ready if t.number in emergency_numbers]
        if emergency_ready and cfg.emergency_workers and not paused and budget_left:
            self.emergency_why = "Waiting for the emergency repair slot or a suitable agent."
        emergency_chosen = pick(emergency_ready, self.emergency_free()) if budget_left and not paused else []
        chosen = pick([t for t in rd.ready if t not in emergency_chosen], self.free_slots()) if budget_left and not worker_paused and not backlog_full else []
        chosen += emergency_chosen
        for t in rd.ready:
            if t in chosen:
                self.last_plan.append(f"run #{t.number} {t.title} on {t.branch}")
            elif draining:
                rd.waiting[t.number] = "on hold: Fletchery is restarting on new code"
            elif st.signed_out:
                rd.waiting[t.number] = "on hold: Claude needs signing in again"
            elif st.held:
                rd.waiting[t.number] = "on hold: Fletchery is paused"
            elif coding_off:
                rd.waiting[t.number] = "on hold: coding is paused on this PC (Building, Machines)"
            elif paused:
                rd.waiting[t.number] = f"paused for the usage limit until {st.paused_until}"
            elif t.number in emergency_numbers and cfg.emergency_workers:
                rd.waiting[t.number] = "waiting for the emergency repair slot"
            elif self.merge_priority and not round_shares:
                rd.waiting[t.number] = "waiting for merge priority; existing jobs continue"
            elif backlog_full:
                rd.waiting[t.number] = (f"merge backlog full: {self.merge_backlog} PRs wait to merge; new tasks start again "
                                        f"at {self._backlog_marks()[1]} (fixes continue)")
            else:
                rd.waiting[t.number] = ("daily budget spent" if not budget_left else
                                        "writing paused: workers set to 0" if cfg.max_parallel <= 0 else
                                        f"no free slot ({cfg.max_parallel})")
        if not self.dry_run:
            for t in chosen:
                st.running[str(t.number)] = now()
                t.labels = [l for l in t.labels if not graph.is_status_label(l)] + [graph.label_for(RUNNING)]
                t.emergency_writer = t in emergency_chosen
                self._start(run_task(t, self.ctx), t.number, None, None, emergency=t in emergency_chosen)
            st.save(self.state_dir / "state.json")
        # Merge eligibility must see this pass's issue dependencies, including on
        # the very first pass after startup rather than the previous snapshot.
        self._tasks, self._prs, self._waiting = tasks, prs, rd.waiting
        self.snapshot = self.make_snapshot(tasks, prs, rd.waiting, [])
        self._maybe_storage(tasks, draining)

    def request_storage_cleanup(self) -> dict:
        self.state.storage_requested = True
        self.state.save(self.state_dir / "state.json")
        self.wake()
        return {"message": "Cleanup queued for a safe idle opportunity; protected work stays in place."}

    def storage_view(self) -> dict:
        return {"enabled": self.cfg.auto_cleanup, "retention_hours": self.cfg.cleanup_retention_hours,
                "running": bool(self._storage_job and not self._storage_job.done()),
                "requested": self.state.storage_requested, "last": dict(self.state.storage_last),
                "free_gb": self.disk_free_gb, "min_free_gb": self.cfg.work_min_free_gb, "disk_why": self.disk_why,
                "tasks": dict(self.task_retire_last), "report": dict(self.disk_report)}

    def _disk_guard(self) -> str:
        """Below `work_min_free_gb` on the worktree drive: hold new work and clean finished task copies now
        (at most every 10 minutes). Returns the reason shown on the dashboard, or ""."""
        import shutil
        import time as _time
        limit = float(self.cfg.work_min_free_gb or 0)
        root = Path(self.cfg.worktree_root)
        try:
            free = shutil.disk_usage(root if root.exists() else (root.anchor or root)).free / 1024 ** 3
        except OSError:
            return ""
        self.disk_free_gb = round(free, 1)
        if limit <= 0 or self.dry_run or free >= limit:
            return ""
        running = bool(self._storage_job and not self._storage_job.done())
        if not running and not self.state.storage_requested and _time.monotonic() >= self._disk_cleanup_at:
            self._disk_cleanup_at = _time.monotonic() + 600
            self.state.storage_requested = True
            self.ctx.log(f"Disk guard: {free:.0f} GB free on the worktree drive, under {limit:g} GB; "
                         "new work waits while finished task copies are cleaned")
        return (f"Only {free:.0f} GB free on the worktree drive (under {limit:g} GB): new tasks, merge rounds and builds "
                "wait while finished task copies are cleaned. Running work finishes.")

    def _maybe_storage(self, tasks: dict, draining: bool = False) -> None:
        # The PC is almost never fully idle, so waiting for zero workers meant cleanup last ran a day before E: filled
        # (2026-11-07). After a landed round it runs alongside workers: every checkout it touches is proven idle,
        # finished and published, and open tasks' checkouts are never candidates.
        after_round = self._storage_after_round and self.cfg.auto_cleanup
        urgent = bool(self.disk_why)  # low disk: clean beside running work; every removal proves its checkout idle
        if (self.dry_run or draining or self.state.held or not hasattr(self.ctx.repo, "git") or (self.jobs and not after_round and not urgent)
                or (self.round_job is not None and not urgent)
                or (self._storage_job and not self._storage_job.done()) or (not self.cfg.auto_cleanup and not self.state.storage_requested)):
            return
        last = self.state.storage_last
        if not after_round and last.get("checked_at") and (last.get("error") or not self.state.storage_requested):
            interval = 300 if last.get("error") else 3600
            try:
                if (datetime.now(timezone.utc) - datetime.fromisoformat(last["checked_at"].replace("Z", "+00:00"))).total_seconds() < interval:
                    return
            except ValueError:
                pass
        protected = [self.cfg.project_root]
        protected.extend(r["worktree"] for r in self.state.worker_handoffs.values()
                         if r.get("state") == "checkpointed" and r.get("worktree"))
        checkpoint = self.state.merge_resume.get("checkpoint") or {}
        if checkpoint.get("worktree"):
            protected.append(checkpoint["worktree"])
        progress = __import__("rounds").read_file(self.cfg.merge_round_file or str(self.state_dir / "merge-round.json"))
        if progress.get("worktree"):
            protected.append(progress["worktree"])
        immediate = self.state.storage_requested
        self._storage_after_round = False
        async def clean():
            import storage
            try:
                trimmed = await asyncio.to_thread(storage.trim_merge_checkouts, self.ctx.repo,
                    protected_paths=protected, lease_file=self.cfg.heavy_lease_file)
                __import__("rounds").write_file(str(self.state_dir / "merge-trim.json"), trimmed)
                if trimmed.get("trimmed"):
                    self.ctx.log(f"Storage: deleted build outputs in {len(trimmed['trimmed'])} folders of idle merge checkouts "
                                 f"({trimmed['freed_bytes'] / 1024**3:.2f} GiB)")
            except Exception as exc:
                self.ctx.log(f"Storage: merge checkout trim failed: {type(exc).__name__}: {exc}")
            try:
                retired = await asyncio.to_thread(storage.retire_merge_checkouts, self.ctx.repo,
                    protected_paths=protected, lease_file=self.cfg.heavy_lease_file, backup_dir=self.cfg.merge_backup_dir,
                    keep=self.cfg.keep_merge_checkouts, retention_hours=self.cfg.cleanup_retention_hours)
                __import__("rounds").write_file(str(self.state_dir / "merge-retire.json"), retired)
                if retired.get("retired"):
                    self.ctx.log(f"Storage: removed {len(retired['retired'])} old merge checkouts "
                                 f"({retired['freed_bytes'] / 1024**3:.2f} GiB); uncommitted files backed up first")
            except Exception as exc:
                self.ctx.log(f"Storage: old merge checkout removal failed: {type(exc).__name__}: {exc}")
            try:
                result = await asyncio.to_thread(storage.maintain, self.ctx.repo, self.ctx.gh, self.state_dir,
                    protected_paths=protected, open_tasks=list(tasks), lease_file=self.cfg.heavy_lease_file,
                    retention_hours=0 if immediate else self.cfg.cleanup_retention_hours, limit=25)
                __import__("rounds").write_file(str(self.state_dir / "storage-cleanup.json"), result)
                self.state.storage_last = {**result, "eligible": result.get("eligible", [])[:10], "skipped": result.get("skipped", [])[:20]}
                if not result.get("error"):
                    self.state.storage_requested = False
                if result.get("retired"):
                    self.ctx.log(f"Storage: retired {len(result['retired'])} verified completed task copies ({result['freed_bytes'] / 1024**3:.2f} GiB logical)")
            except Exception as exc:
                self.state.storage_last = {"checked_at": now(), "error": str(exc)[:500], "freed_bytes": 0}
            try:   # every copy of a closed task, and old attempt/repair copies of open ones (backed up first)
                backup = self.cfg.task_backup_dir or str(self.state_dir / "task-backups")
                tasks_result = await asyncio.to_thread(storage.retire_task_checkouts, self.ctx.repo,
                    open_tasks=list(tasks), protected_paths=protected, backup_dir=backup,
                    closed_hours=0 if immediate else 1, attempt_hours=self.cfg.attempt_retention_hours,
                    running_tasks=set(self.active))
                __import__("rounds").write_file(str(self.state_dir / "task-retire.json"), tasks_result)
                self.task_retire_last = {**tasks_result, "retired": tasks_result.get("retired", [])[:10],
                                         "retired_count": len(tasks_result.get("retired", [])),
                                         "skipped": tasks_result.get("skipped", [])[:20]}
                if tasks_result.get("retired"):
                    self.ctx.log(f"Storage: removed {len(tasks_result['retired'])} finished or superseded task copies "
                                 f"({tasks_result['freed_bytes'] / 1024**3:.1f} GiB); branches kept, changes backed up")
                await asyncio.to_thread(storage.prune_backups, backup, self.cfg.task_backup_days)
            except Exception as exc:
                self.task_retire_last = {"checked_at": now(), "error": f"{type(exc).__name__}: {exc}"[:500]}
            try:   # dashboard: free space and the largest folders, refreshed at most every 6 hours (or when low)
                due = urgent or not self.disk_report or _report_age(self.disk_report) > 6 * 3600
                if due:
                    scan = self.cfg.disk_report_root or str(Path(self.state_dir).parent)
                    self.disk_report = await asyncio.to_thread(storage.disk_report, self.cfg.worktree_root, scan)
                    __import__("rounds").write_file(str(self.state_dir / "disk-report.json"), self.disk_report)
            except Exception as exc:
                self.ctx.log(f"Storage: disk report failed: {type(exc).__name__}: {exc}")
            try:   # a retired task checkout leaves its private Blender bridge behind: stop those, and only those
                reaped = await asyncio.to_thread(__import__("blender_bridge").reap)
                if reaped.get("stopped"):
                    self.ctx.log(f"Storage: stopped {len(reaped['stopped'])} Blender bridges of retired task copies")
            except Exception as exc:
                self.ctx.log(f"Storage: Blender bridge cleanup failed: {type(exc).__name__}: {exc}")
            self.state.save(self.state_dir / "state.json")
            self.wake()
        self._storage_job = asyncio.ensure_future(clean())

    # ---- merge rounds: keep ready PRs merging without anyone (rounds.py) ----
    def _merge_round(self, draining: bool) -> None:
        import rounds
        cfg, st = self.cfg, self.state
        self.plans.active_merge = None
        # A PC check (prcheck.py) runs no agent: it holds the build checkout, not worker capacity.
        self.merge_priority = self.round_job is not None and not self.round_info.get("check") and not self.round_info.get("build")
        if self._start_build(draining):
            return
        if not cfg.merge_rounds:
            self.merge_why = ""
            return
        # An active round owns its reservation even when future merges are held.
        # A deferred merge must not reserve worker capacity for housekeeping.
        if self.round_job is not None:
            self.merge_why = "a round is running"
            return
        if st.merge_held:
            self.merge_why = "merging is paused"
            return
        resume = st.merge_resume
        if resume and not resume.get("authorized"):
            self.merge_why = "Merge interrupted; saved progress waits for Resume / Merge now. " + str(resume.get("stop_reason", ""))[:300]
            return
        if not self.plans.merge_gate(draining):
            return
        if self._storage_job and not self._storage_job.done():
            self.merge_priority = bool(self.plans.active_merge) or not st.planned_integration
            self.merge_why = "Completed-task cleanup is finishing before the next merge"
            return
        if not self.dry_run and st.merge_last.get("result") in ("failed", "crashed", "checkout_failed") and not st.merge_last.get("failure_issue"):
            try:
                st.merge_last["failure_issue"] = rounds.report_failure(
                    self.ctx.gh, cfg, st.merge_last, st.merge_last.get("worktree") or str(rounds.checkout_path(self.ctx.repo)))
                st.save(self.state_dir / "state.json")
            except Exception as e:
                self.ctx.log(f"merge rounds: could not file prior merge failure: {type(e).__name__}: {e}")
        resume = st.merge_resume
        if resume and not resume.get("authorized"):
            self.merge_priority = False
            self.merge_why = "Merge interrupted; saved progress waits for Resume / Merge now. " + str(resume.get("stop_reason", ""))[:300]
            return
        wait_for = resume.get("wait_for") if resume else None
        if wait_for:
            from recovery import handoff_status, release_handoff
            ready, reason = handoff_status(self.state_dir, wait_for)
            st.save(self.state_dir / "state.json")  # persist the deadline across restarts
            if not ready:
                self.merge_priority = False
                self.merge_why = reason
                return
            release_handoff(resume, reason)
            if reason:
                self.ctx.log(reason)
            st.save(self.state_dir / "state.json")
        if self.round_job is not None:
            self.merge_why = "a round is running"
            return
        if st.spent.get(today(), 0.0) >= cfg.daily_budget_usd:
            self.merge_why = "daily budget spent"
            return
        repository_closed: set[tuple[str, int]] = set()
        try:
            ready = rounds.ready_prs(self.ctx.gh.labelled_prs(cfg.merge_label or "ready", cfg.base_branch), cfg)
            labelled = list(ready)
            # External ready PRs need not carry the harness task label and thus
            # may be absent from open_prs(). Verify their own dependency refs too.
            known_closed = getattr(self, "_known_closed_dependencies", set())
            refs = {d for p in ready for d in Task(0, "", p.body).depends_on}
            unresolved = refs - self._tasks.keys() - {p.number for p in ready} - known_closed
            lookup = getattr(self, "_dependency_lookup", self.ctx.gh.issue_closed)
            for dependency in sorted(unresolved):
                try:
                    if lookup(dependency):
                        known_closed.add(dependency)
                except Exception as e:
                    self.dispatch_warning = f"GitHub could not verify prerequisite #{dependency}"
                    self.ctx.log(f"#{dependency}: merge prerequisite lookup deferred: {e}")
            self._known_closed_dependencies = known_closed
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
            for p in ([] if st.merge_resume else self._conflicting(ready)):
                # Caught before the round, not inside it: the PR's fixer (or its repair task) merges the base in.
                st.merge_excluded[str(p.number)] = {
                    "head": p.head_sha, "round": 0, "at": now(), "disposition": "failed", "conflict": True,
                    "why": (f"PR #{p.number} conflicts with {cfg.base_branch}: merge the latest {cfg.base_branch} into its "
                            "branch and resolve the conflicts. Merge rounds leave it out until its head changes.")}
                self.ctx.log(f"merge rounds: #{p.number} conflicts with {cfg.base_branch}; left out until its head changes")
                st.save(self.state_dir / "state.json")
            declarations = [*self._tasks.values(), *(Task(p.number, p.title, p.body) for p in ready)]
            verify = getattr(self, "_verify_repository_dependencies", None)
            repository_closed = verify(declarations) if verify else set()
            self._repository_closed = repository_closed
            ready = rounds.without_failed_dependencies(ready, st.merge_excluded, self._tasks, known_closed, repository_closed)
        except Exception as e:  # GitHub down: try again next pass
            self.merge_why = f"couldn't list the PRs: {type(e).__name__}"
            self.ctx.log(f"merge rounds: {type(e).__name__}: {e}")
            return
        ready = self.plans.select(ready)
        if self.plans.active_merge and not ready:
            return
        if not resume and not self.plans.active_merge:
            # A PR deferred twice at one head (acceptance this PC cannot run) stops taking a slot until it changes;
            # the rest go most-unblocking first, so each round sets the most queued work moving.
            if cfg.merge_playtest:
                back = rounds.unpark_people_only(st.merge_parked)
                if back:
                    self.ctx.log("merge rounds: playtest acceptance is on; unparked " + ", ".join(f"#{x}" for x in back)
                                 + " (their remaining checks need people and go on the playtest checklist)")
                    st.save(self.state_dir / "state.json")
            ready = rounds.by_value(rounds.not_parked(ready, st.merge_parked), self._tasks)
            if st.merge_base_red and not st.merge_split.get("groups"):
                # The base branch itself fails (rounds 160 and 162 on 2026-11-13): testing feature PRs on it only fails
                # them all. Rounds take the PRs labelled merge-next (the fix) until a round lands or the hold expires.
                gate, why = rounds.base_red_gate(st.merge_base_red, ready, cfg.base_branch)
                if why:
                    if not gate:
                        self.merge_why = why
                        return
                else:
                    st.merge_base_red = {}
                    st.save(self.state_dir / "state.json")
                ready = gate
        if resume:
            resume = self._reconcile_resume(resume, ready)
        if resume:
            wanted = {int(n) for n in resume.get("prs", {})}
            ready = [p for p in ready if p.number in wanted]
            if {p.number for p in ready} != wanted:
                self.merge_why = "Saved merge candidate waits for its recorded PRs/dependencies to be eligible; preserved progress is unchanged"
                return
            if any(p.head_sha != resume["prs"].get(str(p.number)) for p in ready):
                self.merge_why = "Saved candidate PR heads changed; reconcile before resuming validation"
                return
        ok, why = rounds.should_start(cfg, st.merge_last, st.merge_held, draining, False, ready)
        if not ready and labelled and why.startswith("no PR labelled"):
            # The label is there; every PR carrying it is left out at its head (repair pending or spent) or parked.
            why = (f"{len(labelled)} PR(s) labelled `{cfg.merge_label or 'ready'}`, but none can join a round: each was left "
                   "out at its current head (repair pending, or needs a person) or is parked until it changes")
            self._starved(labelled, repository_closed, resume)
        self.merge_why = why
        if not ok:
            # Rounds come first on the one PC: a PC check of a single PR only starts when no PR can join a round now,
            # so a check never takes a waiting round's turn (11-10: checks of #1506 kept round 98 waiting).
            if not ready and cfg.pr_check_command and not draining and not st.merge_resume and not self.dry_run:
                self._start_pr_check()
            return
        limit = len(ready) if resume else max(1, int(cfg.merge_round_max or 12))
        batch = []
        split = []
        if not resume and st.merge_split.get("groups"):
            # A failed batch is being tried in parts: its next part goes before anything else (rounds.after_round).
            split = rounds.split_batch(st.merge_split, ready)
            st.save(self.state_dir / "state.json")
        for offset in range(0, 0 if split else len(ready), limit):
            # The same verified cross-repo prerequisites as the eligibility filter above: without them every PR with a
            # "Depends on: owner/repo#n" line was dropped here although that issue had closed (11-14: #1568 on Hearth#42
            # held rounds 180+ with the backend healthy and nothing logged).
            batch = rounds.without_failed_dependencies(ready[offset:offset + limit], st.merge_excluded, self._tasks,
                                                       getattr(self, "_known_closed_dependencies", set()), repository_closed)
            if batch:
                break
        batch = split or batch
        if not batch:
            self.merge_why = "ready PRs wait for prerequisite PRs to become eligible"
            self._starved(labelled, repository_closed, resume)
            return
        if st.merge_starved.get("since"):
            import watchdog
            watchdog.resumed(self)
            st.save(self.state_dir / "state.json")
        self.merge_priority = True
        pool = self.ctx.agents
        agent = pool.merger(cfg.merge_agent) if pool is not None else ""
        if pool is not None and not agent:
            # Reserve the next slot for a busy merger, but let unrelated workers
            # proceed when all merger agents are off, signed out or rate-limited.
            self.merge_priority = any(s.kind in ("claude", "codex", "opencode", "fake")
                                      and pool.available(s.name, "merger", ignore_capacity=True)
                                      for s in pool.specs.values())
            if self.merge_priority and self.plans.drain():
                return
            self.merge_why = "no agent can run a round now (enable a signed-in agent with the merger role that is not at a limit)"
            return
        if self.plans.drain():
            return
        wt_root = getattr(self.ctx.repo, "worktree_root", None)
        if wt_root and not self.dry_run:
            try:
                node_why = "" if resume else rounds.node_round(cfg, batch, getattr(self, "machines", None), self._tasks)
                rounds.checkout_guard(Path(resume["checkpoint"]["worktree"]) if resume else rounds.checkout_path(self.ctx.repo),
                                      "" if node_why else cfg.heavy_lease_file)  # a node round never needs this PC's lease
            except RuntimeError as e:
                self.merge_why = str(e)
                self._note_checkout_stall(str(e))
                if self._checkout_stall["loud"]:
                    self.merge_priority = False  # a stuck checkout must not also stop every worker
                return
        self._checkout_stall = None
        round_file = cfg.merge_round_file or str(self.state_dir / "merge-round.json")
        try:
            last_no = int(str(rounds.read_file(round_file).get("round", "0")).strip() or 0)
        except ValueError:
            last_no = 0
        n = int(resume["round"]) if resume else max(st.merge_round_no, last_no) + 1
        self.last_plan.append(f"merge round {n}: " + ", ".join(f"#{p.number}" for p in batch) + (f" on {agent}" if agent else ""))
        if self.dry_run:
            return
        st.merge_round_no = max(st.merge_round_no, n)
        st.save(self.state_dir / "state.json")
        request_id = self.plans.started(n)
        self.round_info = {"request_id": request_id, "round": n, "agent": agent or "claude", "prs": [p.number for p in batch], "started": now()}
        if split:
            self.round_info["split"] = st.merge_split.get("from_round")
        self.merge_why = "a round is running"
        self.live_round = n
        self.round_job = asyncio.ensure_future(self._run_round(n, batch, agent, rounds.key_of(ready), round_file, request_id,
                                                               in_split=bool(split)))
        def _ended(_f, n=n):
            self.ended_rounds.add(n)
            if self.live_round == n:
                self.live_round = None
        self.round_job.add_done_callback(_ended)
        self.ctx.log(f"merge round {n} started on {agent or 'claude'}: " + ", ".join(f"#{p.number}" for p in batch)
                     + (f" (part of round {st.merge_split.get('from_round')}'s failed batch)" if split else ""))

    async def _run_round(self, n: int, batch: list, agent: str, key: str, round_file: str, request_id: str = "",
                         in_split: bool = False) -> dict:
        import clis
        import rounds
        import rules as rules_mod
        from pipeline import _LimitGuard, Limited
        from runtime import role_prompt
        cfg, gh, pool = self.cfg, self.ctx.gh, self.ctx.agents
        resume = dict(self.state.merge_resume) if self.state.merge_resume.get("authorized") else {}
        branch = resume["checkpoint"]["branch"] if resume else f"fletchery/merge-round-{n}-{datetime.now(timezone.utc).strftime('%m%d%H%M')}"
        started = now()
        res = {"round": n, "result": "failed", "at": started, "started": started,
               "key": key, "summary": "", "pr": 0, "agent": agent}
        node_why = "" if resume else rounds.node_round(cfg, batch, getattr(self, "machines", None), self._tasks)
        if node_why:
            self.ctx.log(f"merge round {n}: {node_why}; this PC only merges")
            if isinstance(self.round_info, dict):
                self.round_info["node"] = node_why
        if pool is not None and agent:
            pool.acquire(agent)
        try:
            if resume:
                from recovery import resume_worktree
                wt = await asyncio.to_thread(resume_worktree, self.ctx.repo, resume["checkpoint"], cfg.heavy_lease_file)
                progress = dict(resume.get("progress") or rounds.read_file(round_file))
                progress.update(round=str(n), branch=branch, worktree=str(wt), state="running", result="", ended="",
                                updated=now(), owner=f"Fletchery ({agent or 'claude'})",
                                note="Resuming saved candidate; verify fingerprints and continue unfinished acceptance.")
                rounds.write_file(round_file, progress)
            else:
                rounds.start_file(round_file, n, branch, str(rounds.checkout_path(self.ctx.repo)), agent or "claude", batch, cfg.base_branch)
                wt = await asyncio.to_thread(rounds.prepare_worktree, self.ctx.repo, branch, "" if node_why else cfg.heavy_lease_file)
                rounds.start_file(round_file, n, branch, str(wt), agent or "claude", batch, cfg.base_branch)
            res["worktree"] = str(wt)
            rules = rules_mod.load(wt, cfg.project_root)
            flags_cfg = [f for f in (cfg.merge_flags or []) if f.get("paths") and f.get("flag")]

            # One file read per PR, outside the event loop: slow GitHub metadata
            # must not freeze worker output, completion handling or dashboard controls.
            files_by_pr = {}
            for p in batch:
                files_by_pr[p.number] = await asyncio.to_thread(gh.pr_files, p.number)
                self.__dict__.setdefault("_merge_files", {})[(p.number, p.head_sha)] = files_by_pr[p.number]

            def flags_for(p):
                files = files_by_pr[p.number]
                return [f["flag"] for f in flags_cfg if any(re.search(f["paths"], x) for x in files)]
            changed = sorted({f for files in files_by_pr.values() for f in files})
            validation = rounds.validation_plan(changed, cfg.base_branch)
            prompt = role_prompt("merger", rules, project=cfg.name, base=cfg.base_branch, branch=branch, round=str(n),
                                 label=cfg.merge_label or "ready", prs=rounds.pr_list_text(batch, flags_for),
                                 round_file=round_file, validation=validation, additions=rounds.notes_text(rules, cfg) or "(none: use the "
                                 "repository's own docs to find how it builds and tests)",
                                 freeze="# Code freeze\n\nThe project is in a code freeze: merge fixes only." if cfg.code_freeze else "")
            prompt += rounds.playtest_text(cfg)
            prompt += rounds.node_tests_text(cfg, n, cfg.base_branch, whole=bool(node_why))
            if in_split:
                prompt += rounds.split_text(self.state.merge_split, batch, cfg.base_branch)
            if resume:
                prompt += ("\n\n## Explicitly authorized interrupted-round recovery\n"
                           "The user authorized continuing final acceptance after the PC reservation clears. "
                           "Work IN THIS SAVED CHECKOUT on its existing branch; do not reset, clean, prune, recreate the batch or remerge unchanged PRs. "
                           "Read the recorded progress and Saved/MergeRound" + str(n) + " receipts first. "
                           "Verify source/content/external-input and binary fingerprints before reusing successful checks. "
                           "Only rerun checks invalidated by an actual input change; record the reason. Compare current PR heads and base against the checkpoint; "
                           "reconcile differences safely and revalidate affected checks, rather than certifying mismatched evidence. "
                           "Keep unverified PRs deferred, never broken. Complete the remaining named visual/runtime and packaged checks, then land. "
                           "A new physical Escape/user stop revokes this continuation and must return interrupted with an updated checkpoint.\n"
                           + json.dumps(resume, indent=2))
            rt = pool.runtime(agent) if pool is not None and agent else self.ctx.runtime
            guard = _LimitGuard(rt, self.ctx, 0, agent or "")
            r = await guard.run("merger", prompt, wt, cfg.merge_budget_usd, cfg.merge_max_turns)
            self._spend(agent or "claude", r.cost_usd)
            for _ in range(2):  # stopped part way with a progress note: carry on in the same session
                cut = "no JSON answer" in (r.error or "") or rounds.output_cut_short(r.error or "")
                if r.ok or rounds.operator_interruption(r.error or "") or not r.session_id or not cut or not hasattr(rt, "command"):
                    break
                self.ctx.log(f"merge round {n}: {agent} stopped before the end; asking it to carry on")
                rt.resume_session = r.session_id
                try:
                    r = await guard.run("merger", prompt, wt, cfg.merge_budget_usd, cfg.merge_max_turns)
                finally:
                    rt.resume_session = ""
                self._spend(agent or "claude", r.cost_usd)
            out = r.output or {}
            res["result"] = rounds.result_of(out) if r.ok else rounds.session_stop_result(r.error or "")
            res["stop_reason"] = str(out.get("stop_reason") or out.get("gave_up") or r.error or "")[:7000]
            res["pr"] = int(out.get("landed_pr") or 0)
            res["summary"] = (out.get("summary") or out.get("gave_up") or r.error or "")[:600]
            res["merged"] = [int(x) for x in out.get("merged") or [] if str(x).isdigit()]
            res["playtest"] = [item for item in out.get("playtest") or [] if isinstance(item, dict)]
            left_out = [item for item in out.get("left_out") or [] if isinstance(item, dict)]
            res["deferred"] = [item for item in left_out if rounds.deferred(item)] + list(out.get("deferred") or [])
            res["left_out"] = [item for item in left_out if not rounds.deferred(item)]
            if res["result"] == "interrupted":
                progress = rounds.read_file(round_file)
                checkpoint = out.get("checkpoint") or {"worktree": str(wt), "branch": branch, "head": ""}
                res["resume_authorized"] = not rounds.operator_interruption(res["stop_reason"])
                self.state.merge_resume = {"round": n, "authorized": res["resume_authorized"], "stop_reason": res["stop_reason"],
                    "checkpoint": checkpoint, "prs": {str(p.number): p.head_sha for p in batch
                        if p.number not in {int(item["pr"]) for item in res["left_out"] if str(item.get("pr", "")).isdigit()}},
                    "progress": progress}
                res["checkpoint"] = checkpoint
            if not r.ok and clis.unreachable(r.error or "") and pool is not None and agent:
                # the model itself can't be reached: not this round's fault. Take the agent out until its next
                # sign-in check and run the round again on the next agent, without waiting out mergeRetryMinutes.
                res["result"] = "unreachable"
                pool.pause(agent, 360, f"model unreachable: {(r.error or '')[:160]}", shared=False)
                pool.accounts.setdefault(agent, {})["ok"] = False
        except rounds.CheckoutBusy as e:
            # A reservation can appear while fetch is running. This is a normal
            # wait, not a broken PR/harness or a 45-minute failed-round backoff.
            res.update(result="waiting", summary=str(e), key="")
        except rounds.CheckoutRecovery as e:
            rounds.start_file(round_file, n, branch, str(e.path), agent or "claude", batch, cfg.base_branch)
            res.update(result="checkout_failed", summary=str(e), key="", worktree=str(e.path), next_worktree=str(e.next_path))
        except Limited as e:
            res["result"], res["summary"] = "limited", f"{agent} hit a limit: {e}"
            if pool is not None and agent:
                pool.pause(agent, limit_pause_minutes(str(e), self.cfg.limit_backoff_minutes), str(e))
        except Exception as e:  # a crash in a round must never stop the loop
            res["result"], res["summary"] = "crashed", f"{type(e).__name__}: {e}"
        finally:
            if pool is not None and agent:
                pool.release(agent)
            if res.get("worktree"):
                try:  # a session's `tail -f` on its own round log outlived round 38 by 3 h (2026-11-07)
                    left = await asyncio.to_thread(rounds.stop_log_followers, res["worktree"])
                    if left:
                        self.ctx.log(f"merge round {n}: stopped leftover log followers: {', '.join(left)}")
                except Exception as e:
                    self.ctx.log(f"merge round {n}: leftover process check failed: {type(e).__name__}: {e}")
        # History and retry cooldowns use completion time, even for long rounds.
        res["at"] = res["ended"] = now()
        rounds.finish_file(round_file, res["result"],
                           (res.get("stop_reason", "") + " " + res["summary"]) if res["result"] == "interrupted" else
                           (f"Landed as #{res['pr']}. " if res["pr"] else "") + res["summary"])
        if res["result"] in ("unreachable", "checkout_failed", "limited"):
            res["key"] = ""  # not the PRs' fault: try the same PRs again at once on another agent
            self.wake()
        for item in res.get("left_out", []):
            if not isinstance(item, dict):
                continue
            p = next((p for p in batch if str(p.number) == str(item.get("pr"))), None)
            if p is not None:
                self.state.merge_excluded[str(p.number)] = {"head": p.head_sha, "why": str(item.get("why", ""))[:4000], "round": n, "at": now()}
        self._after_split(n, batch, res, in_split)
        if res["result"] == "landed":
            self._storage_after_round = True  # finished tasks and the old merge checkout are now cleanable
        if res["result"] == "landed" and res.get("playtest"):
            try:
                self._record_playtest(n, res)
            except Exception as e:
                self.ctx.log(f"merge round {n}: could not update the playtest checklist: {type(e).__name__}: {e}")
        base_red = rounds.base_is_red(res)
        parked = rounds.record_deferrals(self.state.merge_parked, batch, res.get("deferred", []), n, res["at"],
                                         base_red=base_red)
        if base_red:
            self.state.merge_base_red = {"round": n, "why": str(res.get("stop_reason") or "")[:2000], "at": res["at"]}
            self.ctx.log(f"merge round {n}: {self.cfg.base_branch} itself fails; rounds now take only PRs labelled "
                         f"{rounds.PRIORITY_LABEL} until one lands")
        elif res["result"] == "landed":
            self.state.merge_base_red = {}
        if parked:
            self.ctx.log(f"merge round {n}: parked " + ", ".join(f"#{x}" for x in parked)
                         + f" (deferred {rounds.PARK_AFTER} times at the same head; a new commit or {rounds.PARK_HOURS} h unparks)")
        if resume and res["result"] in ("landed", "nothing"):
            self.state.merge_resume = {}
        elif resume and res["result"] in ("failed", "crashed", "checkout_failed"):
            self.state.merge_resume["authorized"] = False
            self.state.merge_resume["stop_reason"] = res.get("stop_reason") or res["summary"]
        if request_id:
            res["request_id"] = request_id
        self.state.merge_last = res
        self.state.save(self.state_dir / "state.json")
        if res["result"] in ("failed", "crashed", "checkout_failed"):
            try:
                res["failure_issue"] = rounds.report_failure(gh, cfg, res, res.get("worktree") or str(rounds.checkout_path(self.ctx.repo)))
                self.state.save(self.state_dir / "state.json")
            except Exception as e:
                self.ctx.log(f"merge round {n}: could not file repair issue: {type(e).__name__}: {e}")
        try:  # a round that lands nothing, a red base or a failure seen before: say so where people look
            alert = rounds.round_alert(res, batch, self.cfg.base_branch)
            if alert:
                rounds.raise_alert(self.ctx.gh, self.state_dir, res, alert)
                self.ctx.log(f"merge round {n}: alert: {alert}")
        except Exception as e:
            self.ctx.log(f"merge round {n}: could not raise the alert: {type(e).__name__}: {e}")
        if request_id:
            self.plans.finish_merge(request_id, res)
        self.ctx.log(f"merge round {n}: {res['result']}" + (f", landed as PR #{res['pr']}" if res["pr"] else "")
                     + (f" ({res['summary'][:200]})" if res["summary"] else ""))
        return res

    def _after_split(self, n: int, batch: list, res: dict, in_split: bool) -> None:
        """A failed round of several PRs that named no culprit is tried again in halves; a PR that fails on its own is
        left out (rounds.after_round). The next part starts at once, without the failed-round wait."""
        import rounds
        known = getattr(self, "_known_closed_dependencies", set())
        split, culprits = rounds.after_round(self.state.merge_split, batch, res, in_split,
                                             lambda prs: rounds.split_halves(prs, self._tasks, known,
                                                                               getattr(self, "_repository_closed", set())))
        heads = {p.number: p.head_sha for p in batch}
        for number, why in culprits:
            self.state.merge_excluded[str(number)] = {"head": heads[number], "why": why[:4000], "round": n, "at": now(),
                                                      "disposition": "failed"}
            res.setdefault("left_out", []).append({"pr": number, "why": why[:4000], "disposition": "failed"})
        if culprits:
            self.ctx.log(f"merge round {n}: left out " + ", ".join(f"#{x}" for x, _ in culprits)
                         + " (failed on its own after the batch was split)")
        started = split.get("groups") and split != self.state.merge_split
        self.state.merge_split = split
        if split.get("groups"):
            res["key"] = ""  # the next part is a different candidate: no failed-round wait
            if started:
                parts = " then ".join("+".join(f"#{x['pr']}" for x in g) for g in split["groups"])
                self.ctx.log(f"merge round {n}: {res['result']} with no PR named; trying the batch in parts: {parts}")
            self.wake()

    def _starved(self, labelled: list, repository_closed: set, resume) -> None:
        """Ready PRs exist but none can join a round (watchdog.py): recheck, retry, and alert if that changes nothing."""
        if resume or self.dry_run or not labelled:
            return
        import watchdog
        pending = {p.number: {f"{repo}#{n}" for repo, n in Task(p.number, p.title, p.body).repository_dependencies
                              - repository_closed} for p in labelled}
        try:
            freed = watchdog.starved(self, labelled, {k: v for k, v in pending.items() if v})
        except Exception as e:  # the watchdog must never stop the pass
            self.ctx.log(f"merge rounds: starve check failed: {type(e).__name__}: {e}")
            return
        self.state.save(self.state_dir / "state.json")
        if freed:
            self.wake()

    def _conflicting(self, ready: list) -> list:
        """Ready PRs that GitHub says conflict with the base branch (the PR list never says; one PR read each, cached
        per head for 10 minutes). A failed lookup counts as no conflict: the round still resolves what it can."""
        lookup = getattr(self.ctx.gh, "pr_conflicts", None)
        if lookup is None or self.dry_run:
            return []
        cache = self.__dict__.setdefault("_conflict_cache", {})
        out, clock = [], time.monotonic()
        excluded = self.state.merge_excluded
        for p in ready:
            if (excluded.get(str(p.number)) or {}).get("head") == p.head_sha:
                continue  # already left out at this head: no lookup, and no fresh "at" that would re-send its feedback
            hit = cache.get((p.number, p.head_sha))
            if hit is None or hit[0] < clock:
                try:
                    hit = (clock + 600, lookup(p.number))
                except Exception as e:
                    self.ctx.log(f"PR #{p.number}: conflict check deferred: {type(e).__name__}: {e}")
                    continue
                cache[(p.number, p.head_sha)] = hit
            if hit[1] is True:
                out.append(p)
        return out

    def _spend(self, agent: str, usd: float) -> None:
        if not usd:
            return
        st = self.state
        st.spent[today()] = round(st.spent.get(today(), 0.0) + usd, 4)
        day = st.spent_by_agent.setdefault(today(), {})
        day[agent] = round(day.get(agent, 0.0) + usd, 4)

    def recover_interrupted(self, data: dict) -> dict:
        """Adopt an older saved round after explicit operator recovery authorization."""
        import rounds
        from recovery import reconcile_landed_round, resume_worktree, reconcile_runtime_resume
        if data.get("reconcile_landed") is True:
            return reconcile_landed_round(self, data)
        if data.get("reconcile_runtime_failure") is True:
            return reconcile_runtime_resume(self, data)
        last = self.state.merge_last
        if self.round_job is not None or int(data.get("round", -1)) != int(last.get("round", -2)):
            raise ValueError("Recovery must name the current stopped round and cannot replace an active merge")
        reason = str(data.get("stop_reason", ""))
        if not rounds.operator_interruption(reason):
            raise ValueError("Recovery requires the actual recorded operator interruption reason")
        checkpoint = data.get("checkpoint") or {}
        resume_worktree(self.ctx.repo, checkpoint)  # read-only metadata; heavy work still waits for the lease
        deferred = list({int(item["pr"]): item for item in list(last.get("left_out", [])) + list(last.get("deferred", []))
                         if rounds.deferred(item)}.values())
        numbers = {int(item["pr"]) for item in deferred}
        heads = {str(n): str(h) for n, h in (data.get("prs") or {}).items()}
        if not numbers or {int(n) for n in heads} != numbers or any(not re.fullmatch(r"[a-f0-9]{40}", h) for h in heads.values()):
            raise ValueError("Recovery PR heads must match exactly the stopped round's deferred PRs")
        if any(item.get("head") and item["head"] != heads[str(item["pr"])] for item in deferred):
            raise ValueError("Recovery heads differ from the recorded deferred heads")
        progress = data.get("progress") or {}
        if str(progress.get("round")) != str(last["round"]) or progress.get("branch") != checkpoint["branch"]:
            raise ValueError("Recovery progress does not match the stopped round and branch")
        wait_for = data.get("wait_for") or {}
        if wait_for and not re.fullmatch(r"[a-f0-9]{32}", str(wait_for.get("request_id", ""))):
            raise ValueError("External acceptance needs a unique recovery request id")
        if wait_for:
            wait_for = {**wait_for, "started_at": datetime.now(timezone.utc).isoformat()}
        last.update(result="interrupted", stop_reason=reason, resume_authorized=True, key="", checkpoint=checkpoint,
                    deferred=deferred, left_out=[item for item in last.get("left_out", []) if not rounds.deferred(item)])
        self.state.merge_resume = {"round": last["round"], "authorized": True, "stop_reason": reason,
                                  "checkpoint": checkpoint, "prs": heads, "progress": progress, "wait_for": wait_for}
        for number in numbers:
            old = self.state.merge_excluded.get(str(number))
            if old and rounds.deferred(old):
                del self.state.merge_excluded[str(number)]
        self.state.save(self.state_dir / "state.json")
        self.wake()
        return {"round": last["round"], "resume_authorized": True, "message": "Saved round queued; heavy work waits for the PC reservation."}

    STALL_MINUTES = 30

    def _note_checkout_stall(self, why: str) -> None:
        """A due round blocked on its checkout also holds the workers' slot, so a stuck owner stops all work. After
        STALL_MINUTES say so loudly: log it once and put it at the top of the status issue's Merge section."""
        since = (getattr(self, "_checkout_stall", None) or {}).get("since") or now()
        minutes = (datetime.now(timezone.utc) - datetime.fromisoformat(since.replace("Z", "+00:00"))).total_seconds() / 60
        stall = {"since": since, "why": why[:400], "minutes": int(minutes),
                 "loud": minutes >= self.STALL_MINUTES}
        if stall["loud"] and not (getattr(self, "_checkout_stall", None) or {}).get("loud"):
            self.ctx.log(f"merge rounds: STALLED {int(minutes)} min, and workers wait too: {why[:300]}. "
                         "End the named process if it is a leftover, or pause merging to free the workers.")
        self._checkout_stall = stall

    def _start_build(self, draining: bool) -> bool:
        """Start a scheduled build that is waiting for this PC (builds.py) in the merge slot. A running round or PC
        check finishes first; a due build then goes before the next round, since Alex asked for it at that time.
        True when one started (or the slot is taken)."""
        if self.round_job is not None:
            return False
        st = self.state
        if self.dry_run or draining or st.held or st.merge_resume or (self._storage_job and not self._storage_job.done()):
            return False
        if "builds" in st.pc_paused:
            return False
        row = self.builds.next_pc()
        if row is None:
            return False
        self.merge_why = f"Build {row['version']} is using this PC; merging waits"
        self.round_info = {"build": row["id"], "round": f"Build {row['version']}", "agent": "", "prs": [],
                           "head": row["sha"], "started": now()}
        self.round_job = asyncio.ensure_future(asyncio.to_thread(self.builds.run_pc, row))
        self.ctx.log(f"builds: {row['version']} ({row['ref']} at {row['sha'][:10]}) starting on this PC")
        return True

    def _start_pr_check(self) -> bool:
        """Start one PC check (prcheck.py) in the merge slot. The caller only asks when no PR can join a round, so
        checks use the PC's idle time and never delay merging. True when a check started."""
        import graph
        import nodecheck
        import prcheck
        cfg, gh = self.cfg, self.ctx.gh
        if "checks" in self.state.pc_paused:
            return False
        try:
            if nodecheck.node_takes_checks(getattr(self, "machines", None)):
                return False  # a test node checks PRs now (nodecheck.py); the PC keeps its idle time for rounds
        except Exception as e:
            self.ctx.log(f"node PR checks: {type(e).__name__}: {e}")
        label = cfg.merge_label or "ready"
        try:
            waiting = prcheck.candidates(gh.open_prs(), cfg, self.state.pr_checks, label, graph.PREFIX)
        except Exception as e:
            self.ctx.log(f"PC check: couldn't list PRs: {type(e).__name__}: {e}")
            return False
        if not waiting:
            return False
        pr = waiting[0]
        self.merge_why = f"PC check of PR #{pr.number} ({len(waiting)} waiting)"
        self.round_info = {"check": pr.number, "round": f"PC check #{pr.number}", "agent": "", "prs": [pr.number],
                           "head": pr.head_sha, "started": now(), "waiting": len(waiting)}
        self.round_job = asyncio.ensure_future(self._run_pr_check(pr))
        self.ctx.log(f"PC check: PR #{pr.number} at {pr.head_sha[:10]} ({len(waiting) - 1} more waiting)")
        return True

    async def _run_pr_check(self, pr) -> None:
        import prcheck
        cfg, gh = self.cfg, self.ctx.gh
        try:
            res = await asyncio.to_thread(prcheck.run, self.ctx.repo, pr, cfg)
        except Exception as e:  # a crash in a check must never stop the loop
            res = prcheck.CheckResult(prcheck.NO_RESULT, f"{type(e).__name__}: {e}")
        if res.code == prcheck.NO_RESULT:
            self.ctx.log(f"PC check: PR #{pr.number}: no result ({res.output.splitlines()[0] if res.output else ''}); "
                         "it runs again later")
            return
        passed = res.code == 0
        self.state.pr_checks[str(pr.number)] = {"head": pr.head_sha, "passed": passed, "at": now(),
                                                "output": res.output[-2000:]}
        self.state.save(self.state_dir / "state.json")
        try:
            if passed:
                gh.add_labels(pr.number, [cfg.merge_label or "ready"])
                gh.comment(pr.number, f"Fletchery: PC check passed for head `{pr.head_sha[:10]}` (merged onto "
                                      f"`{cfg.base_branch}`, built, quick-tested). Labelled for the next merge round.")
            else:
                gh.comment(pr.number, prcheck.failure_comment(pr, res, cfg.base_branch))
        except Exception as e:
            self.ctx.log(f"PC check: PR #{pr.number}: couldn't update the PR: {type(e).__name__}: {e}")
        self.ctx.log(f"PC check: PR #{pr.number} {'passed' if passed else 'failed'}")

    def _maybe_node_pr_checks(self, draining: bool) -> None:
        """Every minute, off the loop: act on finished node PR checks and queue the next one (nodecheck.py)."""
        import time
        import nodecheck
        coord = getattr(self, "machines", None)
        if self.dry_run or not nodecheck.conf_of(coord) or not self.cfg.pr_check_command:
            return
        job = getattr(self, "_node_check_job", None)
        if (job is not None and not job.done()) or time.monotonic() < getattr(self, "_node_check_at", 0):
            return
        self._node_check_at = time.monotonic() + 60
        self._node_check_job = asyncio.ensure_future(asyncio.to_thread(self._node_pr_check_step, coord, draining))

    def _node_pr_check_step(self, coord, draining: bool) -> None:
        import graph
        import nodecheck
        import prcheck
        cfg, gh, m = self.cfg, self.ctx.gh, coord.machines
        label = cfg.merge_label or "ready"
        try:
            for job in m.unhandled(nodecheck.RECIPE):
                self._node_pr_check_result(coord, job)
            conf = nodecheck.conf_of(coord)
            if not nodecheck.eligible(coord, conf):
                # No node can take checks now (PR checks paused on it, or it is offline): give back the queued one so
                # the PC can check PRs instead of waiting on a node that will not claim it.
                for j in m.active(nodecheck.RECIPE):
                    if j["state"] == "queued":
                        m.cancel(j["id"])
                return
            if draining or m.active(nodecheck.RECIPE):
                return
            # Shadow: check the same PRs the PC does, to compare. Live: skip heads the PC already checked.
            checked = {} if m.shadow else self.state.pr_checks
            waiting = prcheck.candidates(gh.open_prs(), cfg, checked, label, graph.PREFIX)
            pr = nodecheck.pick(waiting, m.active(nodecheck.RECIPE), self.state.node_pr_checks)
            if not pr:
                return
            base = coord.resolve(cfg.base_branch)
            job_id = m.enqueue(nodecheck.RECIPE, base, {"pr": pr.number, "head": pr.head_sha}, ref=cfg.base_branch,
                               requested_by="PR check", labels=nodecheck.need_labels(conf), pr=pr.number)
            self.ctx.log(f"node PR check: PR #{pr.number} at {pr.head_sha[:10]} queued as job {job_id}"
                         f"{' (shadow)' if m.shadow else ''}")
        except Exception as e:  # never take the loop down
            self.ctx.log(f"node PR check: {type(e).__name__}: {e}")

    def _node_pr_check_result(self, coord, job: dict) -> None:
        import nodecheck
        import prcheck
        cfg, gh, m = self.cfg, self.ctx.gh, coord.machines
        m.mark_handled(job["id"])
        pr, head = int(job["params"].get("pr") or 0), str(job["params"].get("head") or "")
        v = nodecheck.verdict(job)
        where = job.get("machine") or "a test node"
        if v == "none":
            self.ctx.log(f"node PR check: PR #{pr} job {job['id']}: no result ({job.get('summary')}); it runs again later")
            return
        passed = v == "pass"
        step = nodecheck.MERGE_STEP if str(job.get("summary") or "").startswith(nodecheck.MERGE_STEP) else nodecheck.CHECK_STEP
        output = "" if passed else nodecheck.log_tail(m.log_text(job["id"]), step)
        self.state.node_pr_checks[str(pr)] = {"head": head, "passed": passed, "at": now(), "job": job["id"],
                                              "machine": where, "summary": job.get("summary") or ""}
        if m.shadow:
            pc = self.state.pr_checks.get(str(pr)) or {}
            same = f"; the PC said {'pass' if pc.get('passed') else 'fail'}" if pc.get("head") == head else ""
            self.ctx.log(f"node PR check (shadow): PR #{pr} {'passed' if passed else 'failed'} on {where}{same}")
            self.state.save(self.state_dir / "state.json")
            return
        live = gh.api(f"repos/{cfg.repo}/pulls/{pr}", fresh=True) or {}
        if live.get("state") != "open" or (live.get("head") or {}).get("sha") != head:
            self.ctx.log(f"node PR check: PR #{pr} moved on from {head[:10]}; result dropped")
            self.state.save(self.state_dir / "state.json")
            return
        self.state.pr_checks[str(pr)] = {"head": head, "passed": passed, "at": now(), "output": output[-2000:],
                                         "machine": where}
        self.state.save(self.state_dir / "state.json")
        if passed:
            gh.add_labels(pr, [cfg.merge_label or "ready"])
            gh.comment(pr, f"Fletchery: PR check passed on {where} for head `{head[:10]}` (merged onto "
                           f"`{cfg.base_branch}`, built, quick-tested). Labelled for the next merge round.")
        else:
            res = prcheck.CheckResult(1, output or str(job.get("summary") or "failed"))
            gh.comment(pr, prcheck.failure_comment(SimpleNamespace(number=pr, head_sha=head), res, cfg.base_branch, where))
        self.ctx.log(f"node PR check: PR #{pr} {'passed' if passed else 'failed'} on {where}")

    PLAYTEST_MARKER = "<!-- fletchery-playtest-checklist -->"

    def _record_playtest(self, round_no, res: dict) -> None:
        """Append the people-only checks of PRs that landed to the playtest checklist issue (made on first use)."""
        import rounds
        lines = rounds.playtest_lines(round_no, res.get("playtest"), res.get("merged"))
        if not lines:
            return
        gh = self.ctx.gh
        number = int(self.state.playtest_issue or 0)
        issue = gh.api(f"repos/{self.cfg.repo}/issues/{number}") if number else None
        if issue and issue.get("state") == "open" and self.PLAYTEST_MARKER in (issue.get("body") or ""):
            body = issue.get("body") or ""
            new = [x for x in lines if x not in body]
            if new:
                head, _, tail = body.partition(self.PLAYTEST_MARKER)
                gh.edit_body(number, head.rstrip() + "\n" + "\n".join(new) + "\n\n" + self.PLAYTEST_MARKER + tail)
        else:
            body = ("Checks that need people (two players, a second PC, a manual or visual review). Merge rounds landed "
                    "these PRs after every automated check passed; tick each one off in a playtest, or open a bug.\n\n"
                    + "\n".join(lines) + "\n\n" + self.PLAYTEST_MARKER + "\n")
            number = gh.create_issue(self.cfg.playtest_issue_title or "Playtest checklist", body, ["playtest"])
            self.state.playtest_issue = number
            self.state.save(self.state_dir / "state.json")
        self.ctx.log(f"merge round {round_no}: {len(lines)} playtest check(s) added to issue #{number}")

    def _reconcile_resume(self, resume: dict, ready: list) -> dict:
        """A saved round resumes on its own candidate PRs. Deferred PRs that have changed head since are left for the
        next round instead of blocking the resume. Once every candidate PR has merged or closed (e.g. the round was landed by
        hand), the saved round is dropped so a fresh round can start. A candidate PR whose head changed still blocks:
        its saved validation no longer applies, and an operator reconciles it."""
        saved = {str(k): str(v) for k, v in (resume.get("prs") or {}).items()}
        candidate = {str(n) for n in ((resume.get("progress") or {}).get("prs") or [])} & set(saved) or set(saved)
        live = {str(p.number): p.head_sha for p in ready}

        def gone(n: str) -> bool:
            try:
                return (self.ctx.gh.api(f"repos/{self.cfg.repo}/pulls/{int(n)}") or {}).get("state") != "open"
            except Exception:
                return False  # GitHub unsure: keep waiting rather than drop saved progress

        if candidate.isdisjoint(live) and all(gone(n) for n in candidate):
            stale = sorted(candidate, key=int)
            self.ctx.log(f"merge round {resume.get('round')}: saved candidate dropped; "
                         f"#{', #'.join(stale)} merged or closed since it was validated")
            self.state.merge_resume = {}
            self.state.save(self.state_dir / "state.json")
            return {}
        if any(n in live and live[n] != saved[n] for n in candidate):
            return resume  # blocks below: "Saved candidate PR heads changed"
        keep = {n: h for n, h in saved.items() if n in candidate or live.get(n) == h}
        if keep != saved:
            dropped = sorted(set(saved) - set(keep), key=int)
            self.ctx.log(f"merge round {resume.get('round')}: #{', #'.join(dropped)} changed since the round stopped; "
                         "they go to the next round and the saved candidate resumes without them")
            resume = {**resume, "prs": keep}
            self.state.merge_resume = resume
            self.state.save(self.state_dir / "state.json")
        return resume

    def merge_now(self) -> None:
        """Try the same PRs again now, without waiting out mergeRetryMinutes."""
        if self.state.merge_last:
            self.state.merge_last["key"] = ""
            self.state.merge_last["resume_authorized"] = True
        if self.state.merge_resume:
            self.state.merge_resume["authorized"] = True
            from recovery import release_handoff
            release_handoff(self.state.merge_resume,
                "Operator requested Merge now. External completion is unconfirmed; verify affected inputs and acceptance directly before landing.")
        self.state.save(self.state_dir / "state.json")
        self.wake()

    PC_KINDS = ("coding", "merging", "checks", "builds")

    def pause_pc(self, kind: str, on: bool) -> list:
        """Pause or resume one kind of work on this PC. Merging is the Merge section's Pause; the others start nothing
        new of that kind (running work finishes). Returns the kinds now paused."""
        if kind not in self.PC_KINDS:
            raise ValueError(f"Unknown kind of work {kind!r}; known: {', '.join(self.PC_KINDS)}")
        if kind == "merging":
            self.hold_merging(on)
        else:
            kinds = set(self.state.pc_paused)
            kinds = kinds | {kind} if on else kinds - {kind}
            self.state.pc_paused = [k for k in self.PC_KINDS if k in kinds]
            self.state.save(self.state_dir / "state.json")
            self.ctx.log(f"{kind} {'paused' if on else 'resumed'} on this PC from the app")
            self.wake()
        return self.pc_paused_view()

    def pc_paused_view(self) -> list:
        return [k for k in self.PC_KINDS if k in self.state.pc_paused or (k == "merging" and self.state.merge_held)]

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

    def _round_shares(self, pool) -> bool:
        """While a round runs with an agent pool, cap only the round agent's account; True when new work may start."""
        if pool is not None:
            pool.round_account = None
        if self.round_job is not None and (self.round_info.get("check") or self.round_info.get("build")):
            return True  # a PC check or build uses no agent account
        if self.round_job is None or pool is None or int(self.cfg.round_shared_sessions) < 0:
            return False
        spec = pool.specs.get(self.round_info.get("agent", ""))
        if spec is None:
            return False
        pool.round_account = (pool.account_of(spec), int(self.cfg.round_shared_sessions))
        return True

    def _backlog_marks(self) -> tuple[int, int]:
        size = max(1, int(self.cfg.merge_round_max or 12))
        high = int(self.cfg.merge_backlog_high or 0) or 2 * size
        low = int(self.cfg.merge_backlog_low or 0) or size
        return high, min(low, high)

    def _backlog_full(self, prs: dict) -> bool:
        """Backpressure with hysteresis: the workers' PRs labelled for merging pause new tasks at the high mark and
        let them start again at the low mark. Off when merge rounds are off: nothing would drain the backlog."""
        import rounds
        label = (self.cfg.merge_label or "ready").lower()
        excluded = self.state.merge_excluded
        # A PR left out at its current head waits for its repair, not for a round: it does not count.
        self.merge_backlog = sum(1 for p in prs.values() if p.base == self.cfg.base_branch
                                 and label in [l.lower() for l in p.labels] and not getattr(p, "draft", False)
                                 and (excluded.get(str(p.number)) or {}).get("head") != p.head_sha
                                 # nor does a parked one: it waits for a new commit or a person, not for a round
                                 and not rounds.is_parked(self.state.merge_parked, p.number, p.head_sha))
        # Off when merge rounds are off (nothing would drain it) or mergeBacklogHigh is -1 (Alex, 2026-11-12: "we can
        # still make new features, but merging is high prio"): rounds still take the PC first, features keep starting.
        if not self.cfg.merge_rounds or int(self.cfg.merge_backlog_high or 0) < 0:
            self.backlog_paused = False
            return False
        high, low = self._backlog_marks()
        if self.merge_backlog >= high:
            self.backlog_paused = True
        elif self.merge_backlog <= low:
            self.backlog_paused = False
        return self.backlog_paused

    def free_slots(self) -> int:
        """Normal capacity excludes the independently bounded emergency slot."""
        return max(0, self.cfg.max_parallel) - (len(self.jobs) - len(self.emergency_active))

    def emergency_free(self) -> int:
        return max(0, min(1, self.cfg.emergency_workers) - len(self.emergency_active))

    def repair_numbers(self, tasks: dict) -> set[int]:
        """Only recorded merge/build failures and prerequisites of those repairs use the extra slot."""
        numbers = {int(n) for n in self.state.emergency_repairs if str(n).isdigit()}
        numbers.update(t.number for t in tasks.values() if re.search(
            r"<!-- fletchery-(?:merge-failure:|repair-pr:|failure sig=)", t.body or ""))
        numbers.update(int(e["repair_task"]) for e in self.state.merge_excluded.values() if e.get("repair_task"))
        if self.state.merge_last.get("failure_issue"):
            numbers.add(int(self.state.merge_last["failure_issue"]))
        while True:
            deps = {d for n in numbers if n in tasks for d in tasks[n].depends_on}
            if deps <= numbers:
                return numbers
            numbers.update(deps)

    def _start(self, coro, task_no: int, pr_no: int | None, prior_seen, emergency: bool = False) -> None:
        self.active.add(task_no)
        if emergency:
            self.emergency_active.add(task_no)
            self.emergency_why = "Repair in progress."
        job = asyncio.ensure_future(coro)
        self.jobs[job] = (task_no, pr_no, prior_seen)

    def stop_stalled(self, now_ts: float | None = None) -> list[int]:
        """Self-repair: a session that has made no tool call for `stall_minutes` (a hung CLI, a lost connection) held its
        slot forever. Cancel it; _settle requeues the task and its checkout stays for the next attempt."""
        limit = getattr(self.cfg, "stall_minutes", 0) or 0
        live = getattr(self.ctx, "live", None)
        if limit <= 0 or live is None or not self.jobs:
            return []
        now_ts = now_ts if now_ts is not None else datetime.now(timezone.utc).timestamp()
        seen = {}
        for a in live.read().get("activity", []):
            stamps = [a.get(k) for k in ("last_at", "stage_since", "since") if a.get(k)]
            if stamps:
                seen[a["task"]] = max(datetime.fromisoformat(t).timestamp() for t in stamps)
        stopped = []
        for job, (n, _pr, _prior) in list(self.jobs.items()):
            if job.done() or n not in seen or now_ts - seen[n] < limit * 60:
                continue
            job.cancel()
            stopped.append(n)
            self.ctx.log(f"#{n} made no progress for {limit} min; stopped it and queued the task again (checkout kept)")
        return stopped

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
            self.emergency_active.discard(n)
            exc = job.exception() if not job.cancelled() else asyncio.CancelledError()
            if exc is not None:  # a bug or a crash in one job must not lose the others' results
                self.ctx.log(f"#{n} crashed: {type(exc).__name__}: {exc}")
                try:
                    gh.set_status(n, QUEUED if pr_n is None else PR)
                except Exception:
                    pass
                why = (f"stopped: no progress for {self.cfg.stall_minutes} min" if job.cancelled()
                       else f"crashed: {type(exc).__name__}: {exc}")
                o = Outcome(n, "requeue", why, pr_number=pr_n)
            else:
                o = job.result()
            if o.result == "waiting_resource":
                st.resource_waits[str(o.task)] = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat(timespec="seconds")
            self.handoffs.settled(o)
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
            # Dispatch already recorded the newest feedback actually sent to
            # this fixer. Do not advance it to completion time: comments/checks
            # arriving during the repair still need to be handled next pass.
            if o.result in ("signed_out", "limited", "agent_paused", "waiting_dependencies", "retry_fix", "waiting_resource", "handed_off") and pr_n is not None:
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
                until = datetime.now(timezone.utc) + timedelta(minutes=limit_pause_minutes(o.detail, cfg.limit_backoff_minutes))
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
        temporary = {"requeue", "limited", "signed_out", "agent_paused", "waiting_agent", "waiting_resource", "handed_off"}
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
                self._status_pending = text
                previous = getattr(self, "_status_job", None)
                if previous is None or previous.done():
                    async def send_status():
                        while self._status_pending is not None:
                            pending, self._status_pending = self._status_pending, None
                            try:
                                await asyncio.to_thread(gh.write_status, pending)
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
        import watchdog
        guard = watchdog.HangGuard(getattr(self.cfg, "hang_minutes", 0) if not self.dry_run else 0,
                                   lambda minutes: watchdog.exit_hung(str(self.state_dir), self.ctx.log, minutes))
        self._hang_guard = guard
        guard.start()
        drain_limit = float(getattr(self.cfg, "drain_max_minutes", 0) or 0) * 60
        drain_started = 0.0
        drain_deferred_until = 0.0  # after a drain ran out of time: reload only when nothing runs, or try again then
        while True:
            guard.beat()
            if not self._reload_draining:
                changed = bool(code_changed and code_changed())
                if changed and not self.update_pending and not self.restart_reason:
                    self.ctx.log("Updated code is installed; workers continue until the active merge round finishes"
                                 if self.round_job is not None else "Updated code is installed; preparing to reload")
                self.update_pending = changed
                idle = not self.jobs and self.round_job is None
                if self.restart_reason or (changed and self.round_job is None and (idle or loop.time() >= drain_deferred_until)):
                    self._reload_draining = True
                    drain_started = loop.time()
                    self.ctx.log(f"{self.restart_reason or 'Fletchery' + chr(39) + 's code changed on disk'}; "
                                 "restarting once running sessions finish")
            elif (drain_limit and not self.restart_reason and self.jobs
                    and loop.time() - drain_started > drain_limit):
                # 11-13: a reload waited hours behind one long session while no round could start. Past the limit the
                # reload stops holding everything back: rounds and workers go on, and it happens the moment nothing
                # runs (or the drain is tried again after a while). Nothing running is cancelled.
                self._reload_draining = False
                drain_deferred_until = loop.time() + 6 * drain_limit
                self.ctx.log(f"reload waited {int(drain_limit // 60)} min for {len(self.jobs)} session(s); rounds and "
                             "workers continue, and Fletchery reloads as soon as nothing is running")
            try:
                self.dispatch(draining=self._reload_draining)
                await asyncio.sleep(0)  # let new worker coroutines start before optional status publishing
                self.publish([])
                self.sync_error = self.dispatch_warning
            except Exception as e:
                self.passing = False
                self.sync_error = f"{type(e).__name__}: {e}"
                self.ctx.log(f"check failed: {self.sync_error}")
            if self._reload_draining and not self.jobs and self.round_job is None and not (self._storage_job and not self._storage_job.done()):
                if self._package_poll_job is not None:
                    await self._package_poll_job
                guard.stop()
                return
            wait = min(30, self.cfg.poll_minutes * 60) if self.sync_error else self.cfg.poll_minutes * 60
            handoff = self.state.merge_resume.get("wait_for") or {}
            if handoff.get("started_at") and self.state.merge_resume.get("authorized") and not self.state.held and not self.state.merge_held:
                try:
                    elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(handoff["started_at"].replace("Z", "+00:00"))).total_seconds()
                    wait = min(wait, max(1, 900 - elapsed))
                except (ValueError, TypeError):
                    pass
            future_windows = [(__import__("plans").utc(r["not_before"]) - datetime.now(timezone.utc)).total_seconds()
                              for r in self.plans.rows if r["state"] == "queued"]
            positive_windows = [seconds for seconds in future_windows if seconds > 0]
            if positive_windows:
                wait = min(wait, max(1, min(positive_windows)))
            if any(r["kind"] == "package" and r["state"] not in __import__("plans").TERMINAL for r in self.plans.rows):
                wait = min(wait, 30)
            # A scheduled build starts within a minute of its time, and one waiting for this PC is noticed as soon as
            # the slot frees.
            if any(r["state"] not in ("done", "failed", "cancelled") for r in self.builds.rows):
                wait = min(wait, 60)
            self.next_pass_at = datetime.fromtimestamp(datetime.now(timezone.utc).timestamp() + wait,
                                                       timezone.utc).isoformat(timespec="seconds")
            deadline = loop.time() + wait
            while True:
                left = deadline - loop.time()
                if left <= 0:
                    break
                waits = [asyncio.ensure_future(self._wake.wait())] + list(self.jobs) + \
                    ([self.round_job] if self.round_job is not None else []) + ([self._storage_job] if self._storage_job and not self._storage_job.done() else [])
                done, _ = await asyncio.wait(waits, timeout=min(left, 60), return_when=asyncio.FIRST_COMPLETED)
                guard.beat()
                try:
                    self.stop_stalled()
                except Exception as e:  # the watchdog must never stop the loop
                    self.ctx.log(f"stall check failed: {type(e).__name__}: {e}")
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
                if self._storage_job is not None and self._storage_job in done:
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
    dispatch_warning: str = ""  # partial network failures still get a short retry
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
        import rounds
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
                         "repair_wait": self.state.merge_excluded.get(str(t.pr_number), {}).get("why", ""),
                         "parked": (self.state.merge_parked.get(str(t.pr_number), {}).get("why", "")
                                    if pr and rounds.is_parked(self.state.merge_parked, pr.number, pr.head_sha) else "")})
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
                         "worker_rank": st.task_worker_rank.get(str(t.number), ""),
                         "pr_url": f"{url}/pull/{t.pr_number}" if t.pr_number else ""})
        return {"repo": cfg.repo, "repo_url": url, "project": cfg.name, "label": graph.PREFIX,
                "storage": self.storage_view(),
                "workers_enabled": cfg.workers_enabled, "last_pass": st.last_pass, "auth": cfg.auth,
                "max_parallel": cfg.max_parallel, "emergency_workers": {"max_parallel": cfg.emergency_workers, "live": [], "why": self.emergency_why}, "poll_minutes": cfg.poll_minutes,
                "spent_today": round(st.spent.get(today(), 0.0), 2), "daily_budget": cfg.daily_budget_usd,
                "usage_week": self.usage_week(), "usage_agents": self.usage_agents(),
                "restarting": self.restart_status(), "update_pending": self.update_pending,
                "update_note": self.pending_update_status(),
                "held": st.held, "pc_paused": self.pc_paused_view(), "signed_out": st.signed_out, "limit_until": st.paused_until if st.paused_until and now() < st.paused_until else "",
                "limit_reason": st.paused_reason, "tasks": rows, "merge": self.merge_rows(tasks, prs),
                "merge_view": self.merge_view,
                "merge_auto": {"on": bool(cfg.merge_rounds), "held": st.merge_held, "priority": self.merge_priority,
                               "backlog": {"count": self.merge_backlog, "paused": self.backlog_paused,
                                           "high": self._backlog_marks()[0], "low": self._backlog_marks()[1]},
                               "parked": {k: v for k, v in st.merge_parked.items() if v.get("parked")},
                               "round_shares": (list(self.ctx.agents.round_account) if self.ctx.agents is not None
                                                and self.ctx.agents.round_account else None), "running": dict(self.round_info),
                               "why": self.merge_why, "resume": dict(st.merge_resume), "last": dict(st.merge_last or {}), "label": cfg.merge_label or "ready"},
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
        stall = getattr(self, "_checkout_stall", None) or {}
        if stall.get("loud"):
            L += [f"**Merging stalled for {stall['minutes']} min:** {stall['why']}. Workers keep going; end the named "
                  "process if it is a leftover.", ""]
        if mv["steps"]:
            mark = {"done": "✓", "running": "▶", "waiting": "⏸", "failed": "✗", "todo": "○"}
            L += ["Steps: " + " → ".join(f"{mark[s['state']]} {s['name']}" for s in mv["steps"]), ""]
        in_round = [r for r in merge if r["pr"] in mv["prs"]]
        import rounds
        parked_rows = [r for r in merge if r["pr"] not in mv["prs"] and r.get("parked")]
        rest = [r for r in merge if r["pr"] not in mv["prs"] and not r.get("parked")]
        if in_round:
            L += [f"**In round {mv['round'] or '?'}** ({len(in_round)}):", *[self.merge_line(r) for r in in_round], ""]
        L += [("**Waiting for a round**" if mv["prs"] else "**Waiting to merge**") + f" ({len(rest)}), most-unblocking "
              "first. A task that depends on one starts only once its PR merges:"]
        L += [self.merge_line(r) for r in rest] or ["- (nothing)"]
        L += [""]
        if parked_rows:
            L += [f"**Parked** ({len(parked_rows)}): deferred twice at the same head, so rounds skip them until a new "
                  f"commit (or {rounds.PARK_HOURS} h). They don't count toward the backlog."]
            L += [self.merge_line(r) + f" · {r['parked'][:240]}" for r in parked_rows] + [""]
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

    def usage_agents(self) -> list[dict]:
        """Tokens, sessions and hours per agent over 7 days, from usage.jsonl (usage.py)."""
        import usage
        try:
            return usage.summary(getattr(self.ctx, "usage_log", None))["agents"]
        except Exception:
            return []

    def _ledger(self, o: Outcome) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with (self.state_dir / "ledger.jsonl").open("a", encoding="utf-8") as f:
            row = {"at": now(), "task": o.task, "result": o.result, "pr": o.pr_number,
                   "cost_usd": round(o.cost_usd, 4), "sessions": o.sessions,
                   "detail": o.detail.splitlines()[0] if o.detail else ""}
            if o.worker_rank:
                row["worker_rank"] = o.worker_rank
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


def _report_age(report: dict) -> float:
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(str(report.get("checked_at", "")).replace("Z", "+00:00"))).total_seconds()
    except ValueError:
        return float("inf")


def _tasks_by_pr(tasks: dict[int, Task], prs: dict[int, PullRequest]) -> dict[int, Task]:
    """Match open Fletchery PRs to their tasks by the `Closes #n` line (and the branch name as a fallback)."""
    out: dict[int, Task] = {}
    for pr in prs.values():
        for t in sorted(tasks.values(), key=lambda t: t.status != PR):
            if re.search(r"\bcloses\s+#" + str(t.number) + r"\b", pr.body or "", re.I) or pr.head == t.branch:
                t.pr_number = pr.number
                if t.status == PR or (t.status == QUEUED and "<!-- fletchery-blocker-applied:" in (t.body or "")):
                    out[pr.number] = t
                break
    return out
