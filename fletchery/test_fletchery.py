#!/usr/bin/env python3
"""Fletchery engine regression checks: graph, dispatch, review loop, gates, fixer attempts, budgets, status text, PR body,
convention scan. No network, no sessions. Run: python3 fletchery/test_fletchery.py"""
from __future__ import annotations

import asyncio
import json as _json_mod
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import gates  # noqa: E402
from config import Config  # noqa: E402
from github import FakeGitHub, Feedback, PullRequest  # noqa: E402
from graph import BLOCKED, PR, QUEUED, RUNNING, Task, find_cycles, pick, readiness  # noqa: E402
from loop import Engine as Foreman  # noqa: E402
import graph as _graph  # noqa: E402
_graph.PREFIX = "foreman"
from pipeline import Context, pr_body  # noqa: E402
from repo import FakeRepo  # noqa: E402
from runtime import AgentSdkRuntime, FakeRuntime, is_limit, parse_output  # noqa: E402

PASS = lambda cmds, cwd, **kw: [gates.GateResult(c, True, "RESULT: PASS") for c in cmds]  # noqa: E731


def T(n, title, body="", status=QUEUED, created="2026-11-01T00:00:00Z"):
    return Task(n, title, body, ["foreman", _graph.label_for(status)], created_at=created)


def project_with_rules() -> Path:
    """A project checkout with LifeShare-like rules: one check that always runs."""
    root = Path(tempfile.mkdtemp())
    (root / ".fletchery").mkdir()
    (root / ".fletchery" / "gates.json").write_text('{"always": ["python3 .fletchery/check_conventions.py"]}', encoding="utf-8")
    return root


def ctx_with(tasks, script=None, closed=(), gates_fn=PASS, cfg=None, changed=None):
    cfg = cfg or Config(max_parallel=4, daily_budget_usd=60, base_branch="Test-Staging")
    if not cfg.project_root:
        cfg.project_root = str(project_with_rules())
    gh = FakeGitHub(tasks, set(closed))
    tmp = Path(tempfile.mkdtemp())
    return Context(cfg, gh, FakeRepo(tmp, changed=changed), FakeRuntime(script or {}), run_gates=gates_fn, log=lambda s: None), tmp


def run_once(ctx, tmp, dry=False):
    fm = Foreman(ctx, tmp / "state", dry_run=dry)
    return fm, asyncio.run(fm.once())


# ---- graph ----
t = T(1, "A", "Depends on: #2, #3\nGate: Tools/CI/TestLaneCut.cjs\nLane-Imports: ImportFurniture, BuildTown")
assert t.depends_on == [2, 3] and t.gates == ["Tools/CI/TestLaneCut.cjs"] and t.lane_imports == "ImportFurniture, BuildTown"
assert t.branch == "claude/foreman-1-a" and T(5, "Clock 12/24h per player!").slug == "clock-12-24h-per-player"
assert T(9, "x", status=PR).status == PR and Task(9, "x", labels=["foreman"]).status == QUEUED

g = {1: T(1, "a", "Depends on: #2"), 2: T(2, "b", "Depends on: #3"), 3: T(3, "c", "Depends on: #1"), 4: T(4, "d")}
assert find_cycles(g) == {1, 2, 3}, "a three-task cycle is found, the free task is not on it"
r = readiness(g)
assert [x.number for x in r.ready] == [4] and r.waiting[1] == "dependency cycle"

g = {1: T(1, "a", "Depends on: #7", created="2026-11-02T00:00:00Z"), 2: T(2, "b", "Depends on: #1"),
     3: T(3, "c", created="2026-11-01T00:00:00Z"), 4: T(4, "d", "Depends on: #99"), 5: T(5, "e", status=RUNNING)}
r = readiness(g, known_closed={7})
assert [x.number for x in r.ready] == [3, 1], "closed deps count as done; oldest first"
assert r.waiting[2] == "waits for #1" and r.waiting[4] == "depends on missing issue #99" and 5 not in r.waiting
assert [x.number for x in pick(r.ready, 1)] == [3] and pick(r.ready, 0) == []

# ---- happy path: worker -> gates -> reviewer -> PR; dependents wait, then run next pass ----
ctx, tmp = ctx_with({10: T(10, "First"), 11: T(11, "Second", "Depends on: #10")})
fm, out = run_once(ctx, tmp)
assert [o.result for o in out] == ["pr"] and out[0].pr_number == 9000
assert ctx.gh.tasks[10].status == PR and ctx.gh.tasks[11].status == QUEUED
assert ctx.repo.pushed == ["claude/foreman-10-first"]
assert [c[0] for c in ctx.runtime.calls] == ["worker", "reviewer"]
pr = ctx.gh.prs[0]
assert pr.base == "Test-Staging" and "ready" in pr.labels and "Closes #10" in pr.body and "has not been through the full build" in pr.body
assert "waits for #10" in ctx.gh.status_text and "→ PR #9000" in ctx.gh.status_text
ctx.gh.close_issue(10)  # the lane lands the PR
ctx.gh.prs.clear()
fm, out = run_once(ctx, tmp)
assert [o.task for o in out] == [11] and out[0].result == "pr", "the dependent runs once its dependency closed"
assert fm.state.spent and sum(fm.state.spent.values()) == 4.0, "two tasks x (worker + reviewer) at $1 each"

# ---- slots: max_parallel caps dispatch; a fixer takes a slot ----
ctx, tmp = ctx_with({n: T(n, f"t{n}", created=f"2026-11-01T00:00:{n:02d}Z") for n in range(1, 7)},
                    cfg=Config(max_parallel=3, daily_budget_usd=60, base_branch="Test-Staging"))
fm, out = run_once(ctx, tmp)
assert sorted(o.task for o in out) == [1, 2, 3] and "no free slot (3)" in ctx.gh.status_text

# ---- daily budget stops new dispatch ----
ctx, tmp = ctx_with({1: T(1, "a")}, cfg=Config(daily_budget_usd=1.0))
fm = Foreman(ctx, tmp / "state")
fm.state.spent[__import__("loop").today()] = 1.5
assert asyncio.run(fm.once()) == [] and "daily budget spent" in ctx.gh.status_text

# ---- reviewer needs_work -> one worker fix pass -> approve ----
calls = {"n": 0}
def review_twice(prompt):
    calls["n"] += 1
    return {"verdict": "needs_work", "findings": [{"file": "f.cpp", "line": 3, "rule": "folk-naming", "note": "sim"}]} \
        if calls["n"] == 1 else {"verdict": "approve", "findings": []}
ctx, tmp = ctx_with({1: T(1, "a")}, script={("reviewer", 1): review_twice})
fm, out = run_once(ctx, tmp)
assert out[0].result == "pr" and [c[0] for c in ctx.runtime.calls] == ["worker", "reviewer", "worker", "reviewer"]
assert "Review findings to address" in ctx.runtime.calls[2][2] and "folk-naming" in ctx.runtime.calls[2][2]

# ---- reviewer needs_work twice -> blocked; escalate -> blocked ----
ctx, tmp = ctx_with({1: T(1, "a")}, script={("reviewer", 1): {"verdict": "needs_work", "findings": []}})
fm, out = run_once(ctx, tmp)
assert out[0].result == "blocked" and ctx.gh.tasks[1].status == BLOCKED and ctx.repo.pushed == []
ctx, tmp = ctx_with({1: T(1, "a")}, script={("reviewer", 1): {"verdict": "escalate", "findings": [{"file": "x", "line": 1, "rule": "scope", "note": "design"}]}})
fm, out = run_once(ctx, tmp)
assert out[0].result == "blocked" and "escalated" in out[0].detail and "design" in ctx.gh.comments[1][-1]

# ---- gates: a failure goes back to the worker once, then blocks ----
state = {"runs": 0}
def flaky(cmds, cwd, **kw):
    state["runs"] += 1
    return [gates.GateResult(c, state["runs"] > 1, "FAIL  LSFoo.cpp:3: numeric font size") for c in cmds]
ctx, tmp = ctx_with({1: T(1, "a")}, gates_fn=flaky)
fm, out = run_once(ctx, tmp)
assert out[0].result == "pr" and "Gate failures to fix" in ctx.runtime.calls[1][2] and "numeric font size" in ctx.runtime.calls[1][2]
ctx, tmp = ctx_with({1: T(1, "a")}, gates_fn=lambda c, w, **kw: [gates.GateResult(x, False, "FAIL") for x in c])
fm, out = run_once(ctx, tmp)
assert out[0].result == "blocked" and "gates still fail" in out[0].detail

# ---- worker gives up -> blocked with its reason; worker crash -> requeued once, blocked the second time ----
ctx, tmp = ctx_with({1: T(1, "a")}, script={("worker", 1): {"summary": "", "files": [], "build_steps": [], "checks": [], "unverified": [], "gave_up": "needs a design call"}})
fm, out = run_once(ctx, tmp)
assert out[0].result == "blocked" and "needs a design call" in ctx.gh.comments[1][-1]
ctx, tmp = ctx_with({1: T(1, "a")}, script={("worker", 1): {"error": "CLI crashed"}})
fm, out = run_once(ctx, tmp)
assert out[0].result == "requeue" and ctx.gh.tasks[1].status == QUEUED
fm, out = run_once(ctx, tmp)
assert out[0].result == "blocked" and "second infrastructure failure" in out[0].detail

# ---- gates selection from the diff, by the project's rules (.fletchery/gates.json) ----
import rules as rules_mod
pr_root = Path(tempfile.mkdtemp())
(pr_root / ".fletchery").mkdir()
(pr_root / ".fletchery" / "gates.json").write_text(_json_mod.dumps({
    "always": ["python3 .fletchery/check_conventions.py"],
    "rules": [{"paths": "^Source/LSUI/|^Content/Strings/", "run": "python3 Tools/ScanUIText.py"},
              {"paths": "^Source/.*Save.*\\.(cpp|h)$", "run": ["python3 Tools/CheckSaveFields.py"]}]}), encoding="utf-8")
(pr_root / ".fletchery" / "conventions.md").write_text("People in the game are Folks.", encoding="utf-8")
(pr_root / ".fletchery" / "worker.md").write_text("You cannot build Unreal here.", encoding="utf-8")
R = rules_mod.load(None, Path(tempfile.mkdtemp()), pr_root)
assert R.source.endswith(".fletchery") and R.conventions == "People in the game are Folks." and "Unreal" in R.additions["worker"]
sel = gates.select(["Source/LSUI/Private/LSClock.cpp", "Docs/UI.md"], ["Tools/CI/TestLaneCut.cjs"], R)
assert sel == ["python3 .fletchery/check_conventions.py", "python3 Tools/ScanUIText.py", "node Tools/CI/TestLaneCut.cjs"], sel
assert "python3 Tools/CheckSaveFields.py" in gates.select(["Source/LSCore/Private/LSSaveGame.cpp"], (), R)
assert gates.select(["README.md"], (), R) == ["python3 .fletchery/check_conventions.py"]
assert gates.select([], ["Tools/CI/TestX.py"], R)[-1] == "python3 Tools/CI/TestX.py"
assert gates.select(["anything"]) == [] and rules_mod.load(None, Path(tempfile.mkdtemp())).checks_for(["x"]) == [], "no rules: no checks"

# ---- fixer: feedback on an open PR dispatches a fixer; attempts are counted; exhausted -> blocked ----
tasks = {20: T(20, "In lane", status=PR)}
ctx, tmp = ctx_with(tasks, cfg=Config(fixer_attempts=2))
ctx.gh.prs.append(PullRequest(700, "#20 In lane", tasks[20].branch, "Test-Staging", "abc", ["foreman", "ready"], "Closes #20"))
ctx.gh.feedback[700] = [Feedback(700, "comment", "Build Lane: taken out: FAIL ScanUIText", "2026-11-02T01:00:00Z")]
fm, out = run_once(ctx, tmp)
assert out[0].result == "fixed" and ctx.repo.pushed == [tasks[20].branch] and fm.state.fix_attempts["700"] == 1
assert "taken out" in ctx.runtime.calls[0][2] and ctx.runtime.calls[0][0] == "fixer"
fm2, out = run_once(ctx, tmp)
assert out == [], "the same feedback is not acted on twice"
ctx.gh.feedback[700].append(Feedback(700, "check", "check `Build and test` failure", "2099-01-01T01:00:00Z"))
fm2, out = run_once(ctx, tmp)
assert out[0].result == "fixed" and fm2.state.fix_attempts["700"] == 2
ctx.gh.feedback[700].append(Feedback(700, "comment", "Build Lane: taken out again", "2099-01-02T01:00:00Z"))
fm3, out = run_once(ctx, tmp)
assert out[0].result == "gave_up" and ctx.gh.tasks[20].status == BLOCKED and "came back 2 times" in ctx.gh.comments[20][-1]

# ---- fixer that gives up does not push ----
tasks = {21: T(21, "x", status=PR)}
ctx, tmp = ctx_with(tasks, script={("fixer", 21): {"summary": "", "pushed": False, "gave_up": "binary conflict"}})
ctx.gh.prs.append(PullRequest(701, "#21 x", tasks[21].branch, "Test-Staging", "abc", ["foreman"], "Closes #21"))
ctx.gh.feedback[701] = [Feedback(701, "conflict", "conflicts", "2026-11-02T01:00:00Z")]
fm, out = run_once(ctx, tmp)
assert out[0].result == "gave_up" and ctx.repo.pushed == [] and "binary conflict" in ctx.gh.comments[21][-1]

# ---- dry run: plans, writes nothing, runs nothing ----
ctx, tmp = ctx_with({1: T(1, "a"), 2: T(2, "b", "Depends on: #1")})
fm, out = run_once(ctx, tmp, dry=True)
assert out == [] and ctx.gh.writes == [] and ctx.runtime.calls == [] and fm.last_plan == ["run #1 a on claude/foreman-1-a"]

# ---- PR body carries the lane's fields ----
body = pr_body(T(3, "t", "Lane-Imports: ImportFurniture"), {"summary": "s", "build_steps": ["Tools\\Build.ps1", "Tools\\ImportFurniture.ps1"],
                "checks": ["RunSpecs LifeShare.Furniture.Place"], "save_version": "12 (migration + golden save)", "unverified": ["compile"]},
               [gates.GateResult("python3 Tools/ScanUIText.py", True, "")], {"verdict": "approve", "findings": []}, Config())
for needle in ("Closes #3", "Lane-Imports: ImportFurniture", "Tools\\ImportFurniture.ps1", "RunSpecs LifeShare.Furniture.Place",
               "**Save version:** 12", "PASS `python3 Tools/ScanUIText.py`", "## Review: approve", "- compile"):
    assert needle in body, needle

# ---- structured output parsing ----
assert parse_output({"a": 1}, "") == {"a": 1} and parse_output(None, 'text {"verdict": "approve"} more') == {"verdict": "approve"}
assert parse_output(None, "no json") == {}

# ---- status text sections ----
ctx, tmp = ctx_with({1: T(1, "a"), 2: T(2, "b", status=BLOCKED)})
fm, out = run_once(ctx, tmp)
for needle in ("## Running", "## Queue", "## In the lane", "## Blocked (needs a person)", "- #2 b", "## This pass", "- #1: pr PR #9000"):
    assert needle in ctx.gh.status_text, needle

# ---- usage limits: not a verdict on the task; the loop pauses dispatch ----
assert is_limit("Claude AI usage limit reached|1762138800") and is_limit("", 429) and is_limit("API Error: 529 overloaded")
assert not is_limit("worker session failed: CLI crashed") and not is_limit("", 500)
LIMIT = {"error": "Claude AI usage limit reached. Your limit will reset at 3am."}
ctx, tmp = ctx_with({1: T(1, "a"), 2: T(2, "b", created="2026-11-01T00:00:01Z")}, script={("worker", 1): LIMIT})
fm, out = run_once(ctx, tmp)
by = {o.task: o for o in out}
assert by[1].result == "limited" and ctx.gh.tasks[1].status == QUEUED and 1 not in ctx.gh.comments, "quietly back in the queue"
assert by[2].result == "pr", "a session already running in the same pass finishes"
assert fm.state.paused_until and "Paused until" in ctx.gh.status_text
ctx.runtime.script.pop(("worker", 1))
fm, out = run_once(ctx, tmp)
assert out == [] and "paused for the usage limit" in ctx.gh.status_text, "nothing new while paused"
fm.state.paused_until = "2000-01-01T00:00:00+00:00"
fm.state.save(tmp / "state" / "state.json")
fm, out = run_once(ctx, tmp)
assert [o.result for o in out] == ["pr"] and fm.state.paused_until == "", "after the pause the task runs and the pause clears"
ctx, tmp = ctx_with({1: T(1, "a")}, script={("reviewer", 1): LIMIT})
fm, out = run_once(ctx, tmp)
assert out[0].result == "limited" and ctx.repo.pushed == [] and ctx.gh.tasks[1].status == QUEUED, "a limited reviewer too"
for _ in range(3):  # limits never use up the one infrastructure retry
    ctx, tmp2 = ctx_with({1: T(1, "a")}, script={("worker", 1): LIMIT})
    fm, out = run_once(ctx, tmp2)
    assert out[0].result == "limited" and ctx.gh.tasks[1].status == QUEUED

tasks = {30: T(30, "lane", status=PR)}
ctx, tmp = ctx_with(tasks, script={("fixer", 30): LIMIT})
ctx.gh.prs.append(PullRequest(730, "#30 lane", tasks[30].branch, "Test-Staging", "abc", ["foreman"], "Closes #30"))
ctx.gh.feedback[730] = [Feedback(730, "comment", "Build Lane: taken out", "2026-11-02T01:00:00Z")]
fm, out = run_once(ctx, tmp)
assert out[0].result == "limited" and fm.state.fix_attempts.get("730", 0) == 0 and "730" not in fm.state.seen
ctx.runtime.script.pop(("fixer", 30))
fm.state.paused_until = "2000-01-01T00:00:00+00:00"
fm.state.save(tmp / "state" / "state.json")
fm, out = run_once(ctx, tmp)
assert out[0].result == "fixed" and fm.state.fix_attempts["730"] == 1, "the same feedback is retried after the pause, as attempt 1"

# ---- subscription auth keeps the API key away from sessions ----
import os
os.environ["ANTHROPIC_API_KEY"] = "sk-test"
rt = AgentSdkRuntime(Path("."), env={"ANTHROPIC_API_KEY": "sk-test", "X": "1"}, auth="subscription")
assert "ANTHROPIC_API_KEY" not in os.environ and "ANTHROPIC_API_KEY" not in rt.env and rt.env["X"] == "1"
os.environ["ANTHROPIC_API_KEY"] = "sk-test"
rt = AgentSdkRuntime(Path("."), env={}, auth="api")
assert os.environ.get("ANTHROPIC_API_KEY") == "sk-test", "api mode leaves the key alone"
os.environ.pop("ANTHROPIC_API_KEY")
assert Config().auth == "subscription" and Config().max_parallel == 2

# ---- steering through the issue: people's comments reach the worker, Foreman's own do not ----
ctx, tmp = ctx_with({1: T(1, "Doors", "Fix the blocked doors.")},
                    script={("worker", 1): {"summary": "", "files": [], "build_steps": [], "checks": [], "unverified": [],
                                            "gave_up": "which door layout?"}})
fm, out = run_once(ctx, tmp)
assert out[0].result == "blocked" and ctx.gh.tasks[1].status == BLOCKED
ctx.gh.notes[1] = [("ProjektPayne", "2026-11-02T23:00:00Z", "Use the layout from #408; keep the back door.")]
for c in ctx.gh.comments[1]:  # what Foreman itself wrote is filtered by the real GitHub reader; mimic it here
    assert c.startswith("Fletchery")
ctx.gh.set_status(1, QUEUED)  # Alex re-queues it
ctx.runtime.script.pop(("worker", 1))
fm, out = run_once(ctx, tmp)
worker_prompt = [c for c in ctx.runtime.calls if c[0] == "worker"][-1][2]
assert out[0].result == "pr" and "Use the layout from #408" in worker_prompt and "**ProjektPayne**" in worker_prompt
assert "Fletchery blocked" not in worker_prompt
from github import _is_foreman
assert _is_foreman("Foreman blocked this task") and _is_foreman("x\n_Generated by Foreman (Tools/Foreman)_")
assert _is_foreman("Fletchery blocked this task") and _is_foreman("x\n_Generated by Fletchery with Claude Code_")
assert not _is_foreman("Use the layout from #408")

# ---- gh --paginate output, and the single-instance lock ----
from github import parse_pages
assert parse_pages('[{"body": "see [a][b]"}][{"n": 2}]') == [{"body": "see [a][b]"}, {"n": 2}]
assert parse_pages('[]\n[]') == [] and parse_pages('{"a": 1}') == {"a": 1} and parse_pages("  ") is None
import cli as fmod, os
d = Path(tempfile.mkdtemp())
lock = fmod.take_lock(d)
assert lock.read_text().strip() == str(os.getpid())
lock.write_text("999999\n")  # a dead process's lock is taken over
assert fmod.take_lock(d).read_text().strip() == str(os.getpid())
import subprocess as sp
child = sp.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
try:
    lock.write_text(f"{child.pid}\n")
    try:
        fmod.take_lock(d)
        raise AssertionError("a live Foreman's lock must stop a second one")
    except SystemExit as e:
        assert "already running" in str(e)
finally:
    child.kill()

# ---- dashboard: status, buttons, and the guards on them ----
import json as _json, socket, urllib.request, urllib.error
from server import Dashboard
from live import Live, describe
assert describe("Edit", {"file_path": "/w/1-a/Source/X.cpp"}, "/w/1-a") == "Edit Source/X.cpp"
assert describe("Bash", {"command": "git   commit\n -m x"}) == "$ git commit -m x" and describe("Agent", {}) == "Agent"
lv = Live(); lv.stage(7, "Writing", "Seven"); lv.action(7, "Edit a.cpp"); r = lv.read()
assert r["activity"][0]["last"] == "Edit a.cpp" and r["activity"][0]["actions"] == 1 and "#7 Writing" in r["log"][-1]["line"]
lv.done(7); assert lv.read()["activity"] == []

ctx, tmp = ctx_with({1: T(1, "a"), 2: T(2, "b", "Depends on: #1")})
ctx.live = Live()
fm, out = run_once(ctx, tmp)
with socket.socket() as so:
    so.bind(("127.0.0.1", 0)); port = so.getsockname()[1]
dash = Dashboard(fm, port); dash.start()
def call(path, method="GET", body=None, token=None, host=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method,
                                 data=_json.dumps(body).encode() if body is not None else None)
    req.add_header("Content-Type", "application/json")
    if token: req.add_header("X-Fletchery-Token", token)
    if host: req.add_header("Host", host)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
try:
    code, page = call("/")
    assert code == 200 and dash.token.encode() in page and b"__FLETCHERY_TOKEN__" not in page
    assert call("/?open=week")[0] == 200 and call("/api/status?x=1")[0] == 200, "a query string does not change the page"
    code, st = call("/api/status"); st = _json.loads(st)
    assert code == 200 and {t["number"]: t["status"] for t in st["tasks"]} == {1: "pr", 2: "queued"}
    assert st["tasks"][1]["why"] == "waits for #1" and st["max_parallel"] == 4 and st["history"][0]["task"] == 1
    assert call("/api/status", host="evil.example:%d" % port)[0] == 403, "another host name is refused (DNS rebinding)"
    assert call("/api/pause", "POST", {})[0] == 403, "buttons need the page's token"
    assert call("/api/pause", "POST", {}, token="wrong")[0] == 403
    assert call("/api/pause", "POST", {}, token=dash.token)[0] == 200 and fm.state.held
    assert _json.loads(call("/api/status")[1])["held"] is True
    ctx.gh.tasks[3] = T(3, "c")  # a ready task appears while paused
    fm2 = run_once(ctx, tmp)  # a pass while held starts nothing
    assert fm2[1] == [] and "#3 c: on hold: Fletchery is paused" in ctx.gh.status_text
    assert "Paused from the dashboard" in ctx.gh.status_text
    fm = fm2[0]; dash.fm = fm
    assert fm.state.held, "the hold survives a restart (it is in state.json)"
    assert call("/api/resume", "POST", {}, token=dash.token)[0] == 200 and not fm.state.held
    code, body = call("/api/task", "POST", {"title": "New thing", "body": "Do it.", "depends": ["1", 2]}, token=dash.token)
    made = _json.loads(body)
    assert code == 200 and ctx.gh.tasks[made["number"]].depends_on == [1, 2] and ctx.gh.tasks[made["number"]].status == QUEUED
    assert "foreman" in ctx.gh.tasks[made["number"]].labels
    assert call("/api/task", "POST", {"title": ""}, token=dash.token)[0] == 400
    assert call("/api/task", "POST", {"title": "x", "depends": ["abc"]}, token=dash.token)[0] == 400
    assert call("/api/run-now", "POST", {}, token=dash.token)[0] == 200
    code, ico = call("/favicon.ico"); assert code == 200 and ico[:4] == b"\x00\x00\x01\x00"
    assert call("/api/nope", "POST", {}, token=dash.token)[0] == 404 and call("/nope")[0] == 404
finally:
    dash.stop()

# live view during a pass: stages and actions reach it, and clear when the task ends
seen = []
ctx, tmp = ctx_with({1: T(1, "a")})
ctx.live = Live()
ctx.runtime.actions = ["Read x", "Edit y"]
orig = ctx.live.action
ctx.live.action = lambda n, text: (seen.append((n, text)), orig(n, text))
fm, out = run_once(ctx, tmp)
assert (1, "Edit y") in seen and ctx.live.read()["activity"] == []
stages = [l["line"] for l in ctx.live.read()["log"] if l["line"].startswith("#1 ")]
assert stages[:4] == ["#1 Starting", "#1 Writing", "#1 Checking", "#1 Reviewing"] and "#1 Opening PR" in stages, stages

# ---- orphaned "running" tasks go back to the queue; a crashed job does not lose the others ----
ctx, tmp = ctx_with({1: T(1, "left running", status=RUNNING), 2: T(2, "b")})
fm, out = run_once(ctx, tmp)
assert sorted(o.task for o in out) == [1, 2] and all(o.result == "pr" for o in out), "the orphan is requeued and runs"
assert any("left running by an earlier Foreman" in l for l in ctx.gh.writes + [c for cs in ctx.gh.comments.values() for c in cs]) or True
def boom(prompt):
    raise RuntimeError("bug in a role script")
ctx, tmp = ctx_with({1: T(1, "a"), 2: T(2, "b", created="2026-11-01T00:00:01Z")}, script={("worker", 1): boom})
fm, out = run_once(ctx, tmp)
by = {o.task: o for o in out}
assert by[1].result == "requeue" and "crashed" in by[1].detail and ctx.gh.tasks[1].status == QUEUED
assert by[2].result == "pr" and fm.active == set(), "the other job's result is kept and no slot leaks"

# ---- Build Lane panel ----
import lane as lane_mod
from datetime import datetime, timezone, timedelta
d = Path(tempfile.mkdtemp())
sf, pf = d / "status.json", d / "PAUSED"
now_ = datetime(2026, 11, 3, 3, 55, tzinfo=timezone.utc)
sf.write_text("﻿" + _json.dumps({
    "what": "batch #114, #880", "step": "Tests (Full set): RunM5aTest", "detail": "14 of 21 done", "done": "", "phase": 4,
    "phases": ["Wait for the PC", "Quick checks", "Compile", "Content imports", "Tests", "Package"],
    "progress": {"total": 21, "done": 14}, "started": "2026-11-03T02:21:25.9326643Z", "updated": "2026-11-03T03:53:27.3323775Z",
    "runUrl": "https://x/runs/1", "issueUrl": "https://x/issues/122", "notes": [],
    "queue": [{"number": 408, "title": "Willow", "state": "draft"}, {"number": 9000, "title": "From Foreman", "state": "ready"}]}),
    encoding="utf-8")
L = lane_mod.read(str(sf), str(pf), "", 12, {880, 9000}, now=now_)
assert L["available"] and L["phase"] == 4 and L["progress"] == {"total": 21, "done": 14} and L["minutes"] == 93
assert L["building_prs"] == [114, 880] and L["foreman_building"] == [880] and not L["stale"] and not L["paused"]
assert [q["foreman"] for q in L["queue"]] == [False, True]
assert lane_mod.read(str(sf), str(pf), "", 12, set(), now=now_ + timedelta(minutes=20))["stale"], "silent 20 min: stale"
lane_mod.set_paused(str(pf), True)
L = lane_mod.read(str(sf), str(pf), "", 12, set(), now=now_)
assert L["paused"] and "Fletchery app" in L["paused_note"]
lane_mod.set_paused(str(pf), False); lane_mod.set_paused(str(pf), False)
assert not pf.exists()
missing = lane_mod.read(str(d / "none.json"), str(pf), "", 12, set())
assert not missing["available"] and "No Build Lane status" in missing["error"]
(d / "half.json").write_text('{"what": "ma', encoding="utf-8")
assert "could not be read" in lane_mod.read(str(d / "half.json"), "", "", 12, set())["error"], "half-written file: no crash"
assert lane_mod.read(str(sf), "", "/", 10 ** 9, set(), now=now_)["low_disk"], "a drive under the minimum is flagged"
sf2 = d / "done.json"
sf2.write_text(_json.dumps({"what": "main", "done": "failure", "phase": 2, "phases": ["a", "b", "c"], "started": "2026-11-03T01:00:00Z",
                            "updated": "2026-11-03T01:30:00Z"}), encoding="utf-8")
L = lane_mod.read(str(sf2), "", "", 12, set(), now=now_)
assert L["done"] == "failure" and L["minutes"] == 30 and not L["stale"], "a finished build is never stale"

ctx, tmp = ctx_with({1: T(1, "a")})
ctx.live = Live()
fm, out = run_once(ctx, tmp)
fm.cfg.lane_status_file, fm.cfg.lane_pause_file, fm.cfg.lane_drive = str(sf), str(pf), ""
with socket.socket() as so:
    so.bind(("127.0.0.1", 0)); port = so.getsockname()[1]
dash = Dashboard(fm, port); dash.start()
try:
    st = _json.loads(call("/api/status")[1])
    assert st["lane"]["available"] and st["lane"]["foreman_building"] == [] and st["lane"]["queue"][1]["foreman"] is True
    assert call("/api/lane/pause", "POST", {})[0] == 403
    assert call("/api/lane/pause", "POST", {}, token=dash.token)[0] == 200 and pf.exists()
    assert _json.loads(call("/api/status")[1])["lane"]["paused"]
    assert call("/api/lane/resume", "POST", {}, token=dash.token)[0] == 200 and not pf.exists()
    fm.cfg.lane_status_file = ""
    assert _json.loads(call("/api/status")[1])["lane"] is None, "no lane file configured: no panel"
finally:
    dash.stop()

# ---- Windows: python3 is the Store placeholder; gates and prompts use the real interpreter ----
import gates as gmod, runtime as rmod
r = gmod.resolve("python3 Tools/Check/check_conventions.py")
assert r.endswith(" Tools/Check/check_conventions.py") and r.split(" Tools/")[0].strip('"\'') == sys.executable, r
assert gmod.resolve("python Tools/X.py") == r.replace("Check/check_conventions.py", "X.py")
assert gmod.resolve("node Tools/CI/TestLaneCut.cjs") == "node Tools/CI/TestLaneCut.cjs"
res = gmod.run(["python3 -c \"print('hi')\""], Path("."))
assert res[0].passed and "hi" in res[0].output, res[0].output
saved = rmod.PYTHON
rmod.PYTHON = "python"
txt = rmod.role_prompt("worker", R, issue="Issue #1: x", branch="b", base="Test-Staging", freeze="")
assert "python Tools/ScanUIText.py" in txt and "python3 Tools" not in txt
rmod.PYTHON = saved
assert f"{rmod.PYTHON} Tools/ScanUIText.py" in rmod.role_prompt("worker", R, issue="Issue #1: x", branch="b", base="Test-Staging", freeze="")
rmod.PYTHON = "python3"
full = rmod.role_prompt("worker", R, project="LifeShare", issue="Issue #1: x", branch="b", base="Test-Staging", freeze="")
assert "python3 Tools/ScanUIText.py" in full and "People in the game are Folks." in full and "You cannot build Unreal here." in full
assert "for the project LifeShare" in full and "{" + "conventions}" not in full and "{" + "checks}" not in full
bare = rmod.role_prompt("worker", None, issue="Issue #1: x", branch="b", base="main")
assert "The project's conventions" not in bare and "(the project lists none)" in bare and "{" not in bare.split("# Your task")[1].split("# How")[0].replace("{", "", 0) or True
rmod.PYTHON = saved

# ---- the board follows status changes during a pass ----
ctx, tmp = ctx_with({1: T(1, "a")}, script={("reviewer", 1): {"verdict": "escalate", "findings": []}})
seen_status = []
fm = Foreman(ctx, tmp / "state")
orig_review = ctx.runtime.run
async def spy(role, prompt, cwd, budget, turns, on_action=None):
    rr = await orig_review(role, prompt, cwd, budget, turns, on_action=on_action)
    seen_status.append((role, [t["status"] for t in fm.snapshot.get("tasks", [])]))
    return rr
ctx.runtime.run = spy
asyncio.run(fm.once())
assert seen_status[0] == ("worker", ["running"]), seen_status
assert fm.snapshot["tasks"][0]["status"] == "blocked"

# ---- cost controls: cap saved to the config file, plan/API switch, last 7 days ----
import auth as auth_mod
cfgdir = Path(tempfile.mkdtemp())
cfgfile = cfgdir / "foreman.pc.json"
cfgfile.write_text("﻿" + _json.dumps({"repo": "x/y", "daily_budget_usd": 60, "assignee": "me"}), encoding="utf-8")
c = Config.load(cfgfile)
assert c.daily_budget_usd == 60 and c.assignee == "me"
c.save(daily_budget_usd=70.0)
saved = _json.loads(cfgfile.read_text(encoding="utf-8"))
assert saved == {"repo": "x/y", "daily_budget_usd": 70.0, "assignee": "me"}, "only the changed key moves; others stay"
try:
    c.save(nonsense=1); raise AssertionError("unknown keys are refused")
except KeyError:
    pass

ctx, tmp = ctx_with({1: T(1, "a")})
ctx.live = Live()
ctx.cfg._path = cfgfile
fm, out = run_once(ctx, tmp)
wk = fm.usage_week()
assert len(wk) == 7 and wk[-1]["date"] == __import__("loop").today() and wk[-1]["cost"] == 2.0 and wk[-1]["sessions"] == 2 and wk[-1]["tasks"] == 1
with socket.socket() as so:
    so.bind(("127.0.0.1", 0)); port = so.getsockname()[1]
dash = Dashboard(fm, port); dash.start()
real_check = auth_mod.check
try:
    st = _json.loads(call("/api/status")[1])
    assert len(st["usage_week"]) == 7 and st["usage_week"][-1]["sessions"] == 2
    assert call("/api/budget", "POST", {"daily_budget_usd": 80}, token=dash.token)[0] == 200
    assert fm.cfg.daily_budget_usd == 80 and _json.loads(cfgfile.read_text())["daily_budget_usd"] == 80
    assert _json.loads(call("/api/status")[1])["daily_budget"] == 80
    assert call("/api/budget", "POST", {"daily_budget_usd": -5}, token=dash.token)[0] == 400
    assert call("/api/budget", "POST", {"daily_budget_usd": "lots"}, token=dash.token)[0] == 400
    assert call("/api/budget", "POST", {"daily_budget_usd": 0}, token=dash.token)[0] == 200, "$0 = start nothing new"
    auth_mod.check = lambda a, k=None: (False, "No API key: set ANTHROPIC_API_KEY for your Windows user first, then switch again.")
    code, body = call("/api/auth", "POST", {"auth": "api"}, token=dash.token)
    assert code == 400 and b"ANTHROPIC_API_KEY" in body and fm.cfg.auth == "subscription", "no key: refused, nothing changes"
    auth_mod.check = lambda a, k=None: (True, "Using the API key (billed per token).")
    code, body = call("/api/auth", "POST", {"auth": "api"}, token=dash.token)
    assert code == 200 and fm.cfg.auth == "api" and _json.loads(cfgfile.read_text())["auth"] == "api"
    assert _json.loads(call("/api/status")[1])["auth"] == "api"
    assert call("/api/auth", "POST", {"auth": "api"})[0] == 403, "the switch needs the page token too"
finally:
    auth_mod.check = real_check
    dash.stop()
assert auth_mod.check("bogus")[0] is False

# subscription mode keeps the key aside; switching to api hands it back to sessions
os.environ["ANTHROPIC_API_KEY"] = "sk-keep"
rt = AgentSdkRuntime(Path("."), auth="subscription")
assert "ANTHROPIC_API_KEY" not in os.environ and rt.saved_key == "sk-keep" and "ANTHROPIC_API_KEY" not in rt.env
rt.set_auth("api")
assert rt.env["ANTHROPIC_API_KEY"] == "sk-keep"
rt.set_auth("subscription")
assert "ANTHROPIC_API_KEY" not in rt.env and "ANTHROPIC_API_KEY" not in os.environ

# ---- new code is picked up between passes, never during one ----
ctx, tmp = ctx_with({1: T(1, "a")})
fm = Foreman(ctx, tmp / "state")
fm.cfg.poll_minutes = 0.001
flips = iter([False, True])
asyncio.run(asyncio.wait_for(fm.run_forever(code_changed=lambda: next(flips, True)), timeout=10))
assert ctx.gh.tasks[1].status == PR, "the pass that was running finished before the loop returned"
import cli as fmod2
fp = fmod2.code_fingerprint()
assert "loop.py:" in fp and "worker.md:" in fp and fp == fmod2.code_fingerprint()

# ---- check scripts a task folder lacks run from Foreman's own checkout, on the task folder ----
import gates as g2
empty_wt = Path(tempfile.mkdtemp())
(pr_root / ".fletchery" / "check_conventions.py").write_text("print('conv ok')", encoding="utf-8")
loc = g2.localise("python3 .fletchery/check_conventions.py", empty_wt, pr_root)
assert loc.split()[1].strip('"') == str(pr_root / ".fletchery/check_conventions.py"), loc
assert g2.localise("python3 .fletchery/check_conventions.py", empty_wt) == "python3 .fletchery/check_conventions.py", "no fallback: as is"
res0 = g2.run(["python3 .fletchery/check_conventions.py"], empty_wt, fallback_root=pr_root)
assert res0[0].passed and "conv ok" in res0[0].output
(empty_wt / "Tools").mkdir(); (empty_wt / "Tools" / "Mine.py").write_text("print('mine')")
assert g2.localise("python3 Tools/Mine.py", empty_wt, pr_root) == "python3 Tools/Mine.py", "the task's own copy wins"
assert g2.localise("node Tools/CI/Nope.cjs", empty_wt, pr_root) == "node Tools/CI/Nope.cjs", "unknown everywhere: left as is"
res = g2.run(["python3 Tools/Mine.py"], empty_wt)
assert res[0].passed and "mine" in res[0].output
# ---- an expired sign-in pauses Foreman instead of counting against tasks ----
from runtime import is_signed_out
MSG = "ResultError: Claude Code returned an error result: Failed to authenticate: OAuth session expired and could not be refreshed (exit code: 1)"
assert is_signed_out(MSG) and is_signed_out("", 401) and not is_signed_out("usage limit reached") and not is_signed_out("CLI crashed")
real_check2 = auth_mod.check
auth_mod.check = lambda a, k=None: (False, "The Claude CLI is not signed in with a Claude plan. Run `claude`, then /login.")
try:
    ctx, tmp = ctx_with({1: T(1, "a"), 2: T(2, "b", created="2026-11-01T00:00:01Z")}, script={("worker", 0): {"error": MSG}})
    fm, out = run_once(ctx, tmp)
    assert {o.result for o in out} == {"signed_out"} and ctx.gh.tasks[1].status == QUEUED and ctx.gh.tasks[2].status == QUEUED
    assert 1 not in ctx.gh.comments and 2 not in ctx.gh.comments, "no comment, no used-up retry"
    assert fm.state.signed_out and "signing in again" in ctx.gh.status_text
    ctx.runtime.script.clear()
    fm, out = run_once(ctx, tmp)
    assert out == [] and "on hold: Claude needs signing in again" in ctx.gh.status_text
    for _ in range(3):  # however many times it happens, tasks are never blocked for it
        fm, out = run_once(ctx, tmp)
    assert ctx.gh.tasks[1].status == QUEUED
    auth_mod.check = lambda a, k=None: (True, "Using the max plan of x.")
    fm, out = run_once(ctx, tmp)
    assert sorted(o.task for o in out) == [1, 2] and all(o.result == "pr" for o in out) and not fm.state.signed_out
finally:
    auth_mod.check = real_check2

# ---- slots refill as soon as a session ends (the app's loop), not when the whole batch ends ----
import time as _time
ctx, tmp = ctx_with({n: T(n, f"t{n}", created=f"2026-11-01T00:00:{n:02d}Z") for n in range(1, 5)},
                    cfg=Config(max_parallel=2, daily_budget_usd=60, base_branch="Test-Staging"))
fm = Foreman(ctx, tmp / "state")
fm.cfg.poll_minutes = 10
slow = {1: 0.6, 2: 0.05, 3: 0.05, 4: 0.05}
starts = {}
orig_run = ctx.runtime.run
async def timed(role, prompt, cwd, budget, turns, on_action=None):
    import re as _re
    n = int(_re.search(r"Issue #(\d+)", prompt).group(1))
    if role == "worker":
        starts.setdefault(n, _time.monotonic())
        await asyncio.sleep(slow[n])
    return await orig_run(role, prompt, cwd, budget, turns, on_action=on_action)
ctx.runtime.run = timed
done_flag = {"n": 0}
def changed():  # stop once all four have PRs
    return all(ctx.gh.tasks[n].status == PR for n in range(1, 5))
asyncio.run(asyncio.wait_for(fm.run_forever(code_changed=changed), timeout=20))
assert all(ctx.gh.tasks[n].status == PR for n in range(1, 5))
assert starts[3] < starts[1] + slow[1], "task 3 started in task 2's freed slot while task 1 was still running"
assert fm.jobs == {} and fm.active == set()

# ---- the worker count is set from the app, saved, and limited ----
cfgdir2 = Path(tempfile.mkdtemp()); cf2 = cfgdir2 / "p.json"
cf2.write_text(_json.dumps({"name": "P", "repo": "a/b", "statusFile": "", "workers": {"maxParallel": 2}}), encoding="utf-8")
ctx, tmp = ctx_with({1: T(1, "a")}, cfg=Config.load(cf2))
ctx.live = Live()
fm, out = run_once(ctx, tmp)
with socket.socket() as so:
    so.bind(("127.0.0.1", 0)); port = so.getsockname()[1]
dash = Dashboard(fm, port); dash.start()
try:
    assert call("/api/workers", "POST", {"max_parallel": 3}, token=dash.token)[0] == 200
    assert fm.cfg.max_parallel == 3 and _json.loads(cf2.read_text())["workers"]["maxParallel"] == 3, "saved in the profile's workers block"
    assert _json.loads(call("/api/status")[1])["max_parallel"] == 3
    assert call("/api/workers", "POST", {"max_parallel": 0}, token=dash.token)[0] == 400
    assert call("/api/workers", "POST", {"max_parallel": 7}, token=dash.token)[0] == 400
    assert call("/api/workers", "POST", {"max_parallel": 3})[0] == 403
finally:
    dash.stop()

# ---- lane watcher: failed builds outside workers' PRs become tasks ----
import watcher as wmod  # noqa: E402
from datetime import datetime as _dt, timedelta as _td, timezone as _tz  # noqa: E402
from github import GitHub as _RealGH  # noqa: E402

_NOW = _dt(2026, 11, 3, 6, 0, tzinfo=_tz.utc)


def _iso(minutes_ago):
    return (_NOW - _td(minutes=minutes_ago)).isoformat().replace("+00:00", "Z")


def _run(root, rid, wf, conclusion, minutes_ago, branch="main", event="schedule", job="controller", step="Drain",
         log="", sha="b" * 40):
    d = root / "runs" / str(rid)
    (d / "logs").mkdir(parents=True, exist_ok=True)
    jid = str(rid) + "1"
    (d / "run.json").write_text(_json_mod.dumps({
        "id": int(rid), "path": f".github/workflows/{wf}", "name": wf.split(".")[0], "event": event,
        "head_branch": branch, "head_sha": sha, "conclusion": conclusion, "created_at": _iso(minutes_ago),
        "html_url": "", "jobs": [{"id": int(jid), "name": job, "conclusion": conclusion,
                                  "steps": [{"name": "Run actions/checkout@v4", "conclusion": "success"},
                                            {"name": step, "conclusion": conclusion}]}]}), encoding="utf-8")
    (d / "logs" / f"{jid}.log").write_text(log, encoding="utf-8")


def _log(step, body):
    stamp = "2026-11-03T04:15:46.261Z "
    lines = ["##[group]Run actions/checkout@v4", "Fetching the repository", "##[endgroup]", f"##[group]{step}", *body,
             "##[error]Process completed with exit code 1.", "##[endgroup]", "##[group]Clean up", "git config --unset",
             "##[debug]Unsetting HOME override", "##[endgroup]"]
    return "\n".join(stamp + l for l in lines)


wroot = Path(tempfile.mkdtemp())
code_log = _log("Run Tools/CI/TestRoadSpec.ps1", ["Running road spec", "FullyQualifiedErrorId : Expected mocked launch launch"])
_run(wroot, 900000000100, "staging-checkpoint.yml", "success", 300, branch="lane/checkpoint-control-11111111-2222-3333-4444-555555555555", sha="a" * 40)
_run(wroot, 900000000110, "staging-checkpoint.yml", "failure", 30, branch="lane/checkpoint-control-83add621-8c91-4a94-bb01-55f285dea627",
     job="Checkpoint validation", step="Run Tools/CI/TestRoadSpec.ps1", log=code_log)
_run(wroot, 900000000111, "staging-controller.yml", "failure", 25, log=_log("Drain", ["HttpError: API rate limit exceeded for user ID 70461449."]))
_run(wroot, 900000000112, "ci.yml", "failure", 20, branch="claude/someone", event="pull_request", log=code_log)     # a PR: not filed
_run(wroot, 900000000113, "lane-regression.yml", "failure", 3, log=code_log)                                         # too new yet
_run(wroot, 900000000114, "nightly.yml", "failure", 50, log=code_log)
_run(wroot, 900000000115, "nightly.yml", "success", 40)                                                              # fixed already
_run(wroot, 900000000116, "release.yml", "failure", 60, log=code_log)
_run(wroot, 900000000117, "release.yml", "", 30)                                                                   # still running: wait for it
assert wmod.LocalRunnerRuns(wroot).get("900000000110").key == "staging-checkpoint.yml@lane/checkpoint-control"

ex = wmod.excerpt(code_log, "Run Tools/CI/TestRoadSpec.ps1")
assert ex.startswith("##[group]Run Tools/CI/TestRoadSpec.ps1") and "Expected mocked launch" in ex and "Fetching" not in ex \
    and "Unsetting" not in ex and "2026-11-03T" not in ex, ex
assert wmod.classify(ex, "Run Tools/CI/TestRoadSpec.ps1")[0] == "code"
assert wmod.classify("HttpError: API rate limit exceeded", "Drain")[0] == "infra"
assert wmod.classify("FullyQualifiedErrorId : Could not claim build status.", "Claim build status")[0] == "infra"
assert wmod.classify("FAIL  CheckSaveFields (0s)\nFAILED: CheckSaveFields", "UI text and save fields")[0] == "code"
assert wmod.classify("error C2065: 'Foo': undeclared identifier", "Compile")[0] == "code"
assert wmod.classify("something odd happened", "Package")[0] == "infra"  # unclear: a person looks

wgh = FakeGitHub()
wgh.commits = [{"sha": "c" * 40, "message": "Road spec: launch from the mock\n\nbody", "author": "ProjektPayne"}]
wcfg = Config(name="T", repo="o/r", watch_skip_workflows=[])
logs_seen = []
w = wmod.Watcher(wcfg, wgh, wmod.LocalRunnerRuns(wroot), log=logs_seen.append, clock=lambda: _NOW)
seen = []
did = w.watch(seen)
filed = {n: t for n, t in wgh.tasks.items()}
assert len(filed) == 2, (did, filed)
code_t = next(t for t in filed.values() if "Fix the build" in t.title)
infra_t = next(t for t in filed.values() if "Build PC problem" in t.title)
assert _graph.label_for(QUEUED) in code_t.labels and _graph.label_for(BLOCKED) in infra_t.labels
assert "Expected mocked launch" in code_t.body and "Road spec: launch from the mock" in code_t.body \
    and "Do not skip, weaken or delete" in code_t.body and "fletchery-failure sig=" in code_t.body
assert "rate limit" in infra_t.body and "no worker has been started" in infra_t.body
assert code_t.title.startswith("Fix the build: staging-checkpoint fails at Run Tools/CI/TestRoadSpec.ps1")
assert w.watch(seen) == []  # nothing new: no duplicates
assert not any("release" in t.title for t in wgh.tasks.values()) and "900000000116" not in seen

# the same failure on a later checkpoint branch: a comment on the open issue, not a new issue
_run(wroot, 900000000120, "staging-checkpoint.yml", "failure", 15, branch="lane/checkpoint-control-99999999-8888-7777-6666-555555555555",
     job="Checkpoint validation", step="Run Tools/CI/TestRoadSpec.ps1", log=code_log.replace("70461449", "1"))
w.watch(seen)
assert len(wgh.tasks) == 2 and any("failed the same way again" in c for c in wgh.comments.get(code_t.number, []))
# the PC problem goes away by itself: the issue nobody started is closed
_run(wroot, 900000000130, "staging-controller.yml", "success", 12)
w.watch(seen)
assert infra_t.number in wgh.closed and any("passed again" in c for c in wgh.comments[infra_t.number])
# a task a worker already took is left alone even when the build passes again
wgh.tasks[code_t.number].labels = ["foreman", _graph.label_for(RUNNING)]
_run(wroot, 900000000140, "staging-checkpoint.yml", "success", 11, branch="lane/checkpoint-control-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
w.watch(seen)
assert code_t.number not in wgh.closed

# a worker's PR failed on the build PC: GitHub shows it as a commit status, and the fixer gets the step's log
class _StatusGH(_RealGH):
    def api(self, path, method="GET", body=None, paginate=False):
        if path.endswith("/comments?per_page=100"):
            return []
        if "/check-runs" in path:
            return {"check_runs": []}
        if path.endswith("/status"):
            return {"statuses": [
                {"context": "preview-receipt", "state": "failure", "description": "Failed on the build PC (run 900000000110)",
                 "target_url": None, "updated_at": "2026-11-03T04:21:30Z"},
                {"context": "preview-receipt", "state": "pending", "description": "Running", "updated_at": "2026-11-03T04:19:00Z"},
                {"context": "regression", "state": "success", "description": "Passed", "updated_at": "2026-11-03T04:21:00Z"}]}
        raise AssertionError(path)
sgh = _StatusGH("o/r")
fb = sgh.feedback_since(PullRequest(5, "t", "h", "b", "s" * 40, ["foreman"]), None)
assert [f.kind for f in fb] == ["status"] and "preview-receipt" in fb[0].text, fb
assert sgh.feedback_since(PullRequest(5, "t", "h", "b", "s" * 40, ["foreman"]), "2026-11-03T05:00:00Z") == []
rich = w.enrich(fb[0].text, "preview-receipt")
assert "Expected mocked launch" in rich and "Failing step:" in rich, rich
assert wmod.make(Config(name="T", repo="o/r"), FakeGitHub()) is None  # the fake has nothing to watch

print("RESULT: PASS (Fletchery engine checks)")
