#!/usr/bin/env python3
"""Fletchery: LifeShare's work orchestrator. Docs/Fletchery.md explains it.

    python3 Tools/Fletchery/foreman.py once [--dry-run]     one pass
    python3 Tools/Fletchery/foreman.py run                  keep passing every poll_minutes
    python3 Tools/Fletchery/foreman.py status               print the status text
    python3 Tools/Fletchery/foreman.py plan "<goal>" [--apply]
    python3 Tools/Fletchery/foreman.py config               print the effective config
    python3 Tools/Fletchery/foreman.py demo                 a scripted pass with fakes: shows the shape, touches nothing
    python3 Tools/Fletchery/foreman.py demo --serve         the same, slowed down, with the dashboard on localhost
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import graph  # noqa: E402
from config import Config, PRODUCT_ROOT  # noqa: E402
from github import FakeGitHub, GitHub  # noqa: E402
from loop import Engine  # noqa: E402
from pipeline import Context  # noqa: E402
from repo import FakeRepo, Repo  # noqa: E402
from runtime import AgentSdkRuntime, FakeRuntime  # noqa: E402
from live import Live  # noqa: E402


def lower_priority() -> None:
    """On Windows, Fletchery and every session it starts run below normal: the Build Lane and anything else you are
    running win the CPU."""
    if os.name == "nt":
        try:
            import ctypes
            BELOW_NORMAL = 0x00004000
            ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), BELOW_NORMAL)
        except Exception:
            pass
    else:
        try:
            os.nice(10)
        except Exception:
            pass


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


RESTART_EXIT = 75  # the app (or Start-Fletchery.ps1) starts Fletchery again when it exits with this


def code_fingerprint() -> str:
    """What every file Fletchery runs from contains; a change means new code was installed. Contents, not dates:
    reinstalling or checking out the same code re-dates the files, and that must not drain the workers for nothing."""
    here = Path(__file__).parent
    digest = hashlib.sha256()
    for f in sorted(list(here.glob("*.py")) + list((here / "roles").glob("*.md"))):  # the UI is read per request
        try:
            data = f.read_bytes()
        except OSError:
            continue
        digest.update(f.name.encode() + b"\0" + hashlib.sha256(data).digest())
    return digest.hexdigest()


def take_lock(state_dir: Path) -> Path:
    """One Fletchery loop per state folder (PC rule: never start a second one). A lock whose process is gone is taken over."""
    state_dir.mkdir(parents=True, exist_ok=True)
    # Foreman (LifeShare's copy before it moved here) used foreman.lock in the same folder: never run beside it.
    legacy = state_dir / "foreman.lock"
    if legacy.exists():
        try:
            old_pid = int(legacy.read_text().split()[0])
        except (ValueError, IndexError):
            old_pid = 0
        if pid_alive(old_pid):
            sys.exit(f"Foreman (pid {old_pid}) is still running on this project's state ({legacy}). Stop it first.")
    lock = state_dir / "fletchery.lock"
    if lock.exists():
        try:
            pid = int(lock.read_text().split()[0])
        except (ValueError, IndexError):
            pid = 0
        if pid != os.getpid() and pid_alive(pid):
            sys.exit(f"Fletchery is already running (pid {pid}, lock {lock}). Not starting a second one.")
    lock.write_text(f"{os.getpid()}\n")
    return lock


def real_context(cfg: Config, dry_run: bool) -> Context:
    gh = GitHub(cfg.repo)
    root = Path(cfg.project_root) if cfg.project_root else Path.cwd()
    repo = Repo(root, Path(cfg.worktree_root), cfg.base_branch)
    pool = None
    if dry_run or not cfg.workers_enabled:
        rt = FakeRuntime()
    else:
        plugin = cfg.resolve(cfg.plugin) if cfg.plugin else None
        plugin = plugin if plugin and plugin.exists() else None
        if cfg.agents:
            pool = agent_pool(cfg, root, plugin)
            claude = next((s for s in pool.ordered() if s.kind == "claude"), None)
            rt = pool.runtime(claude.name) if claude else pool.runtime(pool.top().name)  # the planner's
            print("Agents: " + ", ".join(f"{s.label}{'' if s.enabled else ' (off)'}" for s in pool.specs.values()),
                  flush=True)
        else:
            check_auth(cfg)
            rt = AgentSdkRuntime(root, cfg.model or None, plugin_path=plugin, auth=cfg.auth)
    live = Live()
    return Context(cfg, gh, repo, rt, log=lambda line: (print(line, flush=True), live.log(line)), live=live,
                   agents=pool, escalate=cfg.escalate)


def agent_pool(cfg: Config, root: Path, plugin: Path | None, fake=None):
    """The profile's agents (workers.agents) with their runtimes, made on first use."""
    from agents import AgentPool, specs_from
    import clis
    specs = specs_from(cfg.agents, cfg.model)
    for s in specs:  # the old top-level Claude plan / API key switch still decides for an agent that says nothing
        if s.kind == "claude" and not any(a.get("name") == s.name and "auth" in a for a in (cfg.agents or [])):
            s.auth = "key" if cfg.auth == "api" else "plan"
    return AgentPool(specs, cfg.routing, lambda spec: clis.make_runtime(spec, cfg, root, plugin, fake))


def start_dashboard(fm, port: int):
    if not port:
        return None
    from server import Dashboard
    d = Dashboard(fm, port)
    try:
        d.start()
    except OSError as e:
        print(f"Screens not served: port {port} is in use ({e}). Set workers.dashboardPort in the profile.", flush=True)
        return None
    print(f"Screens: http://127.0.0.1:{port}", flush=True)
    return d


async def _serve_only(changed) -> None:
    """Workers off: the screens and the Build Lane panel only. Returns when Fletchery's code changes or a restart is
    asked for (a project switch)."""
    while not changed():
        await asyncio.sleep(2)


def check_auth(cfg: Config) -> None:
    """Fail before any session starts, with the one thing to fix."""
    import auth
    ok, msg = auth.check(cfg.auth)
    if not ok:
        sys.exit(f'auth is "{cfg.auth}": {msg}')
    print(msg + (" (ANTHROPIC_API_KEY ignored)" if cfg.auth == "subscription" else ""), flush=True)


HARBOR = [  # the neutral demo project used for docs and screenshots
    (101, "Add a dark theme to Settings", "", "queued", "2026-10-30T09:00:00Z"),
    (102, "Fix a crash when opening an empty notebook", "", "queued", "2026-10-30T09:01:00Z"),
    (103, "Export a notebook as Markdown", "", "queued", "2026-10-30T09:02:00Z"),
    (104, "Keyboard shortcuts for the sidebar", "Depends on: #101", "queued", "2026-10-30T09:03:00Z"),
    (105, "Show sync errors in the status bar", "", "queued", "2026-10-30T09:04:00Z"),
    (106, "Spell-check in the editor", "", "queued", "2026-10-30T09:05:00Z"),
    (107, "Search inside attachments", "Depends on: #103", "queued", "2026-10-30T09:06:00Z"),
    (108, "Undo for deleted pages", "", "pr", "2026-10-29T09:00:00Z"),
    (109, "Ask before sending crash reports", "", "blocked", "2026-10-29T09:01:00Z"),
    (110, "Remember the last open notebook", "", "queued", "2026-10-30T09:07:00Z"),
    (111, "Tidy the About window", "", "queued", "2026-10-30T09:08:00Z"),
]


def harbor_context(cfg: Config, tmp: Path, serve_delay: float) -> Context:
    from graph import Task
    from github import Feedback, PullRequest
    import gates as g
    graph.PREFIX = "fletchery"
    tasks = {n: Task(n, t, b, ["fletchery", graph.label_for(st)], created_at=c) for n, t, b, st, c in HARBOR}
    gh = FakeGitHub(tasks)
    gh.prs.append(PullRequest(212, "#108 Undo for deleted pages", tasks[108].branch, "main", "abc", ["fletchery", "ready"], "Closes #108"))
    actions = ["Read src/settings/ThemePicker.tsx", "Grep useTheme", "Read src/theme/tokens.ts", "Edit src/theme/tokens.ts",
               "Edit src/settings/ThemePicker.tsx", "$ npm test -- theme", "$ git commit -m \"Settings: dark theme\""]
    rt = FakeRuntime({}, cost_usd=0.9, delay_s=serve_delay, actions=actions)
    live = Live()
    ctx = Context(cfg, gh, FakeRepo(tmp / "worktrees"), rt,
                  run_gates=lambda cmds, cwd, **kw: [g.GateResult(c, True, "RESULT: PASS") for c in cmds],
                  log=lambda line: (print(line), live.log(line)), live=live)
    _demo_agents(cfg, ctx, tmp, rt)
    return ctx


def _demo_agents(cfg: Config, ctx: Context, tmp: Path, rt) -> None:
    if cfg.agents is None:  # the demo shows several agents (all fake) unless the profile names its own
        cfg.agents, cfg.routing = DEMO_AGENTS, DEMO_ROUTING
    if cfg.agents:
        ctx.agents = agent_pool(cfg, tmp, None, fake=rt)
        for spec in ctx.agents.specs.values():
            if spec.enabled:
                ctx.agents.accounts[spec.name] = {"ok": True, "message": "demo: signed in", "at": ""}


def demo_context(cfg: Config, tmp: Path, serve_delay: float = 0.0) -> Context:
    """Six tasks with dependencies, one PR already in the lane with feedback, fake workers. See `demo`."""
    from graph import Task, PR
    from github import Feedback, PullRequest
    import gates as g
    tasks = {
        501: Task(501, "Port ScanUIText to Python", "Gate: Tools/Fletchery/test_foreman.py", ["foreman", "foreman:queued"], created_at="2026-11-01T10:00:00Z"),
        502: Task(502, "Clock 12/24h per player", "Depends on: #501", ["foreman", "foreman:queued"], created_at="2026-11-01T10:01:00Z"),
        503: Task(503, "Player mark beside lobby colours", "", ["foreman", "foreman:queued"], created_at="2026-11-01T10:02:00Z"),
        504: Task(504, "Radial menu: mouse-only audit", "Depends on: #503", ["foreman", "foreman:queued"], created_at="2026-11-01T10:03:00Z"),
        505: Task(505, "Save v12 migration", "Depends on: #499", ["foreman", "foreman:queued"], created_at="2026-11-01T10:04:00Z"),
        506: Task(506, "Pronoun pass on LSStrings.csv", "", ["foreman", "foreman:pr"], created_at="2026-10-31T10:00:00Z"),
        507: Task(507, "Fix sound test door block", "", ["foreman", "foreman:queued"], created_at="2026-11-01T10:05:00Z"),
    }
    gh = FakeGitHub(tasks, closed={499})
    gh.prs.append(PullRequest(880, "#506 Pronoun pass on LSStrings.csv", tasks[506].branch, "Test-Staging", "abc", ["foreman", "ready"], "Closes #506"))
    gh.feedback[880] = [Feedback(880, "comment", "Build Lane: taken out of batch 41: FAIL ScanUIText gendered pronoun in Feed.Hungry", "2026-11-02T01:00:00Z")]
    script = {("reviewer", 503): {"verdict": "needs_work", "findings": [{"file": "Source/LSUI/Private/LSLobby.cpp", "line": 88, "rule": "player-mark", "note": "colour swatch has no ULSPlayerMark glyph"}]},
              ("worker", 507): {"summary": "", "files": [], "build_steps": [], "checks": [], "unverified": [], "gave_up": "the door layout is a design decision"}}
    rt = FakeRuntime(script, cost_usd=1.25, delay_s=serve_delay,
                     actions=["Read Docs/UI.md", "Grep LSPlayerMark", "Read Source/LSUI/Private/LSLobby.cpp",
                              "Edit Source/LSUI/Private/LSLobby.cpp", "Edit Content/Strings/LSStrings.csv",
                              "$ python3 Tools/ScanUIText.py", "$ git commit -m \"Lobby: player mark beside colours\""]
                     if serve_delay else None)
    repo = FakeRepo(tmp / "worktrees")
    live = Live()
    ctx = Context(cfg, gh, repo, rt, run_gates=lambda cmds, cwd, **kw: [g.GateResult(c, True, "RESULT: PASS") for c in cmds],
                  log=lambda line: (print(line), live.log(line)), live=live)
    _demo_agents(cfg, ctx, tmp, rt)
    return ctx


DEMO_AGENTS = [{"name": "opus", "kind": "claude", "model": "claude-opus-5-5", "tier": 5, "vendor": "anthropic"},
               {"name": "sonnet", "kind": "claude", "model": "claude-sonnet-5-5", "tier": 4, "vendor": "anthropic"},
               {"name": "codex-astra", "kind": "codex", "model": "gpt-6-astra", "tier": 4, "vendor": "openai", "enabled": False},
               {"name": "codex-luna", "kind": "codex", "model": "gpt-6-luna", "tier": 1, "vendor": "openai", "enabled": False},
               {"name": "haiku", "kind": "opencode", "model": "opencode-go/claude-haiku-5-5", "tier": 3, "vendor": "anthropic"},
               {"name": "deepseek-pro", "kind": "opencode", "model": "opencode-go/deepseek-v4-pro", "tier": 3, "vendor": "deepseek"},
               {"name": "deepseek", "kind": "opencode", "model": "opencode-go/deepseek-v4.1-flash", "tier": 2, "vendor": "deepseek"}]
DEMO_ROUTING = {"mode": "scores"}


def utf8_console() -> None:
    """Windows consoles default to cp1252, which cannot print the status text's arrows (seen on Alex's PC 2026-11-02)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    utf8_console()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["once", "run", "status", "plan", "config", "demo"])
    ap.add_argument("goal", nargs="?", default="")
    ap.add_argument("--dry-run", action="store_true", help="no sessions, no GitHub writes; print what a pass would do")
    ap.add_argument("--apply", action="store_true", help="plan: create the issues instead of drafting them")
    ap.add_argument("--profile", "--config", dest="profile", type=Path, default=None,
                    help="the project profile (.json); old Foreman configs load too")
    ap.add_argument("--serve", action="store_true", help="demo: serve the dashboard on the demo data and keep running")
    ap.add_argument("--fixture", choices=["lifeshare", "harbor"], default="lifeshare",
                    help="demo: which made-up project (harbor is the neutral one used for docs and screenshots)")
    a = ap.parse_args(argv)
    if a.command == "run":  # the project the app last switched to (Settings > Projects) wins over the launcher's
        import projects
        act = projects.active_profile()
        if act and (not a.profile or act.resolve() != Path(a.profile).resolve()):
            print(f"Running the project chosen in the app: {act}", flush=True)
            a.profile = act
    cfg = Config.load(a.profile)
    graph.PREFIX = cfg.label

    if a.command == "config":
        print(cfg.dump())
        return 0

    if a.command == "demo" and a.serve:
        import tempfile
        d = Path(tempfile.mkdtemp())
        if a.fixture == "harbor":
            import json as _js
            from datetime import datetime as _dt2, timedelta as _td2, timezone as _tz2
            cfg.repo, cfg.name, cfg.base_branch = "example/harbor", "Harbor", "main"
            n2 = _dt2.now(_tz2.utc)
            (d / "status.json").write_text(_js.dumps({
                "what": "batch #212, #214", "step": "Tests: integration", "detail": "9 of 14 done", "done": "", "phase": 4,
                "phases": ["Wait for the PC", "Quick checks", "Compile", "Package assets", "Tests", "Package"],
                "progress": {"done": 9, "total": 14}, "started": (n2 - _td2(minutes=23)).isoformat(),
                "updated": (n2 - _td2(seconds=40)).isoformat(), "runUrl": "https://github.com/example/harbor/actions",
                "issueUrl": "https://github.com/example/harbor/issues", "notes": [],
                "queue": [{"number": 212, "title": "#108 Undo for deleted pages", "state": "in the batch"},
                          {"number": 214, "title": "Faster startup on large notebooks", "state": "in the batch"},
                          {"number": 215, "title": "Update the bundled fonts", "state": "waiting for review"},
                          {"number": 216, "title": "Print a page range", "state": "waits for the next batch"}]}), encoding="utf-8")
            cfg.lane_status_file, cfg.lane_pause_file, cfg.lane_drive = str(d / "status.json"), str(d / "PAUSED"), cfg.lane_drive or ""
            ctx = harbor_context(cfg, d, serve_delay=600.0)
        else:
            cfg.repo = cfg.repo or "Stonefletcher-Productions-LLC/LifeShare"
            graph.PREFIX = "foreman"
            ctx = demo_context(cfg, d, serve_delay=40.0)
        fm = Engine(ctx, d / "state")
        fm.state.accounts_checked = "2999-01-01T00:00:00+00:00"  # the demo's agents are fake: never ask the real CLIs
        import json as _j
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz
        (d / "state").mkdir(parents=True, exist_ok=True)
        with (d / "state" / "ledger.jsonl").open("w", encoding="utf-8") as f:
            for back, cost, n in [(6, 6.4, 3), (5, 11.2, 5), (4, 0, 0), (3, 18.9, 8), (2, 9.5, 4), (1, 22.3, 9), (0, 4.1, 2)]:  # made up
                day = _dt.now(_tz.utc) - _td(days=back)
                fm.state.spent[day.strftime("%Y-%m-%d")] = cost
                for k in range(n):
                    f.write(_j.dumps({"at": day.isoformat(), "task": 600 + k, "result": "pr", "cost_usd": cost / max(n, 1),
                                      "sessions": ["worker:x", "reviewer:y"]}) + "\n")
        start_dashboard(fm, cfg.dashboard_port)
        try:
            asyncio.run(fm.run_forever())
        except KeyboardInterrupt:
            pass
        return 0

    if a.command == "demo":
        import tempfile
        graph.PREFIX = "foreman"
        with tempfile.TemporaryDirectory() as d:
            ctx = demo_context(cfg, Path(d))
            fm = Engine(ctx, Path(d) / "state")
            fm.state.accounts_checked = "2999-01-01T00:00:00+00:00"
            print("== pass 1 ==")
            asyncio.run(fm.once())
            print(ctx.gh.status_text)
            print("\n== GitHub writes ==")
            print("\n".join(ctx.gh.writes))
            print("\n== sessions ==")
            print("\n".join(f"{r} #{n}" for r, n, _ in ctx.runtime.calls))
        return 0

    lower_priority()
    ctx = real_context(cfg, a.dry_run)
    fm = Engine(ctx, Path(cfg.state_dir), dry_run=a.dry_run)
    if a.command == "once":
        asyncio.run(fm.once())
        if a.dry_run:
            print("\n".join(fm.last_plan) or "(nothing to do)")
        return 0
    if a.command == "run":
        lock = take_lock(Path(cfg.state_dir))
        if cfg.workers_enabled:
            print(f"Fletchery running for {cfg.name}: pid {os.getpid()}, every {cfg.poll_minutes} min, "
                  f"{cfg.max_parallel} workers, auth {cfg.auth}.", flush=True)
        else:
            print(f"Fletchery running for {cfg.name}: pid {os.getpid()}, Build Lane only (workers off).", flush=True)
        dash = start_dashboard(fm, cfg.dashboard_port)
        start_code = code_fingerprint()
        changed = lambda: bool(fm.restart_reason) or code_fingerprint() != start_code  # noqa: E731
        restart = False
        try:
            if cfg.workers_enabled:
                asyncio.run(fm.run_forever(code_changed=changed))
            else:
                asyncio.run(_serve_only(changed))
            restart = changed()
        except KeyboardInterrupt:
            pass
        finally:
            if dash:
                dash.stop()
            try:
                if lock.read_text().split()[0] == str(os.getpid()):
                    lock.unlink()
            except (OSError, IndexError):
                pass
        return RESTART_EXIT if restart else 0
    if a.command == "status":
        tasks = ctx.gh.open_tasks()
        prs = {p.number: p for p in ctx.gh.open_prs()}
        from graph import readiness
        rd = readiness(tasks, set())
        print(fm.status_text(tasks, prs, rd.waiting, []))
        return 0
    if a.command == "plan":
        if not a.goal:
            sys.exit("plan needs a goal")
        issues = asyncio.run(fm.plan(a.goal, apply=a.apply))
        for i, it in enumerate(issues):
            print(f"[{i}] {it['title']} (depends on {it.get('depends_on', [])})")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
