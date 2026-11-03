"""The lane watcher: builds that break outside a worker's own PR become tasks.

A failed run on the build PC (or in GitHub Actions, for a project without the local runner) that nothing newer has
fixed is filed as an issue with the project's label:

  - a code error (compile, test, check script)  -> "<label>:queued", so a worker takes it like any other task
  - the PC, GitHub or the lane itself           -> "<label>:blocked", so it shows under Needs you

One issue per distinct failure: the same failure again adds a comment instead. An issue nobody has started is closed by
itself when a newer run of the same build passes. Pull-request runs are not filed: a worker's PR gets its failures
through the fixer (see `enrich`, which gives the fixer the failing step's log), and other people's PRs are theirs.

The issue carries the failing step's log, so a worker can act without access to the build PC."""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import graph

PR_EVENTS = {"pull_request", "pull_request_target", "merge_group"}
MARK = re.compile(r"<!-- fletchery-failure sig=(\w+) key=(\S+) run=(\d+) -->")


@dataclass
class Job:
    id: str
    name: str
    conclusion: str
    steps: list[tuple[str, str]] = field(default_factory=list)  # (name, conclusion)


@dataclass
class Run:
    id: str
    workflow: str        # file name, e.g. ci.yml
    name: str            # display name, e.g. Build and test
    event: str
    branch: str
    sha: str
    conclusion: str      # success | failure | timed_out | cancelled | skipped | ""
    created_at: str      # ISO, UTC
    url: str = ""
    jobs: list[Job] = field(default_factory=list)

    @property
    def key(self) -> str:
        """Runs that answer the same question: same workflow on the same branch. One-off branches the lane makes per
        checkpoint or batch (`lane/checkpoint-control-<uuid>`) count as one branch."""
        b = re.sub(r"[-/_]?[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$", "", self.branch or "")
        b = re.sub(r"[-/_]\d+$", "", b)
        return f"{self.workflow}@{b}"


# ---- where runs come from ----

class LocalRunnerRuns:
    """The local runner's run store: <root>\\runs\\<id>\\run.json and logs\\<job id>.log."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.label = f"the build PC ({self.root})"

    def _read(self, d: Path) -> Run | None:
        try:
            j = json.loads((d / "run.json").read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return None
        jobs = [Job(str(x.get("id")), x.get("name") or "", x.get("conclusion") or "",
                    [(s.get("name") or "", s.get("conclusion") or "") for s in x.get("steps") or []])
                for x in j.get("jobs") or []]
        return Run(str(j.get("id") or d.name), Path(j.get("path") or j.get("workflow_file") or "").name,
                   j.get("name") or "", j.get("event") or "", j.get("head_branch") or "", j.get("head_sha") or "",
                   j.get("conclusion") or "", j.get("created_at") or "", j.get("html_url") or "", jobs)

    def recent(self, limit: int = 400) -> list[Run]:
        runs_dir = self.root / "runs"
        if not runs_dir.is_dir():
            return []
        dirs = sorted((d for d in runs_dir.iterdir() if d.is_dir() and d.name.isdigit()), key=lambda d: int(d.name),
                      reverse=True)[:limit]
        return [r for r in (self._read(d) for d in dirs) if r]

    def get(self, run_id: str) -> Run | None:
        d = self.root / "runs" / str(run_id)
        return self._read(d) if d.is_dir() else None

    def log(self, run: Run, job: Job) -> str:
        p = self.root / "runs" / run.id / "logs" / f"{job.id}.log"
        try:
            return p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def where(self, run: Run) -> str:
        return f"run {run.id} on the build PC (`node buildlane-runner.cjs show {run.id}`)"


class GitHubRuns:
    """GitHub Actions, for a project whose builds run there."""

    def __init__(self, gh):
        self.gh = gh
        self.label = "GitHub Actions"

    def _run(self, r: dict) -> Run:
        return Run(str(r["id"]), Path(r.get("path") or "").name, r.get("name") or "", r.get("event") or "",
                   r.get("head_branch") or "", r.get("head_sha") or "", r.get("conclusion") or "",
                   r.get("created_at") or "", r.get("html_url") or "")

    def recent(self, limit: int = 100) -> list[Run]:
        d = self.gh.api(f"repos/{self.gh.repo}/actions/runs?per_page={min(limit, 100)}") or {}
        return [self._run(r) for r in d.get("workflow_runs", [])]

    def _jobs(self, run: Run) -> None:
        if run.jobs:
            return
        d = self.gh.api(f"repos/{self.gh.repo}/actions/runs/{run.id}/jobs?per_page=100") or {}
        run.jobs = [Job(str(j["id"]), j.get("name") or "", j.get("conclusion") or "",
                        [(s.get("name") or "", s.get("conclusion") or "") for s in j.get("steps") or []])
                    for j in d.get("jobs", [])]

    def get(self, run_id: str) -> Run | None:
        r = self.gh.api(f"repos/{self.gh.repo}/actions/runs/{run_id}")
        return self._run(r) if r else None

    def log(self, run: Run, job: Job) -> str:
        try:
            return self.gh.api_text(f"repos/{self.gh.repo}/actions/jobs/{job.id}/logs")
        except Exception:
            return ""

    def where(self, run: Run) -> str:
        return f"[run {run.id}]({run.url})" if run.url else f"run {run.id}"


def find_runner_root(repo: str, explicit: str = "") -> str:
    """The local runner's run store for this repository: the profile's `runnerRoot`, else the runner's own config
    (%USERPROFILE%\\buildlane-local\\runner.json) when it serves this repository."""
    if explicit:
        return os.path.expandvars(explicit)
    cfg = Path.home() / "buildlane-local" / "runner.json"
    try:
        data = json.loads(cfg.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return ""
    names = {str(n).lower() for n in [data.get("repo", ""), *(data.get("repoAliases") or [])]}
    if repo.lower() not in names or not data.get("root"):
        return ""
    return os.path.expandvars(str(data["root"]))


# ---- reading a failure ----

_STAMP = re.compile(r"^﻿?\d{4}-\d\d-\d\dT[\d:.]+Z ?")


def excerpt(log: str, step: str, max_lines: int = 60) -> str:
    """The failing step's own output, up to its error: not the checkout chatter or the cleanup after it."""
    lines = [_STAMP.sub("", l.rstrip()) for l in log.splitlines()]
    lines = [l for l in lines if not l.startswith("##[debug]")]
    start = None
    for i, l in enumerate(lines):
        if l.startswith("##[group]"):
            title = l[len("##[group]"):].strip()
            if step and (title == step or title == f"Run {step}" or title.startswith(step)):
                start = i
    errors = [i for i, l in enumerate(lines) if l.startswith("##[error]") and (start is None or i > start)]
    if start is None:
        if not errors:
            return "\n".join(lines[-max_lines:])
        end = errors[0]
        return "\n".join(lines[max(0, end - 40):end + 3])
    end = errors[0] if errors else min(len(lines), start + max_lines)
    window = lines[start:end + 1]
    return "\n".join(window[-max_lines:])


# Infrastructure first: a rate limit inside a test step is still not the code's fault.
INFRA = [
    (r"rate limit", "GitHub API rate limit"),
    (r"HttpError: (5\d\d|Bad credentials|Not Found)|\b50[234]\b.*(Gateway|Unavailable)|Server Error", "GitHub API error"),
    (r"Bad credentials|Authentication failed|could not read Username|Permission denied \(publickey\)", "sign-in problem"),
    (r"Could not resolve host|ECONNRESET|ETIMEDOUT|EAI_AGAIN|Connection (reset|refused|timed out)|unable to access 'https", "network problem"),
    (r"No space left|not enough space|disk (is )?full|ENOSPC", "the PC is out of disk space"),
    (r"Could not claim build status|resource lease|lost communication with the server|runner (has )?(received a shutdown|lost)", "the build lane or runner"),
    (r"The operation was canceled|exceeded the maximum execution time|timed out after", "a timeout"),
    (r"LFS|smudge filter|fatal: (unable to|could not) (fetch|read)", "fetching the repository"),
    (r"Access to the path .* is denied|being used by another process|cannot access the file", "a locked file on the PC"),
]
INFRA_STEPS = re.compile(r"^(Run )?(actions/checkout|Check ?out|Set up job|Wait for the PC|Acquire .*lease|Claim |Load trusted|Bind |Release |Preserve )", re.I)
CODE = [
    r"\berror (C|LNK|MSB)\d{3,5}\b", r"\): error\b|: error [A-Z]*\d*:", r"LogCompile: Error|UnrealBuildTool.*(error|failed)",
    r"^FAIL\b|\bFAILED\b|Test(s)? failed|tests? failed|AssertionError|assert(ion)? failed|Expected .* (but|got|to )",
    r"FullyQualifiedErrorId : (Expected|Assert)", r"Traceback \(most recent call last\)",
    r"SyntaxError|TypeError|ReferenceError|NameError|ParserError|is not recognized as the name of a cmdlet",
    r"RESULT: FAIL", r"npm ERR! (Test|code ELIFECYCLE)", r"error TS\d+", r"Compile.*failed|Build failed",
]


def classify(text: str, step: str) -> tuple[str, str]:
    """("code" | "infra", why)."""
    for pat, why in INFRA:
        if re.search(pat, text, re.I | re.M):
            return "infra", why
    if INFRA_STEPS.search(step or ""):
        return "infra", f"the '{step}' step, which sets up the build rather than testing code"
    for pat in CODE:
        if re.search(pat, text, re.M):
            return "code", "a compile, test or check failure"
    return "infra", "no compile or test error in the log"


def first_error(text: str) -> str:
    for l in text.splitlines():
        s = l.strip()
        if re.search(r"error|FAIL|Exception|Expected|Traceback|denied|limit", s, re.I) and not s.startswith("##[group]"):
            return s.replace("##[error]", "").strip()[:160]
    return ""


def signature(run: Run, job: str, step: str, text: str) -> str:
    norm = first_error(text).lower()
    norm = re.sub(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}", "u", norm)
    norm = re.sub(r"\b[0-9a-f]{7,40}\b", "h", norm)
    norm = re.sub(r"\d+", "#", norm)
    norm = re.sub(r"(request id|request-id)\S*.*", "", norm)
    return hashlib.sha1(f"{run.workflow}|{job}|{step}|{norm}".encode()).hexdigest()[:12]


def _age_minutes(iso: str, now: datetime) -> float:
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return 1e9
    return (now - t).total_seconds() / 60


# ---- the watcher ----

class Watcher:
    def __init__(self, cfg, gh, source, log=print, grace_minutes: float = 10, max_age_hours: float = 24,
                 max_new: int = 3, clock=lambda: datetime.now(timezone.utc)):
        self.cfg, self.gh, self.source, self.log = cfg, gh, source, log
        self.grace, self.max_age, self.max_new, self.clock = grace_minutes, max_age_hours, max_new, clock
        self.skip = {w.strip() for w in getattr(cfg, "watch_skip_workflows", []) or [] if w.strip()}
        self.recent: list[dict] = []  # what it filed or noticed lately, newest first (the dashboard)

    def _note(self, what: str, **kw) -> None:
        self.recent = ([{"at": self.clock().isoformat(timespec="seconds"), "what": what, **kw}] + self.recent)[:20]
        self.log(what)

    def watch(self, seen: list[str]) -> list[str]:
        """One look. `seen` (run ids already handled) is updated in place. Returns what it did, one line each."""
        now = self.clock()
        runs = [r for r in self.source.recent() if r.event not in PR_EVENTS and r.workflow not in self.skip]
        newest: dict[str, Run] = {}
        last_pass: dict[str, Run] = {}
        running: dict[str, Run] = {}  # a run still going, newer than the newest result
        for r in sorted(runs, key=lambda r: (r.created_at, int(r.id) if r.id.isdigit() else 0)):
            if r.conclusion in ("success", "failure", "timed_out"):
                newest[r.key] = r
                running.pop(r.key, None)
            elif not r.conclusion and _age_minutes(r.created_at, now) < 6 * 60:  # older: stuck, not running
                running[r.key] = r
            if r.conclusion == "success":
                last_pass[r.key] = r
        tasks = self.gh.open_tasks()
        mine = {}  # sig -> (task, key, run id)
        for t in tasks.values():
            m = MARK.search(t.body or "")
            if m:
                mine[m.group(1)] = (t, m.group(2), m.group(3))
        did: list[str] = []
        # 1. fixed by itself: a newer run of the same build passed and nobody has started on the issue
        for sig, (t, key, run_id) in list(mine.items()):
            r = newest.get(key)
            if r and r.conclusion == "success" and _num(r.id) > _num(run_id) and t.status in (graph.QUEUED, graph.BLOCKED):
                self.gh.comment(t.number, f"Fletchery: {r.name or r.workflow} passed again in {self.source.where(r)}; "
                                          f"closing this, nothing to do.")
                self.gh.close_issue(t.number)
                del mine[sig]
                self._note(f"#{t.number} closed: {key} passes again", issue=t.number, kind="closed")
                did.append(f"closed #{t.number}")
        # 2. builds whose newest result is a failure
        filed = 0
        for key, r in newest.items():
            if r.conclusion == "success" or r.id in seen:
                continue
            age = _age_minutes(r.created_at, now)
            if age > self.max_age * 60:
                seen.append(r.id)
                continue
            if age < self.grace or r.key in running:  # a quick re-run or the next run may fix it: wait for it
                continue
            seen.append(r.id)
            if isinstance(self.source, GitHubRuns):
                self.source._jobs(r)
            failed = [j for j in r.jobs if j.conclusion in ("failure", "timed_out")] or r.jobs[:1]
            for job in failed:
                step = next((n for n, c in job.steps if c in ("failure", "timed_out")), "")
                text = excerpt(self.source.log(r, job), step) if job.id else ""
                kind, why = classify(text, step)
                sig = signature(r, job.name, step, text)
                if sig in mine:
                    t = mine[sig][0]
                    self.gh.comment(t.number, f"Fletchery: failed the same way again in {self.source.where(r)} "
                                              f"(`{r.branch}` at `{r.sha[:10]}`).")
                    self._note(f"#{t.number}: {key} failed again", issue=t.number, kind="again")
                    did.append(f"again #{t.number}")
                    continue
                if filed >= self.max_new:
                    seen.remove(r.id) if r.id in seen else None  # look again next time
                    break
                n = self._file(r, job, step, text, kind, why, sig, last_pass.get(key))
                mine[sig] = (None, key, r.id)
                filed += 1
                where = "queued for a worker" if kind == "code" else "needs you"
                self._note(f"#{n} filed: {r.name or r.workflow} / {job.name} failed ({why}); {where}",
                           issue=n, kind=kind)
                did.append(f"filed #{n} ({kind})")
        del seen[:-500]
        return did

    def _file(self, r: Run, job: Job, step: str, text: str, kind: str, why: str, sig: str, good: Run | None) -> int:
        what = r.name or r.workflow
        part = f"{job.name} / {step}" if step else job.name
        if kind == "code":
            title = f"Fix the build: {what} fails at {step or job.name}"
        else:
            title = f"Build PC problem: {what} fails at {step or job.name}"
        lines = []
        if kind == "code":
            lines += [f"**{what}** failed on {self.source.label} and nothing newer has passed since. "
                      f"This task is to make it pass again.", ""]
        else:
            lines += [f"**{what}** failed on {self.source.label}, and it looks like {why}, not the code. "
                      f"A person needs to look; no worker has been started.", ""]
        lines += [f"- Run: {self.source.where(r)}: `{r.workflow}`, {r.event} on `{r.branch}` at `{r.sha[:10]}`, "
                  f"{r.created_at[:16].replace('T', ' ')} UTC",
                  f"- Failed: **{part}**", "",
                  "Log from the failing step:", "", "```text", (text or "(no log was kept)").replace("```", "'''"),
                  "```", ""]
        commits = self._commits(good, r) if kind == "code" else []
        if commits:
            lines += [f"Commits since it last passed (`{good.sha[:10]}`):", *commits, ""]
        elif kind == "code" and not good:
            lines += ["No earlier passing run of this build to compare with.", ""]
        if kind == "code":
            lines += ["Fix the cause in the code. Do not skip, weaken or delete the check to make it pass. If the log "
                      "shows the failure is not caused by the code (the build PC, GitHub, a flaky test), say so in a "
                      "comment on this issue and stop.", ""]
        else:
            lines += ["When it is sorted, close this issue. If it is a code problem after all, change its label to "
                      f"`{graph.label_for(graph.QUEUED)}` and a worker will take it. Fletchery also closes it by "
                      "itself once the same build passes.", ""]
        lines += ["_Filed by Fletchery's lane watcher._", f"<!-- fletchery-failure sig={sig} key={r.key} run={r.id} -->"]
        status = graph.QUEUED if kind == "code" else graph.BLOCKED
        return self.gh.create_issue(title[:120], "\n".join(lines), [graph.PREFIX, graph.label_for(status)])

    def _commits(self, good: Run | None, bad: Run) -> list[str]:
        if not good or not good.sha or not bad.sha or good.sha == bad.sha:
            return []
        try:
            commits = self.gh.compare_commits(good.sha, bad.sha)
        except Exception:
            return []
        out = [f"- `{c['sha'][:10]}` {c['message'].splitlines()[0][:100]} ({c.get('author', '?')})" for c in commits[-15:]]
        if len(commits) > 15:
            out.insert(0, f"- … {len(commits) - 15} earlier")
        return out

    # ---- a worker's PR failed on the build PC: hand the fixer the log, not only "failed" ----
    def enrich(self, text: str, context: str) -> str:
        m = re.search(r"\brun (\d{6,})\b", text)
        if not m or not hasattr(self.source, "get"):
            return text
        try:
            r = self.source.get(m.group(1))
            if r is None:
                return text
            if isinstance(self.source, GitHubRuns):
                self.source._jobs(r)
            job = next((j for j in r.jobs if j.name == context), None) or \
                next((j for j in r.jobs if j.conclusion in ("failure", "timed_out")), None)
            if job is None:
                return text
            step = next((n for n, c in job.steps if c in ("failure", "timed_out")), "")
            log = excerpt(self.source.log(r, job), step, max_lines=50)
        except Exception:
            return text
        if not log:
            return text
        return f"{text}\n\nFailing step: {job.name} / {step}\n```text\n{log}\n```"


def _num(s: str) -> int:
    return int(s) if str(s).isdigit() else 0


def make(cfg, gh, log=print) -> Watcher | None:
    """The watcher this project gets: the local runner's store when it serves this repository, else GitHub Actions."""
    if not getattr(cfg, "watch_failures", True):
        return None
    root = find_runner_root(cfg.repo, getattr(cfg, "runner_root", ""))
    if root and Path(root).is_dir():
        source = LocalRunnerRuns(root)
    elif hasattr(gh, "api"):
        source = GitHubRuns(gh)
    else:  # the in-memory fake (tests, demo): nothing to watch
        return None
    return Watcher(cfg, gh, source, log)
