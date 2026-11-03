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
    """Size and time of every file Fletchery runs from; a change means new code was pulled."""
    here = Path(__file__).parent
    parts = []
    for f in sorted(list(here.glob("*.py")) + list((here / "roles").glob("*.md"))):  # the UI is read per request
        try:
            st = f.stat()
            parts.append(f"{f.name}:{st.st_size}:{int(st.st_mtime)}")
        except OSError:
            pass
    return "|".join(parts)


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
    if dry_run or not cfg.workers_enabled:
        rt = FakeRuntime()
    else:
        check_auth(cfg)
        plugin = cfg.resolve(cfg.plugin) if cfg.plugin else None
        rt = AgentSdkRuntime(root, cfg.model or None, plugin_path=plugin if plugin and plugin.exists() else None, auth=cfg.auth)
    live = Live()
    return Context(cfg, gh, repo, rt, log=lambda line: (print(line, flush=True), live.log(line)), live=live)


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
    """Workers off: the screens and the Build Lane panel only. Returns when Fletchery's code changes."""
    while not changed():
        await asyncio.sleep(30)


def check_auth(cfg: Config) -> None:
    """Fail before any session starts, with the one thing to fix."""
    import auth
    ok, msg = auth.check(cfg.auth)
    if not ok:
        sys.exit(f'auth is "{cfg.auth}": {msg}')
    print(msg + (" (ANTHROPIC_API_KEY ignored)" if cfg.auth == "subscription" else ""), flush=True)


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
    return ctx


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
    a = ap.parse_args(argv)
    cfg = Config.load(a.profile)
    graph.PREFIX = cfg.label

    if a.command == "config":
        print(cfg.dump())
        return 0

    if a.command == "demo" and a.serve:
        import tempfile
        d = Path(tempfile.mkdtemp())
        cfg.repo = cfg.repo or "Stonefletcher-Productions-LLC/LifeShare"
        graph.PREFIX = "foreman"
        ctx = demo_context(cfg, d, serve_delay=40.0)
        fm = Engine(ctx, d / "state")
        import json as _j
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz
        (d / "state").mkdir(parents=True, exist_ok=True)
        with (d / "state" / "ledger.jsonl").open("w", encoding="utf-8") as f:
            for back, cost, n in [(6, 6.4, 3), (5, 11.2, 5), (4, 0, 0), (3, 18.9, 8), (2, 9.5, 4), (1, 22.3, 9), (0, 4.1, 2)]:
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
        changed = lambda: code_fingerprint() != start_code  # noqa: E731
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
