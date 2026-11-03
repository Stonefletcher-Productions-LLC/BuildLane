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
    POST /api/agents/add      {name, kind, model, tier, vendor, keyEnv}: a new agent (saved in the profile)
    POST /api/agents/remove   {name}
    POST /api/agents/check    check every agent's sign-in now
    POST /api/agents/login    {name}: open that agent's own sign-in in a console window on this PC
    POST /api/agents/escalate {on}: hand a failed task to a stronger agent before blocking it
    POST /api/agents/auth     {name, mode: plan|key}: run that agent on its plan sign-in or on its stored API key
    POST /api/keys/set        {name, value}: store an API key in Windows Credential Manager (never shown again)
    POST /api/keys/clear      {name}

Every POST needs the header X-Fletchery-Token: a random value made at start and written only into the served page, so a
web page from anywhere else cannot press the buttons. Requests whose Host is not this machine are refused (DNS
rebinding)."""
from __future__ import annotations

import json
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import lane
from live import ledger_tail

HERE = Path(__file__).parent
UI = HERE.parent / "ui"
MAX_BODY = 64 * 1024


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
        snap["held"] = fm.state.held
        snap["agents"] = fm.agents_view()  # live: a switch or a sign-in shows at once, not at the next pass
        snap["signed_out"] = fm.state.signed_out
        cfg = fm.cfg
        lane_view = None
        if cfg.lane_status_file:
            prs = {t["pr"] for t in snap.get("tasks", []) if t.get("pr")}
            lane_view = lane.read(cfg.lane_status_file, cfg.lane_pause_file, cfg.lane_drive, cfg.lane_min_free_gb, prs)
        return {**snap, "lane": lane_view, "next_pass_at": fm.next_pass_at, "passing": fm.passing, "live": live["activity"],
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
        if not 1 <= v <= 6:
            raise ValueError("Workers go from 1 to 6. On a Claude plan, more workers use your plan's limits faster.")
        self.fm.cfg.save(max_parallel=v)
        if self.fm.snapshot:
            self.fm.snapshot["max_parallel"] = v
        self.fm.ctx.log(f"workers set to {v} from the app")
        self.fm.wake()  # more workers start now; fewer: running ones finish, no new one starts until below the limit
        return {"max_parallel": v}

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

    def _save_agents(self) -> None:
        """Write the agents back into the profile, keeping any keys this version does not know."""
        pool, cfg = self._pool(), self.fm.cfg
        raw = {str(a.get("name")): dict(a) for a in (cfg.agents or [])}
        out = []
        for spec in pool.specs.values():
            a = raw.get(spec.name, {"name": spec.name})
            a.update({"kind": spec.kind, "tier": spec.tier, "vendor": spec.vendor, "enabled": spec.enabled,
                      "auth": spec.auth})
            if spec.model:
                a["model"] = spec.model
            if spec.key_env:
                a["keyEnv"] = list(spec.key_env)
            out.append(a)
        cfg.save(agents=out)
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
        from agents import guess_vendor, specs_from, tidy_name
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
            tier = int(data.get("tier") or 2)
        except (TypeError, ValueError):
            raise ValueError("Tier is a whole number, 1 (cheapest) to 5.")
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
        key_env = [k.strip() for k in str(data.get("keyEnv", "")).split(",") if k.strip()]
        raw = {"name": name, "kind": kind, "model": model, "tier": max(1, min(5, tier)),
               "vendor": str(data.get("vendor", "")).strip().lower() or guess_vendor(kind, model), "enabled": True, "keyEnv": key_env}
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
                self.path = self.path.split("?", 1)[0].split("#", 1)[0]  # the page reads its own query (?open=, ?only=)
                if not self._host_ok():
                    return self._json(403, {"error": "Open the dashboard at http://127.0.0.1:%d" % dash.port})
                if self.path in ("/", "/index.html"):
                    return self._send(200, dash.page(), "text/html; charset=utf-8")
                if self.path == "/api/status":
                    return self._json(200, dash.status())
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
                    routes = {"/api/agents/toggle": dash.toggle_agent, "/api/agents/add": dash.add_agent,
                              "/api/agents/remove": dash.remove_agent, "/api/agents/login": dash.login_agent,
                              "/api/agents/escalate": dash.set_escalate, "/api/agents/auth": dash.set_agent_auth,
                              "/api/agents/models": dash.list_models, "/api/keys/set": dash.set_key,
                              "/api/keys/clear": dash.clear_key}
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
        threading.Thread(target=self.server.serve_forever, name="fletchery-server", daemon=True).start()

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
