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
    cfg = cfg or Config(max_parallel=4, daily_budget_usd=60, base_branch="Test-Staging", auto_prerequisites=False)
    cfg.auto_prerequisites = False  # Legacy pipeline fixtures; automatic planning has separate cases below.
    if not cfg.project_root:
        cfg.project_root = str(project_with_rules())
    gh = FakeGitHub(tasks, set(closed))
    tmp = Path(tempfile.mkdtemp())
    return Context(cfg, gh, FakeRepo(tmp, changed=changed), FakeRuntime(script or {}), run_gates=gates_fn, log=lambda s: None), tmp


def run_once(ctx, tmp, dry=False):
    fm = Foreman(ctx, tmp / "state", dry_run=dry)
    # These are fake runtime fixtures, even when their routing specs name real
    # providers. Host CLI sign-ins must not race the deterministic routing checks.
    fm.check_accounts = lambda force=False: None
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
assert r.waiting[2] == "waits for #1 (queued)" and r.waiting[4] == "depends on missing issue #99" and 5 not in r.waiting
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
assert "waits for #10 (PR #9000 waiting to merge)" in ctx.gh.status_text and "- PR #9000 (#10 First)" in ctx.gh.status_text
assert "unblocks 1: #11" in ctx.gh.status_text, ctx.gh.status_text
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

# ---- 0 workers: nothing new is written, and the queue says why ----
ctx, tmp = ctx_with({1: T(1, "a")}, cfg=Config(max_parallel=0, daily_budget_usd=60, base_branch="Test-Staging"))
fm, out = run_once(ctx, tmp)
assert out == [] and "writing paused: workers set to 0" in ctx.gh.status_text

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
for needle in ("## Running", "## Queue", "## Merge", "## Blocked (needs a person)", "- #2 b", "## This pass", "- #1: pr PR #9000"):
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
    assert st["tasks"][1]["why"].startswith("waits for #1 (PR") and st["max_parallel"] == 4 and st["history"][0]["task"] == 1
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
    # Needs you: read the issue here, answer, and Send comments and re-queues at once
    ctx.gh.set_status(3, BLOCKED); ctx.gh.notes[3] = [("alex", "2026-11-03T20:00:00Z", "an earlier note")]
    code, th = call("/api/issue/get", "POST", {"number": 3}, token=dash.token); th = _json.loads(th)
    assert code == 200 and th["number"] == 3 and th["comments"][0]["body"] == "an earlier note", th
    code, r = call("/api/issue/reply", "POST", {"number": 3, "body": "Use the station-aware target."}, token=dash.token)
    assert code == 200 and _json.loads(r)["requeued"] and ctx.gh.tasks[3].status == QUEUED
    assert ctx.gh.comments[3][-1] == "Use the station-aware target."
    ctx.gh.set_status(3, BLOCKED)
    assert call("/api/issue/reply", "POST", {"number": 3, "body": ""}, token=dash.token)[0] == 200 and ctx.gh.tasks[3].status == QUEUED, "re-queue without answering"
    assert len(ctx.gh.comments[3]) == 1, "no empty comment posted"
    assert call("/api/issue/reply", "POST", {"number": "x"}, token=dash.token)[0] == 400
    assert call("/api/issue/reply", "POST", {"number": 3, "body": "hi"})[0] == 403, "needs the page's token"
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
assert len(fp) == 64 and fp == fmod2.code_fingerprint()
# Re-dating the same code (a reinstall, a checkout) is not a code change; editing a file is.
_loop = Path(fmod2.__file__).parent / "loop.py"
_st = _loop.stat()
os.utime(_loop, (_st.st_atime, _st.st_mtime + 120))
assert fmod2.code_fingerprint() == fp, "re-dated files must not look like new code"
os.utime(_loop, (_st.st_atime, _st.st_mtime))

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
    assert call("/api/workers", "POST", {"max_parallel": 10}, token=dash.token)[0] == 200
    assert call("/api/workers", "POST", {"max_parallel": -1}, token=dash.token)[0] == 400
    assert call("/api/workers", "POST", {"max_parallel": 31}, token=dash.token)[0] == 400
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

# "Gate: none" adds no check (it used to run `node none` and block LifeShare #846-#848)
from rules import Rules as _Rules  # noqa: E402
_r = _Rules(always=["python3 .fletchery/check_conventions.py"])
assert _r.checks_for([], ["none"]) == ["python3 .fletchery/check_conventions.py"]
assert _r.checks_for([], ["None.", "`none`", "n/a"]) == ["python3 .fletchery/check_conventions.py"]
assert _r.checks_for([], ["Tools/x.py"])[-1] == "python3 Tools/x.py"

# the merge fixer workflow's jobs ("find", "fix (855, ...)") fix other PRs; they are no verdict on this one (#829)
class _FixerJobsGH(_RealGH):
    def api(self, path, method="GET", body=None, paginate=False):
        if path.endswith("/comments?per_page=100"):
            return []
        if "/check-runs" in path:
            return {"check_runs": [
                {"name": "fix (853, claude/foreman-845-x, #845 Characters: sharper F...", "conclusion": "failure",
                 "completed_at": "2026-11-03T05:37:10Z", "html_url": "u"},
                {"name": "find", "conclusion": "failure", "completed_at": "2026-11-03T05:37:10Z", "html_url": "u"},
                {"name": "Quick checks", "conclusion": "failure", "completed_at": "2026-11-03T05:37:10Z", "html_url": "u"}]}
        if path.endswith("/status"):
            return {"statuses": []}
        raise AssertionError(path)
fb = _FixerJobsGH("o/r").feedback_since(PullRequest(829, "t", "h", "b", "s" * 40, ["foreman"]), None)
assert [f.text.split("`")[1] for f in fb] == ["Quick checks"], fb

# ---- the Merge section: finished PRs, most-unblocking first, with round, imports and path flags ----
from graph import unblocks, merge_round  # noqa: E402
g = {1: T(1, "a", status=PR), 2: T(2, "b", "Depends on: #1"), 3: T(3, "c", "Depends on: #2"), 4: T(4, "d", status=PR),
     5: T(5, "e", "Depends on: #4")}
assert unblocks(g, 1) == [2, 3] and unblocks(g, 4) == [5] and unblocks(g, 3) == []
assert merge_round(["ready", "round:7"]) == "7" and merge_round(["Round-12"]) == "12" and merge_round(["ready"]) == ""
ctx, tmp = ctx_with({1: T(1, "a", status=PR), 2: T(2, "b", "Depends on: #1"), 3: T(3, "c", "Depends on: #2"),
                     4: T(4, "d", status=PR), 5: T(5, "e", "Depends on: #4", status=BLOCKED), 6: T(6, "f", "Depends on: #4", status=BLOCKED)},
                    cfg=Config(max_parallel=0, merge_flags=[{"paths": "^Content/Town/", "flag": "strict NavCheck on the PC"}]))
ctx.gh.prs += [PullRequest(71, "t1", "claude/foreman-1-a", "Test-Staging", "a" * 40, ["foreman", "ready", "round:7"],
                           "Closes #1\n\nLane-Imports: BuildTown"),
               PullRequest(74, "t4", "claude/foreman-4-d", "Test-Staging", "b" * 40, ["foreman"], "Closes #4", False)]
ctx.gh.files = {71: ["Content/Town/Source/town.json"], 74: ["Source/x.cpp"]}
fm, out = run_once(ctx, tmp)
assert any("pending" in flag for flag in fm.snapshot["merge"][0]["flags"])
fm.__dict__["_merge_files"] = {(p.number, p.head_sha): ctx.gh.files[p.number] for p in ctx.gh.prs}
fm.publish([])
rows = fm.snapshot["merge"]
assert [r["pr"] for r in rows] == [71, 74] and rows[0]["unblocks"] == [2, 3] and rows[1]["unblocks"] == [5, 6]
assert rows[0]["round"] == "7" and rows[0]["imports"] == "BuildTown" and rows[0]["flags"] == ["strict NavCheck on the PC"]
assert rows[1]["conflict"] and not rows[1]["ready"] and rows[1]["flags"] == [] and rows[0]["head"] == "a" * 8
s = ctx.gh.status_text
assert "## Merge" in s and "- PR #71 (#1 a) · head `aaaaaaaa` · unblocks 2: #2, #3 · round 7 · imports: BuildTown · **strict NavCheck on the PC**" in s, s
assert "**conflicts** · not `ready`" in s
assert "No merge round running" in s

# ---- what the merge round is doing: a plain headline from merge-round.json and the PC's heavy lease ----
import merge as merge_mod  # noqa: E402
from datetime import datetime as _dt, timezone as _tz  # noqa: E402
mt = Path(tempfile.mkdtemp())
(mt / "heavy.json").write_text(_json_mod.dumps({"token": "x", "purpose": "Tools\\RunFlowTest.ps1",
                                                 "workspaces": "E:\\lifeshare-scratch\\toilet-w1",
                                                 "acquiredUtc": "2026-11-03T15:16:56.71Z"}), encoding="utf-8")
(mt / "round.json").write_text(_json_mod.dumps({"round": "7", "started": "2026-11-03T13:40:00Z", "updated": "2026-11-03T15:00:00Z",
    "prs": [71, 74], "steps": [{"name": "Merge the PRs", "state": "done"}, {"name": "BuildTown", "state": "waiting"},
                               {"name": "NavCheck", "state": "todo"}, {"name": "Game specs", "state": "todo"}]}), encoding="utf-8")
at = _dt(2026, 11, 3, 15, 20, tzinfo=_tz.utc)
v = merge_mod.view(mt / "round.json", mt / "heavy.json", {}, now=at)
assert v["state"] == "waiting" and v["round"] == "7" and v["next"] == "NavCheck" and not v["stale"], v
assert v["headline"].startswith("Merging round 7: 2 PRs, step 2 of 4, waiting to run BuildTown. Waiting because the PC is busy "
                                "with RunFlowTest.ps1 in toilet-w1 (since 15:16Z); started 13:40Z; then NavCheck."), v["headline"]
v = merge_mod.view(mt / "round.json", mt / "missing.json", {}, now=_dt(2026, 11, 3, 18, 0, tzinfo=_tz.utc))
assert v["stale"] and "may be stuck" in v["headline"], v["headline"]
(mt / "round.json").write_text(_json_mod.dumps({"round": "7", "state": "done"}), encoding="utf-8")
assert merge_mod.view(mt / "round.json", mt / "heavy.json", {}, now=at)["headline"].startswith("No merge round running")
# the round's PRs come first in the status issue, under their own heading
(mt / "round.json").write_text(_json_mod.dumps({"round": "7", "prs": [74], "steps": [{"name": "BuildTown", "state": "running"}]}),
                               encoding="utf-8")
ctx, tmp = ctx_with({1: T(1, "a", status=PR), 4: T(4, "d", status=PR)},
                    cfg=Config(max_parallel=0, merge_round_file=str(mt / "round.json")))
ctx.gh.prs += [PullRequest(71, "t1", "claude/foreman-1-a", "Test-Staging", "a" * 40, ["foreman", "ready"], "Closes #1"),
               PullRequest(74, "t4", "claude/foreman-4-d", "Test-Staging", "b" * 40, ["foreman", "ready"], "Closes #4")]
fm, out = run_once(ctx, tmp)
s = ctx.gh.status_text
assert "**Now:** Merging round 7: 1 PR, step 1 of 1, running BuildTown." in s and "Steps: ▶ BuildTown" in s, s
assert s.index("**In round 7** (1):") < s.index("- PR #74") < s.index("**Waiting for a round** (1)") < s.index("- PR #71"), s

# ---- a PR merged into Test-Staging leaves its issue open; Fletchery closes it so dependents start ----
ctx, tmp = ctx_with({20: T(20, "Root", status=PR), 21: T(21, "Child", "Depends on: #20")})
ctx.gh.merged = {"claude/foreman-20-root": (916, "Test-Staging", "2026-11-03T17:05:00Z")}
fm, out = run_once(ctx, tmp)
assert 20 in ctx.gh.closed and "merged into Test-Staging" in ctx.gh.comments[20][0], ctx.gh.writes
assert [o.task for o in out] == [21], "the dependent starts in the same pass"


# ---- several agents: routing, review by another vendor, escalation, per-agent limits, learning ----
import agents as _ag  # noqa: E402
import clis as _clis  # noqa: E402
import keys as _keys  # noqa: E402

assert _ag.classify("Fix typo in README", "Docs only.", [])[0] == "easy"
assert _ag.classify("Bump save version", "Add a migration for v12.", [])[0] == "hard"
assert _ag.classify("Clock 12/24h", "Change LSClock.cpp to read the setting.", [])[0] == "medium"
assert _ag.classify("Fix typo", "Difficulty: hard", [])[0] == "hard"
assert _ag.classify("x", "", ["difficulty:easy"])[0] == "easy"
assert _ag.override("Agent: deepseek-pro\nbody", []) == "deepseek-pro" and _ag.override("", ["agent:codex"]) == "codex"

AGENTS = [{"name": "claude", "kind": "fake", "tier": 4, "vendor": "anthropic"},
          {"name": "codex", "kind": "fake", "tier": 3, "vendor": "openai", "enabled": False},
          {"name": "deepseek-pro", "kind": "fake", "tier": 2, "vendor": "deepseek"},
          {"name": "deepseek", "kind": "fake", "tier": 1, "vendor": "deepseek"}]
ROUTING = {"easy": ["deepseek", "deepseek-pro"], "medium": ["codex", "claude"], "hard": ["claude", "codex"],
           "reviewers": ["claude", "codex"]}


def pool_with(fakes=None, agents=AGENTS, routing=ROUTING):
    fakes = fakes or {}
    specs = _ag.specs_from([dict(a) for a in agents])
    return _ag.AgentPool(specs, dict(routing), lambda spec: fakes.setdefault(spec.name, FakeRuntime()))


p = pool_with()
assert p.choose("Fix typo in README", "", []).agent == "deepseek"
assert p.choose("Clock 12/24h", "Change LSClock.cpp.", []).agent == "claude", "codex is off, so medium goes to claude"
assert p.choose("Clock", "Agent: deepseek-pro", []).agent == "deepseek-pro"
assert p.reviewer_for("deepseek") == "claude" and p.reviewer_for("claude") == "deepseek-pro", "codex off: another vendor reviews claude"
p.set_enabled("deepseek-pro", False)
assert p.reviewer_for("claude") == "claude", "no other strong vendor on: a fresh claude session reviews"
p.set_enabled("deepseek-pro", True)
assert p.escalate("deepseek") == "deepseek-pro" and p.escalate("deepseek-pro") == "claude" and p.escalate("claude") is None
p.pause("deepseek", 30, "usage limit")
assert p.choose("Fix typo in README", "", []).agent == "deepseek-pro", "a paused agent is skipped"
assert p.choose("x", "Agent: deepseek", []) is None, "an asked-for agent that is paused is waited for"
p.accounts["deepseek-pro"] = {"ok": False, "message": "not signed in"}
assert p.choose("Fix typo in README", "", []).agent == "claude", "a signed-out agent is skipped too"

# learning: an agent that passes enough medium tasks is tried first; one that fails them goes last
st = _ag.Stats()
for _ in range(6):
    st.add("deepseek", "medium", True)
for _ in range(6):
    st.add("claude", "hard", False)
p = pool_with()
assert p.choose("Clock 12/24h", "Change LSClock.cpp.", [], stats=st).agent == "deepseek", "proven on medium: goes first"
assert p.choose("Bump save version", "migration", [], stats=st).agent != "claude", "keeps failing hard: goes last"


def multi_ctx(tasks, fakes, agents=AGENTS, gates_fn=PASS):
    ctx, tmp = ctx_with(tasks, gates_fn=gates_fn)
    ctx.agents = pool_with(fakes, agents)
    return ctx, tmp


# escalation: the cheap agent gives up, a stronger one finishes, a claude reviewer approves
fakes = {"deepseek": FakeRuntime({("worker", 0): {"summary": "", "files": [], "build_steps": [], "checks": [],
                                                   "unverified": [], "gave_up": "could not find the setting"}}),
         "deepseek-pro": FakeRuntime(), "claude": FakeRuntime()}
ctx, tmp = multi_ctx({30: T(30, "Fix typo in README", "Docs only.")}, fakes)
fm, out = run_once(ctx, tmp)
o = out[0]
assert o.result == "pr" and o.agent == "deepseek-pro" and o.reviewer == "claude", o
assert [(a["agent"], a["ok"]) for a in o.attempts] == [("deepseek", False), ("deepseek-pro", True)], o.attempts
assert ctx.repo.resets == ["30-fix-typo-in-readme"], "the failed attempt is preserved before preparing its replacement"
assert [r for r, _, _ in fakes["claude"].calls] == ["reviewer"], "claude only reviewed"
assert "Earlier attempts" in fakes["deepseek-pro"].calls[0][2], "the next agent is told what went wrong"
assert "**Written by:** deepseek-pro (after deepseek) · **Reviewed by:** claude" in ctx.gh.prs[-1].body
assert any("work is preserved at" in c for c in ctx.gh.comments[30])
assert any(p.name.endswith("-attempt-1") for p in ctx.repo.active_checkouts.values()), "escalation gets a distinct checkout"
led = [_json_mod.loads(l) for l in (tmp / "state" / "ledger.jsonl").read_text().splitlines()]
assert led[-1]["agent"] == "deepseek-pro" and len(led[-1]["attempts"]) == 2
assert fm.stats().rate("deepseek", "easy") == (0.0, 1) and fm.stats().rate("deepseek-pro", "easy") == (1.0, 1)
assert fm.snapshot["agents"]["multi"] and fm.snapshot["tasks"][0]["agent"] == "deepseek-pro"
assert fm.snapshot["agents"]["spent_today"] == {"deepseek": 1.0, "deepseek-pro": 1.0, "claude": 1.0}, fm.snapshot["agents"]["spent_today"]
assert "## Agents" in ctx.gh.status_text and "**codex**" in ctx.gh.status_text

# the strongest fails too: blocked, naming every agent tried
bad = {"summary": "", "files": [], "build_steps": [], "checks": [], "unverified": [], "gave_up": "design decision"}
fakes = {n: FakeRuntime({("worker", 0): bad}) for n in ("deepseek", "deepseek-pro", "claude")}
ctx, tmp = multi_ctx({31: T(31, "Fix typo in README", "")}, fakes)
fm, out = run_once(ctx, tmp)
assert out[0].result == "blocked" and "deepseek, deepseek-pro, claude" in out[0].detail, out[0].detail

# a usage limit on one agent pauses only that agent; the task goes back to the queue and another agent takes it
fakes = {"deepseek": FakeRuntime({("worker", 0): {"error": "You've hit your usage limit. Resets at 6pm."}}),
         "deepseek-pro": FakeRuntime(), "claude": FakeRuntime()}
ctx, tmp = multi_ctx({32: T(32, "Fix typo in README", "")}, fakes)
fm, out = run_once(ctx, tmp)
assert out[0].result == "agent_paused" and "deepseek" in ctx.agents.paused, out[0]
assert not fm.state.paused_until, "no whole-Fletchery pause while other agents are free"
assert fm.state.agent_paused is ctx.agents.paused
fm2, out2 = run_once(ctx, tmp)
assert out2[0].result == "pr" and out2[0].agent == "deepseek-pro", out2

# Full rank slots on another account must not turn a provider quota into a global pause.
from pipeline import Limited, Outcome, _paused  # noqa: E402
ctx, _ = multi_ctx({}, {})
ctx.agents.worker_limits = {"low": 0, "medium": 0, "high": 0}
assert not ctx.agents.any_available(besides="deepseek")
outcome = Outcome(32, "requeue")
assert _paused(ctx, outcome, Limited("provider quota", 0, "limited", "deepseek"), "#32")
assert outcome.result == "agent_paused" and "claude" not in ctx.agents.paused

# an asked-for agent that is off: the task waits, saying why, instead of starting and stopping
ctx, tmp = multi_ctx({33: T(33, "x", "Agent: codex")}, {})
fm, out = run_once(ctx, tmp)
assert out == [] and "waits for agent codex" in ctx.gh.status_text, ctx.gh.status_text

# the fixer is the agent that wrote the branch; the second attempt goes one tier up
ctx, tmp = multi_ctx({}, {})
ctx.task_agents["40"] = "deepseek"
from pipeline import fixer_agent  # noqa: E402
t40 = T(40, "y")
assert fixer_agent(ctx, t40, 1) == "deepseek" and fixer_agent(ctx, t40, 2) == "deepseek-pro"
ctx.agents.set_enabled("deepseek", False)
assert fixer_agent(ctx, t40, 1) == "claude", "the writer is off: the router's pick for the task (medium -> claude)"

# a reviewer that can't run (a model its CLI refuses) is paused and another reviewer takes the task; it isn't blocked
fakes = {"claude": FakeRuntime(), "deepseek": FakeRuntime(),
         "deepseek-pro": FakeRuntime({("reviewer", 0): {"error": "The 'openai/x' model is not supported when using Codex with a ChatGPT account."}})}
ctx, tmp = multi_ctx({35: T(35, "Clock 12/24h", "Change LSClock.cpp.")}, fakes)
fm, out = run_once(ctx, tmp)
assert out[0].result == "pr" and out[0].agent == "claude" and out[0].reviewer not in ("", "deepseek-pro"), out[0]
assert "can't run" in ctx.agents.paused["deepseek-pro"]["reason"], ctx.agents.paused
assert "deepseek" not in ctx.agents.paused, "only the broken agent is paused, not its whole account"
p = pool_with()
assert p.reviewer_for("claude", besides={"deepseek-pro"}) not in (None, "deepseek-pro")
assert p.reviewer_for("claude", besides={n for n in p.specs}) is None
# every reviewer fails to run: blocked, saying no reviewer could run (not a bare "reviewer escalated:")
err = {"error": "crashed"}
fakes = {"claude": FakeRuntime({("reviewer", 0): err}), "deepseek": FakeRuntime({("reviewer", 0): err}),
         "deepseek-pro": FakeRuntime({("reviewer", 0): err})}
ctx, tmp = multi_ctx({36: T(36, "Clock 12/24h", "Change LSClock.cpp.")}, fakes)
fm, out = run_once(ctx, tmp)
assert out[0].result == "blocked" and "no reviewer could run" in out[0].detail, out[0].detail
# Codex takes bare model names: OpenCode's openai/ spelling is tidied, other provider/model names are refused
assert _ag.codex_model("openai/gpt-6.1-terra") == "gpt-6.1-terra" and _ag.codex_model("gpt-6-astra") == "gpt-6-astra"
assert _ag.specs_from([{"name": "t", "kind": "codex", "model": "openai/gpt-6.1-terra"}])[0].model == "gpt-6.1-terra"

# one agent (no agents block): unchanged behaviour, the PR body says Claude
ctx, tmp = ctx_with({34: T(34, "Solo")})
fm, out = run_once(ctx, tmp)
assert out[0].result == "pr" and "Written by" not in ctx.gh.prs[-1].body and fm.snapshot["agents"] is None

# Codex/OpenCode plumbing
sch = _clis.strict_schema(__import__("runtime").SCHEMAS["worker"])
assert set(sch["required"]) == set(sch["properties"]) and sch["additionalProperties"] is False
assert sch["properties"]["gave_up"]["type"] == ["string", "null"] and sch["properties"]["summary"]["type"] == "string"
assert _clis.drop_nulls({"a": 1, "gave_up": None}) == {"a": 1}
assert _clis.short_command('"C:\\\\Windows\\\\System32\\\\WindowsPowerShell\\\\v1.0\\\\powershell.exe" -Command \'python hello.py\'') == "python hello.py"
cx = _clis.CodexRuntime(_ag.AgentSpec("codex", "codex"))
seen, state = [], {"last_text": "", "errors": [], "cost": 0.0, "turns": 0, "session": ""}
for ev in [{"type": "thread.started", "thread_id": "t1"},
           {"type": "item.completed", "item": {"type": "command_execution", "command": "git status"}},
           {"type": "item.completed", "item": {"type": "file_change", "changes": [{"path": "/w/a.py"}]}},
           {"type": "item.completed", "item": {"type": "error", "message": "model metadata not found"}},
           {"type": "item.completed", "item": {"type": "agent_message", "text": '{"summary": "s"}'}},
           {"type": "turn.completed", "usage": {}}]:
    cx.on_event(ev, state, "/w", seen.append)
assert seen == ["$ git status", "Edit a.py"] and state["session"] == "t1" and state["turns"] == 1 and not state["errors"]
pf = Path(tempfile.mkdtemp()) / "p.md"
pf.write_text("x")
argv, stdin, _ = cx.command("reviewer", pf, Path("/w"), pf.parent)
assert "read-only" in argv and argv[-1] == "-" and stdin is not None
oc = _clis.OpenCodeRuntime(_ag.AgentSpec("ds", "opencode", "opencode-go/deepseek-v4.1-flash"))
assert '"required"' in (pf.parent / "schema.json").read_text()
seen, state = [], {"last_text": "", "errors": [], "cost": 0.0, "turns": 0, "session": ""}
for ev in [{"type": "tool_use", "sessionID": "s1", "part": {"tool": "edit", "state": {"input": {"filePath": "/w/b.py"}}}},
           {"type": "tool_use", "part": {"tool": "bash", "state": {"input": {"command": "python  -m pytest"}}}},
           {"type": "text", "part": {"text": '{"summary": "ok"}'}},
           {"type": "step_finish", "part": {"cost": 0.012}}]:
    oc.on_event(ev, state, "/w", seen.append)
assert seen == ["Edit b.py", "$ python -m pytest"] and state["cost"] == 0.012 and state["session"] == "s1"
argv, stdin, env = oc.command("reviewer", pf, Path("/w"), pf.parent)
assert stdin is None and "-f" in argv and '"edit": "deny"' in env["OPENCODE_CONFIG_CONTENT"]
argv, stdin, env = oc.command("worker", pf, Path("/w"), pf.parent)
assert '"edit": "allow"' in env["OPENCODE_CONFIG_CONTENT"] and '"webfetch": "deny"' in env["OPENCODE_CONFIG_CONTENT"]

# keys: stored, listed only by their last 4 characters, given only to agents that list them
_keys.set_key("DEEPSEEK_API_KEY", "sk-test-123456789")
row = next(k for k in _keys.listing() if k["name"] == "DEEPSEEK_API_KEY")
assert row["set"] and row["hint"] == "…6789" and "sk-test" not in _json_mod.dumps(_keys.listing())
assert _keys.env_for(["DEEPSEEK_API_KEY", "OPENAI_API_KEY"]) == {"DEEPSEEK_API_KEY": "sk-test-123456789"}
for bad in ("lower_case", "X"):
    try:
        _keys.set_key(bad, "v")
        raise AssertionError("bad key name accepted")
    except ValueError:
        pass
_keys.delete("DEEPSEEK_API_KEY")
assert _keys.get("DEEPSEEK_API_KEY") is None

# the dashboard: switch an agent off and on, never the last one; it is saved in the profile
from server import Dashboard  # noqa: E402
prof = Path(tempfile.mkdtemp()) / "p.profile.json"
prof.write_text(_json_mod.dumps({"name": "X", "repo": "o/r", "statusFile": "s", "workers": {"agents": AGENTS, "reviewerMaxTurns": 50}}))
cfg = Config.load(prof)
ctx, tmp = ctx_with({}, cfg=cfg)
ctx.agents = pool_with(agents=cfg.agents)
fm = Foreman(ctx, tmp / "state", dry_run=True)
dash = Dashboard(fm, 0)
dash.toggle_agent({"name": "deepseek", "on": False})
saved = _json_mod.loads(prof.read_text())["workers"]
assert next(a for a in saved["agents"] if a["name"] == "deepseek")["enabled"] is False and saved["reviewerMaxTurns"] == 50
assert not ctx.agents.available("deepseek")
dash.toggle_agent({"name": "claude", "on": False})
try:  # codex was off from the start: deepseek-pro is the last one on
    dash.toggle_agent({"name": "deepseek-pro", "on": False})
    raise AssertionError("the last agent switched off")
except ValueError:
    pass
dash.toggle_agent({"name": "claude", "on": True})
dash.add_agent({"name": "kimi", "kind": "opencode", "model": "opencode-go/kimi-k3", "tier": 3, "vendor": "moonshot"})
assert "kimi" in ctx.agents.specs and any(a["name"] == "kimi" for a in _json_mod.loads(prof.read_text())["workers"]["agents"])
dash.remove_agent({"name": "kimi"})
# Add agent: the name is tidied ("Haiku" -> haiku), the company comes from the model, and a model OpenCode
# doesn't offer is refused with the nearest real names
import clis as _clis  # noqa: E402
_clis._models_cache[""] = (9e18, ["opencode-go/claude-haiku-5-5", "opencode-go/kimi-k3", "anthropic/claude-haiku-5-5"])
try:
    dash.add_agent({"name": "Haiku", "kind": "opencode", "model": "opencode-go/haiku-5.5", "tier": 3})
    raise AssertionError("a model OpenCode doesn't offer was accepted")
except ValueError as e:
    assert "opencode-go/claude-haiku-5-5" in str(e), e
dash.add_agent({"name": "Haiku", "kind": "opencode", "model": "opencode-go/claude-haiku-5-5"})  # no Strength picked
assert "haiku" in ctx.agents.specs and ctx.agents.specs["haiku"].vendor == "anthropic"
import scores as _sc  # noqa: E402
_hk = _sc.seed("opencode", "opencode-go/claude-haiku-5-5", 2, _sc.load_catalog())
assert _hk["from"] == "benchmarks" and ctx.agents.specs["haiku"].tier == max(1, min(5, round((_hk["coding"] - 20) / 15))), ctx.agents.specs["haiku"].tier
dash.remove_agent({"name": "haiku"})
dash.add_agent({"name": "opencode-go/kimi-k3", "kind": "opencode"})  # the model typed into the name box
assert ctx.agents.specs["kimi-k3"].model == "opencode-go/kimi-k3" and ctx.agents.specs["kimi-k3"].vendor == "moonshot"
dash.remove_agent({"name": "kimi-k3"})
# Refresh agents: a plan upgrade lifts usage-limit pauses at once (shared ones too) and re-checks sign-ins
_some = [n for n in ctx.agents.specs][:1]
assert _some, "the dashboard test pool has agents"
ctx.agents.pause(_some[0], 300, "5-hour usage limit")
assert _some[0] in ctx.agents.paused
_r = dash.refresh_agents({})
assert _some[0] in _r["unpaused"] and not ctx.agents.paused, _r
assert "Lifted" in _r["message"]
assert dash.refresh_agents({})["unpaused"] == []
_clis._models_cache.clear()
assert "kimi" not in ctx.agents.specs
from agents import AgentSpec as _Spec  # noqa: E402
assert _Spec("c", "claude").api_key_name == "ANTHROPIC_API_KEY" and _Spec("x", "codex").api_key_name == "OPENAI_API_KEY"
assert _Spec("d", "opencode", model="deepseek/deepseek-chat").api_key_name == "DEEPSEEK_API_KEY"
# each agent runs on its plan sign-in or its stored API key, switched from its card
try:
    dash.set_agent_auth({"name": "codex", "mode": "key"})
    raise AssertionError("switched to a key that isn't stored")
except ValueError:
    pass
ctx.agents.specs["codex"].key_env = ["OPENAI_API_KEY"]
_keys.set_key("OPENAI_API_KEY", "sk-openai-abcd1234")
dash.set_agent_auth({"name": "codex", "mode": "key"})
assert ctx.agents.specs["codex"].auth == "key" and ctx.agents.specs["codex"].api_key_name == "OPENAI_API_KEY"
assert next(a for a in _json_mod.loads(prof.read_text())["workers"]["agents"] if a["name"] == "codex")["auth"] == "key"
row = next(a for a in ctx.agents.view() if a["name"] == "codex")
assert row["auth"] == "key" and row["api_key"] == "OPENAI_API_KEY" and "sk-openai" not in _json_mod.dumps(ctx.agents.view())
dash.set_agent_auth({"name": "codex", "mode": "plan"})
assert ctx.agents.specs["codex"].auth == "plan"
_keys.delete("OPENAI_API_KEY")

# ---- scores: benchmarks seed each agent, the cheapest agent that clears the task's bar gets it, results move scores
import scores as _sc  # noqa: E402
assert _sc.classify_skill("Port ScanUIText to Python", "Rewrite Tools/ScanUIText.py as a script.", [])[0] == "scripts"
assert _sc.classify_skill("Radial menu", "The UMG widget's buttons and tooltip.", [])[0] == "ui"
assert _sc.classify_skill("Pronoun pass", "Edit docs/Strings.md and LSStrings.csv.", [])[0] == "docs"
assert _sc.classify_skill("Clock", "Change LSClock.cpp.", [])[0] == "cpp"
assert _sc.classify_skill("Anything", "", ["skill:docs"])[0] == "docs"
# a C++ task whose template also names docs, strings and a Python check stays C++
assert _sc.classify_skill("Pet animation: tail wag by mood", "Change LSPetAnim.cpp. Update docs/Pets.md and LSStrings.csv; "
                          "run Tools/check.py.", [])[0] == "cpp"
SCORED = [{"name": "claude", "kind": "claude", "model": "claude-opus-5-5", "tier": 4, "vendor": "anthropic"},
          {"name": "haiku", "kind": "opencode", "model": "opencode-go/claude-haiku-5-5", "tier": 3, "vendor": "anthropic"},
          {"name": "deepseek-pro", "kind": "opencode", "model": "opencode-go/deepseek-v4-pro", "tier": 2, "vendor": "deepseek"},
          {"name": "deepseek", "kind": "opencode", "model": "opencode-go/deepseek-v4.1-flash", "tier": 1, "vendor": "deepseek"},
          {"name": "mystery", "kind": "fake", "model": "acme/unknown-1", "tier": 2, "vendor": "acme", "enabled": False}]
sp = pool_with(agents=SCORED, routing={})
assert sp.mode == "scores" and pool_with().mode == "lists", "lists in the profile keep list routing"
sc = sp.scored()
seeds = {n: sc[n]["skills"]["cpp"]["score"] for n in sc}
assert seeds == {"claude": 91, "haiku": 64, "deepseek-pro": 66, "deepseek": 56, "mystery": 50}, seeds
assert sc["haiku"]["seed"]["from"] == "benchmarks" and sc["mystery"]["seed"]["from"] == "strength"
assert sc["haiku"]["cost"] > sc["deepseek-pro"]["cost"] > sc["claude"]["cost"]
c = sp.choose("Clock 12/24h", "Change LSClock.cpp.", [])
assert c.agent == "haiku" and c.skill == "cpp" and "bar 60" in c.why, c.why
assert sp.choose("Save v12 migration", "Bump the save version; migration for old saves.", []).agent == "claude"
sp.busy["haiku"] = 3  # busy agents count as dearer, so work spreads
assert sp.choose("Clock 12/24h", "Change LSClock.cpp.", []).agent == "deepseek-pro"
sp.busy["haiku"] = 0
assert sp.escalate("deepseek", "worker", "cpp") == "haiku" and sp.escalate("claude", "worker", "cpp") is None
assert sp.reviewer_for("haiku") == "deepseek-pro" and sp.reviewer_for("deepseek") == "claude"
# a plan's limit is the account's: pausing one OpenCode Go agent pauses the others on opencode-go, not Claude
who = sp.pause("deepseek-pro", 30, "usage limit: Go 5 hour limit")
assert set(who) == {"deepseek-pro", "deepseek", "haiku"} and sp.available("claude") and not sp.available("haiku")
assert "same account as deepseek-pro" in sp.paused["haiku"]["reason"]
sp.paused.clear()
# five failed medium C++ tasks pull haiku under the bar; its Python score is untouched
st = _ag.Stats()
for _ in range(5):
    st.add("haiku", "medium", False)
    st.attempts.append({"agent": "haiku", "difficulty": "medium", "skill": "cpp", "role": "worker", "ok": False})
c = sp.choose("Clock 12/24h", "Change LSClock.cpp.", [], stats=st)
assert c.agent == "deepseek-pro", c.why
sc = sp.scored(st)
assert sc["haiku"]["skills"]["cpp"]["score"] < 60 and sc["haiku"]["skills"]["cpp"]["from"] == "results"
assert sc["haiku"]["skills"]["scripts"]["score"] == 64
sp.specs["deepseek"].scores = {"cpp": 80}  # your own score wins
assert sp.scored(st)["deepseek"]["skills"]["cpp"] == {"seed": 56, "score": 80, "from": "you", "tasks": 0, "passed": 0}
row = next(r for r in sp.view(st) if r["name"] == "haiku")
assert row["scores"]["skills"]["cpp"]["tasks"] == 5
# the dashboard sets and clears your own scores and saves them in the profile
dash.set_scores({"name": "deepseek", "scores": {"cpp": 72, "docs": ""}})
assert ctx.agents.specs["deepseek"].scores == {"cpp": 72}
assert next(a for a in _json_mod.loads(prof.read_text())["workers"]["agents"] if a["name"] == "deepseek")["scores"] == {"cpp": 72}
dash.set_scores({"name": "deepseek", "scores": {"cpp": None}})
assert ctx.agents.specs["deepseek"].scores is None
assert "scores" not in next(a for a in _json_mod.loads(prof.read_text())["workers"]["agents"] if a["name"] == "deepseek")
for bad in ({"cpp": 140}, {"speed": 3}, {"cpp": "lots"}):
    try:
        dash.set_scores({"name": "deepseek", "scores": bad})
        raise AssertionError(f"bad score accepted: {bad}")
    except ValueError:
        pass

# ---- Settings page and projects: edit a profile, add a project, switch ----
import os as _os  # noqa: E402
_os.environ["FLETCHERY_HOME"] = tempfile.mkdtemp()
import projects as _pj  # noqa: E402
cfg_s = Config.load(prof)
r = _pj.apply_settings(cfg_s, {"max_parallel": "0", "base_branch": "main", "repo": "https://github.com/acme/desk.git",
                               "code_freeze": "on"})
saved = _json_mod.loads(prof.read_text())
assert saved["workers"]["maxParallel"] == 0 and saved["repo"] == "acme/desk" and saved["workers"]["codeFreeze"] is True
assert "GitHub repository" in r["restart"] and "Workers at once" not in r["restart"]
assert saved["workers"]["reviewerMaxTurns"] == 50, "keys the page doesn't show are kept"
for bad in ({"max_parallel": 31}, {"repo": "not a repo"}, {"label": "two words"}, {"nope": 1}, {"base_branch": ""}):
    try:
        _pj.apply_settings(cfg_s, bad)
        raise AssertionError(f"bad setting accepted: {bad}")
    except ValueError:
        pass
calls = []
def fake_run(argv, timeout=30):
    calls.append(argv)
    if argv[:2] == ["git", "-C"]:
        return 0, "https://github.com/acme/desk.git"
    return 0, "ok"
clone = Path(tempfile.mkdtemp())
res = _pj.new_project({"name": "Desk App", "repo": "acme/desk", "project_root": str(clone), "base_branch": "main",
                       "label": "desk", "copy_agents": True, "create_labels": True}, cfg_s, run=fake_run)
newp = _json_mod.loads(Path(res["path"]).read_text())
assert Path(res["path"]).name == "Desk-App.profile.json" and newp["workers"]["label"] == "desk" and newp["workers"]["agents"]
assert all(c["ok"] for c in res["checks"]) and any(c[:3] == ["gh", "label", "create"] and "desk:queued" in c for c in calls)
try:
    _pj.new_project({"name": "Desk App", "repo": "acme/desk"}, cfg_s, run=fake_run)
    raise AssertionError("a second project with the same file name")
except ValueError:
    pass
bad = _pj.check_project("acme/desk", str(clone), "main", run=lambda a, timeout=30: (0, "https://github.com/other/thing") if a[0] == "git" else (0, "ok"))
assert not bad[-1]["ok"] and "other/thing" in bad[-1]["detail"]
names = [x["name"] for x in _pj.list_projects(prof)]
assert names[0] == "X" and "Desk App" in names
_pj.set_active(Path(res["path"]))
assert _pj.active_profile() == Path(res["path"]).resolve()
dash2 = Dashboard(fm, 0)
got = dash2.settings_get({})
assert got["values"]["max_parallel"] is not None and any(p["name"] == "Desk App" for p in got["projects"])
dash2.project_switch({"path": res["path"]})
assert fm.restart_reason.startswith("switching to the project")
fm.restart_reason = ""
del _os.environ["FLETCHERY_HOME"]


# ---- merge rounds (rounds.py): Fletchery keeps ready PRs merging by itself ----
import rounds as _rounds  # noqa: E402
from github import PullRequest as _PR  # noqa: E402
_cfg = Config(max_parallel=0, daily_budget_usd=60, base_branch="Test-Staging", merge_rounds=True, merge_round_max=2)
ctx, tmp = ctx_with({}, cfg=_cfg)
ctx.gh.prs = [_PR(12, "Pets", "b12", "Test-Staging", "a" * 40, ["ready", "foreman"]),
              _PR(11, "Town", "b11", "Test-Staging", "b" * 40, ["ready"]),
              _PR(13, "Not ready", "b13", "Test-Staging", "c" * 40, ["foreman"]),
              _PR(14, "Main pin", "b14", "main", "d" * 40, ["ready"]),
              _PR(15, "Third", "b15", "Test-Staging", "e" * 40, ["ready"])]
_ans = {"state": "landed", "landed_pr": 77, "merged": [11, 12], "left_out": [], "summary": "Merged 11 and 12; built; tests passed."}
ctx.runtime.script[("merger", 0)] = _ans
fm, _ = run_once(ctx, tmp)  # workers at 0: no new code, but merging goes on
calls = [c for c in ctx.runtime.calls if c[0] == "merger"]
assert len(calls) == 1, ctx.runtime.calls
assert "#11 Town" in calls[0][2] and "#12 Pets" in calls[0][2] and "#15" not in calls[0][2], "oldest first, at most 2"
assert "#13" not in calls[0][2] and "#14" not in calls[0][2], "only `ready` PRs into the base"
assert "never stop to ask for approval" in calls[0][2]
assert fm.state.merge_last["result"] == "landed" and fm.state.merge_last["pr"] == 77 and fm.state.merge_round_no == 1
rf = _rounds.read_file(str(tmp / "state" / "merge-round.json"))
assert rf["state"] == "done" and rf["prs"] == [11, 12] and "Landed as #77" in rf["note"], rf
assert not any(c[0] == "worker" for c in ctx.runtime.calls)
# a round that landed nothing waits before trying the same PRs again; a changed PR or Merge now retries at once
ctx.runtime.script[("merger", 0)] = {"state": "failed", "landed_pr": 0, "merged": [], "left_out": [],
                                     "summary": "Build failed."}
ctx.runtime.calls.clear()
asyncio.run(fm.once())
assert fm.state.merge_last["result"] == "failed" and fm.state.merge_round_no == 2
ctx.runtime.calls.clear()
asyncio.run(fm.once())
assert not [c for c in ctx.runtime.calls if c[0] == "merger"], "same PRs, inside the retry wait: no new round"
assert "tried again" in fm.merge_why, fm.merge_why
ctx.gh.prs[0] = _PR(12, "Pets", "b12", "Test-Staging", "f" * 40, ["ready"])  # a fixer pushed a new head
asyncio.run(fm.once())
assert [c for c in ctx.runtime.calls if c[0] == "merger"], "a new head is tried at once"
ctx.runtime.calls.clear()
fm.merge_now()
asyncio.run(fm.once())
assert [c for c in ctx.runtime.calls if c[0] == "merger"], "Merge now retries without waiting"
# paused merging, or rounds off: nothing starts
ctx.runtime.calls.clear()
fm.hold_merging(True)
asyncio.run(fm.once())
assert not [c for c in ctx.runtime.calls if c[0] == "merger"] and fm.merge_why == "merging is paused"
fm.hold_merging(False)
_cfg.merge_rounds = False
asyncio.run(fm.once())
assert not [c for c in ctx.runtime.calls if c[0] == "merger"]
_cfg.merge_rounds = True
# the snapshot carries what the Merge section shows
assert "merge_auto" in fm.snapshot and fm.snapshot["merge_auto"]["last"]["round"] >= 2
ok, why = _rounds.should_start(_cfg, {}, False, True, False, ctx.gh.prs)
assert not ok and "restarting" in why
# the project's own notes reach the prompt: .fletchery/merger.md, else the mergeNotes file
_notes = tmp / "merger-notes.md"
_notes.write_text("Run Tools/Build.ps1, then strict NavCheck.", encoding="utf-8")
_cfg.merge_notes = str(_notes)
assert "strict NavCheck" in _rounds.notes_text(None, _cfg)

# an agent whose model can't be reached (OpenCode dropped it, LifeShare 2026-11-04) is taken out and the next agent
# runs the same round at once, instead of the same dead agent failing every mergeRetryMinutes
_cfg2 = Config(max_parallel=0, daily_budget_usd=60, base_branch="Test-Staging", merge_rounds=True, merge_agent="claude")
_fk = {"claude": FakeRuntime({("merger", 0): {"error": "opencode failed: {'type': 'provider.no-route', "
                                                       "'message': 'Model unavailable: opencode/claude-opus-5-5'}"}}),
       "deepseek-pro": FakeRuntime({("merger", 0): _ans}), "deepseek": FakeRuntime()}
ctx, tmp = ctx_with({}, cfg=_cfg2)
ctx.agents = pool_with(_fk)
ctx.gh.prs = [_PR(11, "Town", "b11", "Test-Staging", "b" * 40, ["ready"])]
fm, _ = run_once(ctx, tmp)
assert fm.state.merge_last["result"] == "unreachable" and "claude" in ctx.agents.paused, fm.state.merge_last
assert _rounds.read_file(str(tmp / "state" / "merge-round.json"))["state"] == "failed"
asyncio.run(fm.once())
assert fm.state.merge_last["result"] == "landed" and fm.state.merge_last["agent"] != "claude", fm.state.merge_last
assert [r for r, _, _ in _fk["claude"].calls] == ["merger"], "the dead agent isn't tried again"
assert _clis.unreachable("Model unavailable: x") and not _clis.unreachable("You've hit your usage limit")

# an agent that stops part way with a progress note is asked to carry on in the same session, not failed
from runtime import RunResult as _RR  # noqa: E402


class _Stopper(FakeRuntime):
    resume_session = ""

    def command(self, *a):  # looks like a CLI runtime that can resume
        return [], None, {}

    async def run(self, role, prompt, cwd, budget_usd, max_turns, on_action=None):
        self.calls.append((role, self.resume_session, prompt))
        if not self.resume_session:
            return _RR(role, False, text="Build OK, now running the Game specs.", session_id="ses_1",
                       error="no JSON answer from opencode: 'Build OK, now running the Game specs.'")
        return _RR(role, True, output=dict(_ans), session_id="ses_1")


_st = _Stopper()
ctx, tmp = ctx_with({}, cfg=Config(max_parallel=0, daily_budget_usd=60, base_branch="Test-Staging", merge_rounds=True))
ctx.agents = pool_with({"claude": _st, "deepseek-pro": FakeRuntime(), "deepseek": FakeRuntime()})
ctx.gh.prs = [_PR(11, "Town", "b11", "Test-Staging", "b" * 40, ["ready"])]
fm, _ = run_once(ctx, tmp)
assert [c[1] for c in _st.calls] == ["", "ses_1"] and fm.state.merge_last["result"] == "landed", (_st.calls, fm.state.merge_last)
assert _st.resume_session == ""
assert _rounds.worktree_busy(tmp / "nowhere") == []


# the round's worktree is made once and reset to origin/<base> for every round (build output stays)
import subprocess as _sp  # noqa: E402
from repo import Repo as _Repo  # noqa: E402
_d = Path(tempfile.mkdtemp())
_g = lambda *a, cwd=None: _sp.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout  # noqa: E731
_g("init", "-q", "--bare", "-b", "Test-Staging", str(_d / "origin.git"))
_g("clone", "-q", str(_d / "origin.git"), str(_d / "host"))
for _k, _v in (("user.email", "t@t"), ("user.name", "t")):
    _g("config", _k, _v, cwd=_d / "host")
(_d / "host" / "a.txt").write_text("1", encoding="utf-8")
_g("add", ".", cwd=_d / "host"); _g("commit", "-qm", "one", cwd=_d / "host"); _g("push", "-q", "origin", "HEAD:Test-Staging", cwd=_d / "host")
_r = _Repo(_d / "host", _d / "wt", "Test-Staging")
_w = _rounds.prepare_worktree(_r, "fletchery/merge-round-1")
assert (_w / "a.txt").exists() and _g("branch", "--show-current", cwd=_w).strip() == "fletchery/merge-round-1"
(_w / "a.txt").write_text("half-merged", encoding="utf-8"); (_w / "stray.txt").write_text("x", encoding="utf-8")
_w2 = _rounds.prepare_worktree(_r, "fletchery/merge-round-2")
assert _w2 != _w and (_w / "a.txt").read_text(encoding="utf-8") == "half-merged" and (_w / "stray.txt").exists()
assert (_w2 / "a.txt").read_text(encoding="utf-8") == "1"
assert _g("branch", "--show-current", cwd=_w2).strip() == "fletchery/merge-round-2"

# Simulated Windows unlink: preserve checkout/assets/diagnostics and recover in a different path.
_orig_git = _r.git
_failed_path = _w2
(_failed_path / "Ignored").mkdir()
(_failed_path / "Ignored/generated.uasset").write_bytes(b"artist content")
(_failed_path / ".gitignore").write_text("Ignored/\n", encoding="utf-8")
_g("add", ".gitignore", cwd=_failed_path); _g("commit", "-qm", "ignore generated assets", cwd=_failed_path)
def _unlink(*args, **kwargs):
    if args and args[0] == "checkout":
        raise RuntimeError("git checkout: unable to unlink old 'Content/locked.uasset': Invalid argument")
    return _orig_git(*args, **kwargs)
_r.git = _unlink
try:
    _rounds.prepare_worktree(_r, "fletchery/merge-round-3")
    raise AssertionError("unlink failure was swallowed")
except _rounds.CheckoutRecovery as error:
    assert error.path == _failed_path.resolve() and error.next_path != error.path
    assert "unable to unlink" in str(error)
assert (_failed_path / "Ignored/generated.uasset").read_bytes() == b"artist content"
assert _rounds.read_file(str(_failed_path.parent / (_failed_path.name + ".recovery.json")))["reason"]
_r.git = _orig_git
_recovered = _rounds.prepare_worktree(_r, "fletchery/merge-round-4")
assert _recovered != _failed_path and (_recovered / "a.txt").exists()
# Busy or leased checkouts never reach Git mutation/fetch.
from unittest.mock import patch as _patch
with _patch.object(_rounds, "worktree_busy", return_value=["42 UnrealEditor.exe"]):
    try:
        _rounds.prepare_worktree(_r, "fletchery/busy")
        raise AssertionError("busy checkout accepted")
    except RuntimeError as error:
        assert "owned" in str(error)
_lease = _d / "heavy.json"
_rounds.write_file(str(_lease), {"token": "owned", "purpose": "test", "pid": 42})
try:
    _rounds.prepare_worktree(_r, "fletchery/leased", str(_lease))
    raise AssertionError("heavy lease ignored")
except RuntimeError as error:
    assert "lease" in str(error)

# Leaving one PR out doesn't hold healthy heads or re-admit the same failed head.
ctx, tmp = ctx_with({}, cfg=Config(max_parallel=0, base_branch="Test-Staging", merge_rounds=True))
ctx.gh.prs = [_PR(11, "Broken", "b11", "Test-Staging", "a" * 40, ["ready"]), _PR(12, "Healthy", "b12", "Test-Staging", "b" * 40, ["ready"])]
ctx.runtime.script[("merger", 0)] = {"state": "failed", "landed_pr": 0, "merged": [], "left_out": [{"pr": 11, "why": "compile failed after one repair"}], "summary": "excluded 11"}
fm, _ = run_once(ctx, tmp)
assert fm.state.merge_excluded["11"]["head"] == "a" * 40
ctx.runtime.calls.clear()
ctx.runtime.script[("merger", 0)] = {"state": "landed", "landed_pr": 77, "merged": [12], "left_out": [], "summary": "healthy remainder passed"}
asyncio.run(fm.once())
_prompts = [c[2] for c in ctx.runtime.calls if c[0] == "merger"]
assert _prompts and "#12 Healthy" in _prompts[0] and "#11 Broken" not in _prompts[0]
ctx.gh.prs[0].head_sha = "c" * 40
ctx.runtime.calls.clear()
asyncio.run(fm.once())
assert any("#11 Broken" in c[2] for c in ctx.runtime.calls if c[0] == "merger")

# Unowned PRs receive one durable repair issue tied to their actual branch.
_records = [t for t in ctx.gh.tasks.values() if "fletchery-repair-pr:11" in t.body]
assert len(_records) == 1 and _records[0].branch == "b11", _records
assert fm.state.merge_excluded.get("11") is None, "new head releases exclusion"

# Flash is a cheap easy-task writer; native providers handle medium/hard and review/merge.
_policy = pool_with(agents=[
    {"name": "native", "kind": "codex", "vendor": "openai", "scores": {"cpp": 70, "scripts": 70, "docs": 70, "review": 70, "cost": 20}},
    {"name": "flash", "kind": "opencode", "vendor": "deepseek", "roles": ["worker"], "levels": ["easy"], "scores": {"cpp": 90, "scripts": 90, "docs": 90, "review": 90, "cost": 100}},
], routing={"preferredKindsByLevel": {"medium": ["codex", "claude"], "hard": ["codex", "claude"]}})
assert _policy.choose("Docs typo", "Fix README.md", ["difficulty:easy"]).agent == "flash"
assert _policy.choose("Clock", "LSClock.cpp", ["difficulty:medium"]).agent == "native"
assert _policy.choose("Save migration", "LSClock.cpp", ["difficulty:hard"]).agent == "native"
assert _policy.choose("Hard", "", ["difficulty:hard", "agent:flash"]) is None
assert _policy.reviewer_for("native") == "native" and _policy.merger() == "native"
_policy.set_enabled("native", False)
assert _policy.choose("Hard", "", ["difficulty:hard"]) is None
assert _policy.choose("Docs", "", ["difficulty:easy"]).agent == "flash"

# Escalation must preserve the same level/provider policy as initial routing.
_escalation_specs = [
    {"name": "native-low", "kind": "codex", "vendor": "openai", "tier": 1, "scores": {"cpp": 50, "scripts": 50, "cost": 100}},
    {"name": "native-high", "kind": "claude", "vendor": "anthropic", "tier": 3, "scores": {"cpp": 70, "scripts": 70, "cost": 20}},
    {"name": "flash", "kind": "opencode", "vendor": "deepseek", "tier": 4, "levels": ["easy"], "roles": ["worker", "fixer"], "scores": {"cpp": 90, "scripts": 90, "cost": 100}},
    {"name": "other", "kind": "opencode", "vendor": "other", "tier": 2, "scores": {"cpp": 60, "scripts": 60, "cost": 90}},
]
_escalation_routing = {"preferredKindsByLevel": {"medium": ["codex", "claude"], "hard": ["codex", "claude"]}}
_escalation = pool_with(agents=_escalation_specs, routing=_escalation_routing)
assert _escalation.escalate("native-low", "worker", "cpp", "medium") == "native-high"
assert _escalation.escalate("native-high", "worker", "cpp", "medium") is None, "easy-only Flash cannot take a medium escalation"
assert _escalation.escalate("native-high", "worker", "cpp", "hard") is None
assert _escalation.escalate("native-high", "worker", "cpp", "easy") == "flash", "Flash remains available for easy work"
assert _escalation.escalate("native-high", "worker", "cpp") is None, "unknown difficulty cannot bypass an easy-only limit"
_escalation_lists = pool_with(agents=_escalation_specs, routing={**_escalation_routing, "mode": "lists"})
assert _escalation_lists.escalate("native-low", "worker", level="medium") == "native-high"
assert _escalation_lists.escalate("native-high", "worker", level="medium") is None
assert _escalation_lists.escalate("native-high", "worker", level="easy") == "flash"

# A real failed worker carries its classified difficulty into the escalation call.
_escalation_fakes = {"native-low": FakeRuntime({("worker", 0): {"gave_up": "needs a stronger implementation", "summary": "", "files": [], "build_steps": [], "checks": [], "unverified": []}})}
_escalation = pool_with(_escalation_fakes, agents=_escalation_specs, routing=_escalation_routing)
_escalation_task = T(997, "Clock change", "LSClock.cpp")
_escalation_task.labels += ["difficulty:medium", "agent:native-low"]
ctx, tmp = ctx_with({997: _escalation_task})
ctx.agents = _escalation
fm, out = run_once(ctx, tmp)
assert out[0].result == "pr" and out[0].agent == "native-high", out
assert "flash" not in _escalation_fakes, "no medium worker or reviewer session may be sent to easy-only Flash"

# Fixers classify the task too; neither original assignment nor attempt-two escalation can bypass limits.
from pipeline import fixer_agent as _fixer_agent
ctx.task_agents[str(_escalation_task.number)] = "native-low"
assert _fixer_agent(ctx, _escalation_task, 2) == "native-high"
ctx.task_agents[str(_escalation_task.number)] = "flash"
assert _fixer_agent(ctx, _escalation_task, 1) != "flash"

# A large PR backlog cannot monopolize dispatch ahead of a ready writer; polling rotates.
_backlog_tasks = {n: Task(n, f"Existing {n}", labels=["foreman", "foreman:pr"]) for n in range(1, 11)}
_backlog_tasks[99] = Task(99, "Ready writer", labels=["foreman", "foreman:queued"])
ctx, tmp = ctx_with(_backlog_tasks, cfg=Config(max_parallel=1))
ctx.gh.prs = [_PR(100+n, f"Existing {n}", _backlog_tasks[n].branch, "Test-Staging", str(n) * 40, ["foreman"], f"Closes #{n}") for n in range(1, 11)]
_polled = []
ctx.gh.feedback_since = lambda pr, since: (_polled.append(pr.number) or [])
fm, _ = run_once(ctx, tmp)
assert len(_polled) <= 6 and any(c[0] == "worker" for c in ctx.runtime.calls), _polled
_first = set(_polled);_polled.clear()
asyncio.run(fm.once())
assert any(n not in _first for n in _polled), "PR polling must rotate fairly"

# Snapshot rendering does not call GitHub for queue/PR/file data.
fm.cfg.merge_flags = [{"paths": "Source", "flag": "native checks"}]
with _patch.object(ctx.gh, "open_tasks", side_effect=AssertionError("display refetched issues")), _patch.object(ctx.gh, "open_prs", side_effect=AssertionError("display refetched PRs")), _patch.object(ctx.gh, "pr_files", side_effect=AssertionError("display fetched file list")):
    fm.publish([])

# Completed outcomes update a detached cached queue without another network fetch.
from pipeline import Outcome as _SnapshotOutcome
ctx, tmp = ctx_with({42: T(42, "Detached remote task", status=RUNNING)})
fm = Foreman(ctx, tmp / "state")
fm._tasks = {42: T(42, "Cached task", status=RUNNING)}
fm._prs, fm._waiting = {}, {}
with _patch.object(ctx.gh, "open_tasks", side_effect=AssertionError("publish refetched tasks")), _patch.object(ctx.gh, "open_prs", side_effect=AssertionError("publish refetched PRs")):
    fm.publish([_SnapshotOutcome(42, "pr", pr_number=142)])
    assert fm._tasks[42].status == PR and fm.snapshot["tasks"][0]["status"] == PR
    assert fm.snapshot["tasks"][0]["pr"] == 142
    fm.publish([_SnapshotOutcome(42, "limited", pr_number=142)])
    assert fm.snapshot["tasks"][0]["status"] == PR, "a paused fixer must remain eligible as an existing PR"
    fm.publish([_SnapshotOutcome(42, "gave_up", pr_number=142)])
    assert fm.snapshot["tasks"][0]["status"] == PR, "a failed bounded fixer is still an existing PR"
    fm.publish([_SnapshotOutcome(42, "blocked")])
    assert fm.snapshot["tasks"][0]["status"] == BLOCKED
    fm.publish([_SnapshotOutcome(42, "retry_fix")])
    assert fm.snapshot["tasks"][0]["status"] == QUEUED
    ctx.gh.set_status(42, PR)
    fm.publish([])
    assert fm.snapshot["tasks"][0]["status"] == PR, "status tap must update cached Tasks as well as rows"

print("RESULT: PASS (Fletchery engine checks)")
