"""Fletchery's dashboard: a small web server inside the Fletchery process, on this machine only (127.0.0.1).

    GET  /                 the page (dashboard.html, with this run's token in it)
    GET  /api/status       the last pass, live activity, log tail, history
    POST /api/pause        start nothing new until resume (running work finishes)
    POST /api/resume
    POST /api/run-now      run a pass now instead of waiting for the poll
    POST /api/task         {title, body, depends: [n]} -> a new `foreman` issue
    POST /api/lane/pause   pause the Build Lane (the same PAUSED file as Tools/CI/Pause.ps1)
    POST /api/lane/resume
    POST /api/agents/toggle   {name, on}: switch one coding agent on or off (saved in the profile)
    POST /api/agents/add      {name, kind, model, vendor, keyEnv; tier optional, else from benchmarks}: a new agent (saved in the profile)
    POST /api/agents/remove   {name}
    POST /api/agents/check    check every agent's sign-in now
    POST /api/agents/login    {name}: open that agent's own sign-in in a console window on this PC
    POST /api/agents/escalate {on}: hand a failed task to a stronger agent before blocking it
    POST /api/agents/scores   {name, scores: {cpp|scripts|ui|docs|review|cost: 0-100 or null}}: your own scores
    POST /api/agents/refresh-scores  fetch the newest benchmark catalog (model_scores.json) from Fletchery's main branch
    POST /api/merge/hold      {on}: pause or resume merge rounds (a running round finishes)
    POST /api/merge/now       look for ready PRs now, retrying a round that didn't land
    POST /api/agents/refresh  lift usage-limit pauses, re-read OpenCode's models, check every sign-in again
    POST /api/settings/get    the Settings page: this project's settings, and every project
    POST /api/settings/save   {changes: {field: value}}: validate and save into the profile
    POST /api/settings/check  {repo, project_root, base_branch}: can GitHub see it, is the clone that repo
    POST /api/projects/new    {name, repo, project_root, base_branch, label, copy_agents, create_labels}
    POST /api/projects/switch {path}: run that project (after running sessions finish)
    POST /api/restart         restart Fletchery once running sessions finish
    POST /api/agents/auth     {name, mode: plan|key}: run that agent on its plan sign-in or on its stored API key
    POST /api/keys/set        {name, value}: store an API key in Windows Credential Manager (never shown again)
    POST /api/keys/clear      {name}
POST /api/issue/get       {number}: the issue and its comments (the Needs you reader)
POST /api/issue/reply     {number, body, requeue=true}: comment on it and put it back in the queue

Every POST needs the header X-Fletchery-Token: a random value made at start and written only into the served page, so a
web page from anywhere else cannot press the buttons. Requests whose Host is not this machine are refused (DNS
rebinding)."""
from __future__ import annotations

import json
import os
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import lane
from live import ledger_tail

HERE = Path(__file__).parent
UI = HERE.parent / "ui"
MAX_BODY = 64 * 1024


def dashboard_thread_priority() -> None:
    """Keep HTTP threads schedulable while the controller and agents run below normal."""
    if os.name != "nt":
        return
    try:
        import ctypes
        kernel = ctypes.windll.kernel32
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        kernel.GetCurrentThread.restype = ctypes.c_void_p
        kernel.GetPriorityClass.argtypes = [ctypes.c_void_p]
        kernel.SetThreadPriority.argtypes = [ctypes.c_void_p, ctypes.c_int]
        if kernel.GetPriorityClass(kernel.GetCurrentProcess()) == 0x00004000:
            # BELOW_NORMAL's base 6 plus HIGHEST's 2 equals ordinary priority 8.
            # No process/child priority change and no time-critical scheduling.
            kernel.SetThreadPriority(kernel.GetCurrentThread(), 2)
    except (AttributeError, OSError):
        pass  # Status remains usable when the OS does not support this hint.


def live_merge_progress(fm) -> dict:
    import merge
    progress = fm.cfg.merge_round_file or str(fm.state_dir / "merge-round.json")
    cached = (getattr(fm, "snapshot", None) or {}).get("merge_view") or {}
    return merge.refresh(progress, fm.cfg.heavy_lease_file, cached, getattr(fm.cfg, "worktree_root", None))


class Dashboard:
    def __init__(self, foreman, port: int, host: str = "127.0.0.1"):
        self.fm = foreman
        self.port = port
        self.host = host
        self.token = secrets.token_urlsafe(24)
        self.server: ThreadingHTTPServer | None = None

    # ---- data ----
    def status(self) -> dict:
        fm = self.fm
        live = fm.ctx.live.read() if fm.ctx.live else {"activity": [], "log": []}
        snap = dict(fm.snapshot or {})
        if not snap:
            import graph
            snap = {"repo": fm.cfg.repo, "repo_url": f"https://github.com/{fm.cfg.repo}", "tasks": [], "this_pass": [],
                    "project": fm.cfg.name, "label": graph.PREFIX, "workers_enabled": fm.cfg.workers_enabled,
                    "auth": fm.cfg.auth, "max_parallel": fm.cfg.max_parallel, "daily_budget": fm.cfg.daily_budget_usd,
                    "spent_today": 0, "held": fm.state.held, "last_pass": "", "poll_minutes": fm.cfg.poll_minutes,
                    "usage_week": fm.usage_week()}
        from copy import deepcopy
        ranks = deepcopy(getattr(fm, "worker_rank_view", None))
        if ranks:
            ranks["mode"] = "manual" if fm.cfg.worker_rank_limits is not None else "automatic"
            if fm.cfg.worker_rank_limits is not None:
                ranks["limits"] = dict(fm.cfg.worker_rank_limits)
        snap["max_parallel"] = fm.cfg.max_parallel
        snap["worker_ranks"] = ranks
        recorded_ranks = getattr(fm.state, "task_worker_rank", {})
        snap["tasks"] = [{**row, "worker_rank": recorded_ranks.get(str(row["number"]), row.get("worker_rank", ""))}
                         for row in snap.get("tasks", [])]
        snap["worker_handoffs"] = fm.handoffs.view() if hasattr(fm, "handoffs") else {}
        snap["integration_queue"] = fm.plans.view() if hasattr(fm, "plans") else None
        snap["held"] = fm.state.held
        # Merge controls must reflect a click immediately, even during a slow GitHub poll.
        snap["merge_auto"] = {**snap.get("merge_auto", {}), "on": bool(fm.cfg.merge_rounds),
                              "held": fm.state.merge_held, "running": dict(fm.round_info),
                              "last": dict(fm.state.merge_last), "resume": dict(fm.state.merge_resume),
                              "why": "merging is paused" if fm.state.merge_held else fm.merge_why}
        # Progress changes independently of scheduler/GitHub polling, including
        # while a settings restart drains a long-running merger. No ancestry
        # subprocesses on this request path: membership is unknown until polled.
        snap["merge_view"] = live_merge_progress(fm)
        snap["storage"] = fm.storage_view()
        snap["agents"] = fm.agents_view()  # live: a switch or a sign-in shows at once, not at the next pass
        snap["signed_out"] = fm.state.signed_out
        snap["setup_needed"] = not fm.cfg.repo
        snap["restarting"] = fm.restart_status()
        snap["update_pending"] = getattr(fm, "update_pending", False)
        snap["sync_error"] = getattr(fm, "sync_error", "")
        cfg = fm.cfg
        lane_view = None
        if cfg.lane_status_file:
            prs = {t["pr"] for t in snap.get("tasks", []) if t.get("pr")}
            lane_view = lane.read(cfg.lane_status_file, cfg.lane_pause_file, cfg.lane_drive, cfg.lane_min_free_gb, prs)
        activity = [{**row, "emergency": row.get("task") in fm.emergency_active} for row in live["activity"]]
        snap["emergency_workers"] = {"max_parallel": cfg.emergency_workers,
                                     "live": [row for row in activity if row["emergency"]],
                                     "why": "Emergency workers are set to 0; running repairs finish." if not cfg.emergency_workers else fm.emergency_why}
        return {**snap, "lane": lane_view, "next_pass_at": fm.next_pass_at, "passing": fm.passing, "live": activity,
                "log": live["log"][-200:], "history": ledger_tail(fm.state_dir / "ledger.jsonl", 40)}

    def add_task(self, data: dict) -> dict:
        title = str(data.get("title", "")).strip()
        body = str(data.get("body", "")).strip()
        depends = data.get("depends") or []
        if not title or len(title) > 200:
            raise ValueError("Give the task a title (up to 200 characters).")
        if len(body) > 20000:
            raise ValueError("The description is too long (20,000 characters at most).")
        try:
            depends = sorted({int(d) for d in depends})
        except (TypeError, ValueError):
            raise ValueError("Depends on takes issue numbers, like 12, 15.")
        if depends:
            body = (body + "\n\n" if body else "") + "Depends on: " + ", ".join(f"#{d}" for d in depends)
        import graph
        n = self.fm.ctx.gh.create_issue(title, body, [graph.PREFIX, graph.label_for("queued")])
        self.fm.ctx.log(f"#{n} added from the dashboard: {title}")
        self.fm.wake()
        return {"number": n, "url": f"https://github.com/{self.fm.cfg.repo}/issues/{n}"}

    @staticmethod
    def _issue_number(data: dict) -> int:
        try:
            n = int(data.get("number"))
        except (TypeError, ValueError):
            raise ValueError("Which issue? Give its number.")
        if n <= 0:
            raise ValueError("Which issue? Give its number.")
        return n

    def issue_get(self, data: dict) -> dict:
        """The issue and its comments, for the Needs you reader."""
        return self.fm.ctx.gh.issue_thread(self._issue_number(data))

    def issue_reply(self, data: dict) -> dict:
        """Answer a task that needs you: post the comment (if any) and put the task back in the queue straight away, so
        the next worker reads the answer with the issue (pipeline gives workers every comment people left)."""
        n = self._issue_number(data)
        body = str(data.get("body", "")).strip()
        requeue = data.get("requeue", True) is not False
        if len(body) > 20000:
            raise ValueError("The answer is too long (20,000 characters at most).")
        if not body and not requeue:
            raise ValueError("Write an answer, or re-queue it.")
        gh = self.fm.ctx.gh
        if body:
            gh.comment(n, body)
        if requeue:
            import graph
            gh.set_status(n, graph.QUEUED)
        self.fm.ctx.log(f"#{n} " + ("answered and re-queued" if body and requeue else "answered" if body else "re-queued")
                        + " from the dashboard")
        if requeue:
            for t in (self.fm.snapshot or {}).get("tasks", []):  # leave Needs you now, not at the next pass
                if t.get("number") == n:
                    t["status"], t["why"] = graph.QUEUED, "ready"
            self.fm.wake()
        return {"number": n, "commented": bool(body), "requeued": requeue}

    def set_budget(self, data: dict) -> dict:
        try:
            v = round(float(data.get("daily_budget_usd")))
        except (TypeError, ValueError):
            raise ValueError("The daily cap is a number of dollars.")
        if not 0 <= v <= 1000:
            raise ValueError("The daily cap goes from $0 (start nothing new) to $1,000.")
        self.fm.cfg.save(daily_budget_usd=float(v))
        if self.fm.snapshot:
            self.fm.snapshot["daily_budget"] = float(v)
        self.fm.ctx.log(f"daily cap set to ${v} from the dashboard")
        self.fm.wake()
        return {"daily_budget": v}

    def set_workers(self, data: dict) -> dict:
        try:
            v = int(data.get("max_parallel"))
        except (TypeError, ValueError):
            raise ValueError("The number of workers is a whole number.")
        if not 0 <= v <= 30:
            raise ValueError("Workers go from 0 (pause normal coding; merging, builds and emergency repairs carry on) to 30. On a plan, more "
                             "workers use its limits faster.")
        self.fm.cfg.save(max_parallel=v, worker_rank_limits=None)
        if self.fm.snapshot:
            self.fm.snapshot["max_parallel"] = v
        self.fm.ctx.log(f"workers set to {v} from the app" + (": normal writing is paused (running sessions finish); "
                                                                  "merging, builds, enabled emergency repairs and issue closing carry on" if v == 0 else ""))
        self.fm.wake()  # more workers start now; fewer: running ones finish, no new one starts until below the limit
        return {"max_parallel": v}

    def set_worker_ranks(self, data: dict) -> dict:
        limits = data.get("limits")
        if not isinstance(limits, dict) or set(limits) != {"low", "medium", "high"} or any(type(v) is not int or not 0 <= v <= 10 for v in limits.values()):
            raise ValueError("Set low, medium and high writing capacity to whole numbers from 0 to 10.")
        if self.fm.ctx.agents is None:
            raise ValueError("Configure agents before setting worker ranks.")
        self.fm.cfg.save(max_parallel=sum(limits.values()), worker_rank_limits=dict(limits))
        self.fm.ctx.log("Worker rank limits changed; active sessions finish normally")
        self.fm.wake()
        return {"limits": limits, "max_parallel": sum(limits.values())}

    def set_auth(self, data: dict) -> dict:
        import auth
        want = str(data.get("auth", ""))
        rt = self.fm.ctx.runtime
        ok, msg = auth.check(want, getattr(rt, "saved_key", None))
        if not ok:
            raise ValueError(msg)
        if hasattr(rt, "set_auth"):
            rt.set_auth(want)
        self.fm.cfg.save(auth=want)
        if self.fm.snapshot:
            self.fm.snapshot["auth"] = want
        self.fm.ctx.log(f"sessions now run on {'the API key' if want == 'api' else 'the Claude plan'} (from the dashboard)")
        return {"auth": want, "message": msg}

    # ---- agents and keys ----
    def _pool(self):
        pool = self.fm.ctx.agents
        if pool is None:
            raise ValueError("This project runs Claude only. Add a workers.agents list to its profile to use other agents.")
        return pool

    def _agent_config(self, specs=None) -> list:
        """Serialize agents for the profile, keeping any keys this version does not know."""
        pool, cfg = self._pool(), self.fm.cfg
        raw = {str(a.get("name")): dict(a) for a in (cfg.agents or [])}
        out = []
        for spec in (pool.specs if specs is None else specs).values():
            a = raw.get(spec.name, {"name": spec.name})
            a.update({"kind": spec.kind, "tier": spec.tier, "vendor": spec.vendor, "enabled": spec.enabled,
                      "auth": spec.auth, "roles": spec.roles, "levels": spec.levels, "maxParallel": spec.max_parallel})
            a["model"] = spec.model
            a["keyEnv"] = list(spec.key_env or [])
            if spec.scores:
                a["scores"] = dict(spec.scores)
            else:
                a.pop("scores", None)
            out.append(a)
        return out

    def _save_agents(self) -> None:
        self.fm.cfg.save(agents=self._agent_config())
        self.fm.wake()

    def toggle_agent(self, data: dict) -> dict:
        pool = self._pool()
        name, on = str(data.get("name", "")), bool(data.get("on"))
        if name not in pool.specs:
            raise ValueError(f"No agent called {name}.")
        if not on and not any(s.enabled for s in pool.specs.values() if s.name != name):
            raise ValueError("At least one agent has to stay on. Pause Fletchery instead to stop all work.")
        pool.set_enabled(name, on)
        if on:
            pool.accounts.pop(name, None)
            self.fm.check_accounts(force=True)
        self._save_agents()
        self.fm.ctx.log(f"agent {name} switched {'on' if on else 'off'} from the dashboard"
                        + ("" if on else "; tasks it is running now finish"))
        return {"name": name, "on": on}

    def add_agent(self, data: dict) -> dict:
        from agents import guess_vendor, restrictions, specs_from, tidy_name
        pool = self._pool()
        kind = str(data.get("kind", "")).strip()
        model = str(data.get("model", "")).strip()
        name = str(data.get("name", "")).strip()
        if "/" in name and not model:  # a model typed into the name box
            model, name = name, ""
        name = tidy_name(name, model)
        if not re.match(r"^[a-z0-9][a-z0-9._\-]{0,31}$", name):
            raise ValueError("Name the agent in lower case, letters, digits and dashes (e.g. kimi).")
        if name in pool.specs:
            raise ValueError(f"There is already an agent called {name}.")
        if kind not in ("claude", "codex", "opencode"):
            raise ValueError("Kind is claude, codex or opencode.")
        try:
            tier = int(data["tier"]) if data.get("tier") not in (None, "") else 0
        except (TypeError, ValueError):
            raise ValueError("Tier is a whole number, 1 (cheapest) to 5.")
        if kind == "codex":
            from agents import codex_model
            model = codex_model(model)
            if "/" in model:
                raise ValueError(f"Codex takes a plain model name such as gpt-6-astra, not {model}. "
                                 "For a provider/model name, add it as an OpenCode agent.")
        if kind == "opencode" and "/" not in model:
            raise ValueError("OpenCode models are provider/model, e.g. opencode-go/kimi-k3.")
        if kind == "opencode":
            import clis
            known = clis.opencode_models()
            if known and model not in known:
                import difflib
                tail = model.split("/")[-1].lower()
                near = [m for m in known if tail and all(w in m.lower() for w in re.split(r"[\-._]", tail) if w)]
                near = (near or difflib.get_close_matches(model, known, n=3, cutoff=0.5))[:3]
                raise ValueError(f"OpenCode doesn't offer {model}." + (f" Did you mean {' or '.join(near)}?" if near
                                 else " Run `opencode models` for the list."))
        if not tier:  # nobody picks a strength: it follows the model's benchmark score (2 when no benchmark lists it)
            import scores
            tier = scores.tier_for(kind, model, scores.load_catalog(getattr(pool, "catalog_override", None)))
        key_env = [k.strip() for k in str(data.get("keyEnv", "")).split(",") if k.strip()]
        raw = {"name": name, "kind": kind, "model": model, "tier": max(1, min(5, tier)),
               "vendor": str(data.get("vendor", "")).strip().lower() or guess_vendor(kind, model), "enabled": True, "keyEnv": key_env}
        raw.update(restrictions(data))
        spec = specs_from([raw])[0]
        pool.specs[name] = spec
        self.fm.cfg.agents = list(self.fm.cfg.agents or []) + [{k: v for k, v in raw.items() if v not in ("", [])}]
        self._save_agents()
        self.fm.check_accounts(force=True)
        self.fm.ctx.log(f"agent {name} ({spec.label}) added from the dashboard")
        return {"name": name}

    def list_models(self, data: dict) -> dict:
        import clis
        return {"models": clis.opencode_models()}

    def remove_agent(self, data: dict) -> dict:
        pool = self._pool()
        name = str(data.get("name", ""))
        if name not in pool.specs:
            raise ValueError(f"No agent called {name}.")
        if len(pool.specs) <= 1 or not any(s.enabled for s in pool.specs.values() if s.name != name):
            raise ValueError("At least one switched-on agent has to stay.")
        if pool.busy.get(name):
            raise ValueError(f"{name} is working on a task. Switch it off first; remove it once that finishes.")
        del pool.specs[name]
        self.fm.cfg.agents = [a for a in (self.fm.cfg.agents or []) if a.get("name") != name]
        self._save_agents()
        self.fm.ctx.log(f"agent {name} removed from the dashboard")
        return {"name": name}

    def refresh_agents(self, data: dict) -> dict:
        """After a plan upgrade or a new sign-in: lift every usage-limit pause, forget the cached model list and check
        every sign-in again. An agent still at its limit pauses again at its next session."""
        pool = self._pool()
        gone = pool.unpause()
        st = self.fm.state
        was_all = bool(st.paused_until)
        st.paused_until, st.paused_reason = "", ""
        import clis
        clis._models_cache.clear()
        self.fm.check_accounts(force=True)
        msg = ("Lifted the usage-limit pause on " + ", ".join(gone) + ". " if gone else
               "No agent was paused. ") + ("Fletchery's own limit pause is lifted too. " if was_all else "") + \
              "Checking every sign-in again; the panel updates in a few seconds."
        self.fm.ctx.log("agents refreshed from the app" + (f": unpaused {', '.join(gone)}" if gone else ""))
        self.fm.wake()
        return {"unpaused": gone, "message": msg}

    def queue_add(self, data: dict) -> dict:
        return self.fm.plans.enqueue(data)

    def queue_cancel(self, data: dict) -> dict:
        return self.fm.plans.cancel(str(data.get("id", "")))

    def queue_mode(self, data: dict) -> dict:
        planned = data.get("planned")
        if type(planned) is not bool:
            raise ValueError("Choose planned windows or automatic merging.")
        if not planned and any(r["state"] not in ("complete", "cancelled", "blocked") for r in self.fm.plans.rows):
            raise ValueError("Complete or cancel queued requests before returning to automatic merging.")
        self.fm.state.planned_integration = planned
        self.fm.plans.save()
        self.fm.wake()
        return {"planned": planned}

    def merge_hold(self, data: dict) -> dict:
        on = bool(data.get("on"))
        self.fm.hold_merging(on)
        return {"held": on, "message": "Merging paused: no new round starts; a running round finishes." if on
                else "Merging resumed: a round starts when PRs are ready."}

    def storage_cleanup(self, data: dict) -> dict:
        return self.fm.request_storage_cleanup()

    def merge_recover(self, data: dict) -> dict:
        return self.fm.recover_interrupted(data)

    def merge_now(self, data: dict) -> dict:
        if not self.fm.cfg.merge_rounds:
            raise ValueError("Merge rounds are off. Turn them on in Settings > This project > Merging.")
        if self.fm.state.planned_integration and not self.fm.state.merge_resume:
            raise ValueError("Use Queue merge window to choose a candidate and start time in planned mode.")
        self.fm.merge_now()
        return {"message": "Looking for ready PRs now."}

    def login_agent(self, data: dict) -> dict:
        import clis
        pool = self._pool()
        name = str(data.get("name", ""))
        if name not in pool.specs:
            raise ValueError(f"No agent called {name}.")
        msg = clis.open_login(pool.specs[name])
        pool.accounts.pop(name, None)
        return {"message": msg + " Press Check sign-ins when you are done."}

    def set_agent_auth(self, data: dict) -> dict:
        import keys
        pool = self._pool()
        name, mode = str(data.get("name", "")), str(data.get("mode", ""))
        if name not in pool.specs:
            raise ValueError(f"No agent called {name}.")
        if mode not in ("plan", "key"):
            raise ValueError("Run on plan or key.")
        spec = pool.specs[name]
        if mode == "key":
            key = spec.api_key_name
            if not key:
                raise ValueError(f"Fletchery doesn't know which key {name}'s model takes. Add it under Keys it needs.")
            stored = keys.get(key)
            if not stored and spec.kind == "claude":
                import auth
                stored = auth.api_key()
            if not stored:
                raise ValueError(f"Store {key} first (API keys, below), then switch {name} to it.")
        spec.auth = mode
        pool._runtimes.pop(name, None)  # the next session starts on the new sign-in; running ones finish
        if spec.kind == "claude":
            rt = self.fm.ctx.runtime
            if hasattr(rt, "set_auth"):
                rt.set_auth("api" if mode == "key" else "subscription")
            self.fm.cfg.save(auth="api" if mode == "key" else "subscription")
        pool.accounts.pop(name, None)
        self._save_agents()
        self.fm.check_accounts(force=True)
        self.fm.ctx.log(f"agent {name} now runs on {'its API key' if mode == 'key' else 'its plan sign-in'} (from the dashboard)")
        return {"name": name, "mode": mode}

    def set_routing_policy(self, data: dict) -> dict:
        """Explicit subscription/provider preferences and task/role limits, without changing quality scores."""
        from copy import deepcopy
        from agents import restrictions
        if not isinstance(data, dict):
            raise ValueError("Routing policy must be an object")
        pool = self._pool()
        preferred = data.get("preferredKindsByLevel", pool.routing.get("preferredKindsByLevel", {}))
        if not isinstance(preferred, dict) or any(k not in ("easy", "medium", "hard") or not isinstance(v, list)
                or any(x not in ("codex", "claude", "opencode") for x in v) for k, v in preferred.items()):
            raise ValueError("Preferred kinds must be provider lists for easy, medium or hard tasks")
        changes = data.get("agents", [])
        if not isinstance(changes, list) or any(not isinstance(c, dict) for c in changes):
            raise ValueError("Agent restrictions must be a list")
        for change in changes:
            if not isinstance(change.get("name"), str) or change["name"] not in pool.specs:
                raise ValueError("Unknown agent")
            restrictions(change)
        routing, specs = deepcopy(pool.routing), deepcopy(pool.specs)
        routing["preferredKindsByLevel"] = deepcopy(preferred)
        for change in changes:
            for key, value in restrictions(change).items():
                setattr(specs[change["name"]], key, value)
        cfg = self.fm.cfg
        old_routing, old_agents = cfg.routing, cfg.agents
        try:
            cfg.save(routing=routing, agents=self._agent_config(specs))
        except Exception:
            cfg.routing, cfg.agents = old_routing, old_agents
            raise
        # Publish only after the complete policy is durably saved. Preserve spec identity
        # for running sessions and update just the limits affected by this endpoint.
        pool.routing = routing
        for name, spec in specs.items():
            pool.specs[name].roles, pool.specs[name].levels = spec.roles, spec.levels
        self.fm.wake()
        return {"preferredKindsByLevel": preferred}

    def set_scores(self, data: dict) -> dict:
        import scores
        pool = self._pool()
        name = str(data.get("name", ""))
        if name not in pool.specs:
            raise ValueError(f"No agent called {name}.")
        mine = dict(pool.specs[name].scores or {})
        for k, v in (data.get("scores") or {}).items():
            if k not in scores.SKILLS + ["cost"]:
                raise ValueError(f"No score called {k}.")
            if v in (None, ""):
                mine.pop(k, None)
                continue
            try:
                n = float(v)
            except (TypeError, ValueError):
                raise ValueError(f"{scores.SKILL_NAMES.get(k, k)} is a number from 0 to 100, or empty for automatic.")
            if not 0 <= n <= 100:
                raise ValueError(f"{scores.SKILL_NAMES.get(k, k)} is a number from 0 to 100, or empty for automatic.")
            mine[k] = int(round(n))
        pool.specs[name].scores = mine or None
        self._save_agents()
        self.fm.ctx.log(f"agent {name} scores set from the dashboard: {mine or 'all automatic'}")
        return {"name": name, "scores": mine}

    def refresh_scores(self, data: dict) -> dict:
        """The newest model_scores.json from Fletchery's main branch, kept in the state dir."""
        import subprocess
        pool = self._pool()
        repo = str(pool.routing.get("catalogRepo") or "Stonefletcher-Productions-LLC/Fletchery")
        p = subprocess.run(["gh", "api", f"repos/{repo}/contents/fletchery/model_scores.json",
                            "-H", "Accept: application/vnd.github.raw"], capture_output=True, text=True, timeout=60,
                           encoding="utf-8", errors="replace")
        if p.returncode != 0:
            raise ValueError(f"Couldn't fetch the benchmark list from {repo}: {(p.stderr or p.stdout).strip()[:200]}")
        try:
            cat = json.loads(p.stdout)
            assert isinstance(cat.get("models"), dict) and cat["models"]
        except (ValueError, AssertionError, AttributeError):
            raise ValueError("The benchmark list on GitHub isn't valid; kept the current one.")
        target = pool.catalog_override or (self.fm.state_dir / "model_scores.json")
        target.write_text(json.dumps(cat, indent=2), encoding="utf-8")
        pool.catalog_override = target
        self.fm.ctx.log(f"benchmark list refreshed from {repo} (updated {cat.get('updated', '?')})")
        return {"updated": cat.get("updated", ""), "models": len(cat["models"])}

    def settings_get(self, data: dict) -> dict:
        import projects
        out = projects.read_settings(self.fm.cfg)
        out["projects"] = projects.list_projects(getattr(self.fm.cfg, "_path", None))
        out["projects_dir"] = str(projects.projects_dir())
        out["restarting"] = self.fm.restart_status()
        return out

    def settings_save(self, data: dict) -> dict:
        import projects
        res = projects.apply_settings(self.fm.cfg, data.get("changes") or {})
        cfg = self.fm.cfg
        if "max_parallel" in res["saved"]:
            cfg.save(worker_rank_limits=None)
        self.fm.ctx.escalate = cfg.escalate
        if self.fm.snapshot:
            self.fm.snapshot.update(max_parallel=cfg.max_parallel, daily_budget=cfg.daily_budget_usd)
        if res["saved"]:
            self.fm.ctx.log("settings changed from the app: " + ", ".join(res["saved"]))
            self.fm.wake()
        return res

    def settings_check(self, data: dict) -> dict:
        import projects
        return {"checks": projects.check_project(str(data.get("repo", "")), str(data.get("project_root", "")),
                                                 str(data.get("base_branch", "")))}

    def project_new(self, data: dict) -> dict:
        import projects
        res = projects.new_project(data, self.fm.cfg)
        self.fm.ctx.log(f"project {res['name']} added from the app ({res['path']})")
        return res

    def project_switch(self, data: dict) -> dict:
        import projects
        p = Path(str(data.get("path", "")))
        known = {str(Path(x["path"]).resolve()).lower() for x in projects.list_projects(getattr(self.fm.cfg, "_path", None))}
        if not p.is_file() or str(p.resolve()).lower() not in known:
            raise ValueError("That project file isn't in the list.")
        projects.set_active(p)
        self.fm.request_restart(f"switching to the project in {p.name}")
        return {"switching": str(p), "message": "Switching once running sessions finish."}

    def restart(self, data: dict) -> dict:
        self.fm.request_restart("restarting for new settings")
        return {"message": "Restarting once running sessions finish."}

    def set_escalate(self, data: dict) -> dict:
        on = bool(data.get("on"))
        self.fm.ctx.escalate = on
        self.fm.cfg.save(escalate=on)
        return {"on": on}

    def set_key(self, data: dict) -> dict:
        import keys
        name = str(data.get("name", "")).strip().upper()
        keys.set_key(name, str(data.get("value", "")))
        self.fm.ctx.log(f"key {name} stored in Credential Manager from the dashboard")
        self.fm.check_accounts(force=True)
        return {"name": name, "set": True}

    def clear_key(self, data: dict) -> dict:
        import keys
        name = str(data.get("name", "")).strip().upper()
        keys.delete(name)
        self.fm.ctx.log(f"key {name} removed from Credential Manager from the dashboard")
        self.fm.check_accounts(force=True)
        return {"name": name, "set": False}

    # ---- server ----
    def page(self) -> bytes:
        html = (UI / "index.html").read_text(encoding="utf-8")
        return html.replace("__FLETCHERY_TOKEN__", self.token).encode("utf-8")

    def start(self) -> None:
        dash = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "Fletchery"

            def handle(self):
                dashboard_thread_priority()
                super().handle()

            def log_message(self, *a):  # quiet: the dashboard polls every few seconds
                pass

            def _host_ok(self) -> bool:
                host = (self.headers.get("Host") or "").lower()
                return host in (f"127.0.0.1:{dash.port}", f"localhost:{dash.port}", f"[::1]:{dash.port}")

            def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy",
                                 "default-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                                 "font-src https://fonts.gstatic.com; script-src 'self' 'unsafe-inline'; "
                                 "connect-src 'self'; frame-ancestors 'none'")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _json(self, code: int, obj) -> None:
                self._send(code, json.dumps(obj).encode("utf-8"))

            def do_GET(self):
                query = parse_qs(urlsplit(self.path).query)
                self.path = self.path.split("?", 1)[0].split("#", 1)[0]  # the page reads its own query (?open=, ?only=)
                if not self._host_ok():
                    return self._json(403, {"error": "Open the dashboard at http://127.0.0.1:%d" % dash.port})
                if self.path in ("/", "/index.html"):
                    return self._send(200, dash.page(), "text/html; charset=utf-8")
                if self.path == "/api/status":
                    return self._json(200, dash.status())
                if self.path == "/api/merge/log":
                    import merge
                    try:
                        cfg = dash.fm.cfg
                        text = merge.log_text(cfg.merge_round_file or str(dash.fm.state_dir / "merge-round.json"),
                                              cfg.worktree_root, query.get("round", [""])[0], query.get("name", [""])[0])
                        return self._send(200, text.encode("utf-8"), "text/plain; charset=utf-8")
                    except (OSError, ValueError, TypeError):
                        return self._json(404, {"error": "Current round log is unavailable"})
                if self.path == "/favicon.ico":  # the app window's taskbar icon
                    return self._send(200, (UI / "Fletchery.ico").read_bytes(), "image/x-icon")
                return self._json(404, {"error": "not found"})

            def do_POST(self):
                if not self._host_ok():
                    return self._json(403, {"error": "wrong host"})
                if not secrets.compare_digest(self.headers.get("X-Fletchery-Token", ""), dash.token):
                    return self._json(403, {"error": "Reload the window (F5): Fletchery restarted since this page opened."})
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BODY:
                    return self._json(413, {"error": "request too large"})
                try:
                    data = json.loads(self.rfile.read(length) or b"{}") if length else {}
                except ValueError:
                    return self._json(400, {"error": "bad JSON"})
                try:
                    if self.path == "/api/pause":
                        dash.fm.hold(True)
                        return self._json(200, {"held": True})
                    if self.path == "/api/workers/handoff":
                        return self._json(200, dash.fm.handoffs.request())
                    if self.path == "/api/resume":
                        dash.fm.hold(False)
                        return self._json(200, {"held": False})
                    if self.path == "/api/run-now":
                        dash.fm.ctx.log("pass requested from the dashboard")
                        dash.fm.wake()
                        return self._json(200, {"ok": True})
                    if self.path == "/api/task":
                        return self._json(200, dash.add_task(data))
                    if self.path == "/api/budget":
                        return self._json(200, dash.set_budget(data))
                    if self.path == "/api/workers":
                        return self._json(200, dash.set_workers(data))
                    if self.path == "/api/auth":
                        return self._json(200, dash.set_auth(data))
                    routes = {"/api/workers/ranks": dash.set_worker_ranks, "/api/queue/add": dash.queue_add, "/api/queue/cancel": dash.queue_cancel,
                              "/api/queue/mode": dash.queue_mode, "/api/agents/toggle": dash.toggle_agent, "/api/agents/add": dash.add_agent,
                              "/api/agents/remove": dash.remove_agent, "/api/agents/login": dash.login_agent,
                              "/api/agents/escalate": dash.set_escalate, "/api/agents/auth": dash.set_agent_auth,
                              "/api/agents/models": dash.list_models, "/api/agents/scores": dash.set_scores, "/api/agents/routing": dash.set_routing_policy,
                              "/api/agents/refresh-scores": dash.refresh_scores, "/api/agents/refresh": dash.refresh_agents, "/api/merge/hold": dash.merge_hold, "/api/merge/now": dash.merge_now, "/api/merge/recover": dash.merge_recover, "/api/storage/cleanup": dash.storage_cleanup,
                              "/api/settings/get": dash.settings_get, "/api/settings/save": dash.settings_save,
                              "/api/settings/check": dash.settings_check, "/api/projects/new": dash.project_new,
                              "/api/projects/switch": dash.project_switch, "/api/restart": dash.restart, "/api/keys/set": dash.set_key,
                              "/api/keys/clear": dash.clear_key, "/api/issue/get": dash.issue_get,
                              "/api/issue/reply": dash.issue_reply}
                    if self.path in routes:
                        return self._json(200, routes[self.path](data))
                    if self.path == "/api/agents/check":
                        dash._pool()
                        dash.fm.check_accounts(force=True)
                        return self._json(200, {"ok": True})
                    if self.path in ("/api/lane/pause", "/api/lane/resume"):
                        if not dash.fm.cfg.lane_pause_file:
                            return self._json(400, {"error": "No Build Lane pause file is configured."})
                        on = self.path.endswith("pause")
                        lane.set_paused(dash.fm.cfg.lane_pause_file, on)
                        dash.fm.ctx.log("Build Lane paused from the dashboard" if on else "Build Lane resumed from the dashboard")
                        return self._json(200, {"paused": on})
                except ValueError as e:
                    return self._json(400, {"error": str(e)})
                except Exception as e:  # GitHub down, gh signed out: say so on the page
                    return self._json(502, {"error": f"{type(e).__name__}: {e}"})
                return self._json(404, {"error": "not found"})

        self.server = ThreadingHTTPServer((self.host, self.port), Handler)
        self.server.daemon_threads = True
        def serve():
            dashboard_thread_priority()
            self.server.serve_forever()
        threading.Thread(target=serve, name="fletchery-server", daemon=True).start()

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
