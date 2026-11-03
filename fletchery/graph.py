"""The task graph: open GitHub issues with the project's task label, linked by `Depends on: #n` lines.

Pure functions over plain data so the graph can be tested without GitHub (test_foreman.py) and printed by `--dry-run`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# The task label is per project (profile `workers.label`, LifeShare: "foreman"); cli.py sets PREFIX at start. Inside the
# engine a task's state is one of the plain words below; only the GitHub layer turns them into labels ("foreman:queued").
PREFIX = "fletchery"
QUEUED, RUNNING, PR, BLOCKED = "queued", "running", "pr", "blocked"
STATES = (QUEUED, RUNNING, PR, BLOCKED)


def label_for(state: str) -> str:
    return f"{PREFIX}:{state}"


def is_status_label(name: str) -> bool:
    return name.startswith(PREFIX + ":") and name.split(":", 1)[1] in STATES

_DEPENDS = re.compile(r"^\s*depends\s+on\s*:\s*(.*)$", re.IGNORECASE | re.MULTILINE)
_GATE = re.compile(r"^\s*gate\s*:\s*(.*)$", re.IGNORECASE | re.MULTILINE)
_IMPORTS = re.compile(r"^\s*lane-imports\s*:\s*(.*)$", re.IGNORECASE | re.MULTILINE)
_ISSUE_REF = re.compile(r"#(\d+)")


@dataclass
class Task:
    number: int
    title: str
    body: str = ""
    labels: list[str] = field(default_factory=list)
    closed: bool = False
    created_at: str = ""
    pr_number: int | None = None  # the open PR that says "Closes #<number>", if any
    notes: str = ""               # people's comments on the issue, filled just before a session (pipeline.py)
    rules: object = None          # the project's rules (rules.Rules), loaded from the task's worktree

    @property
    def depends_on(self) -> list[int]:
        out: list[int] = []
        for m in _DEPENDS.finditer(self.body or ""):
            out += [int(n) for n in _ISSUE_REF.findall(m.group(1))]
        return sorted(set(out))

    @property
    def gates(self) -> list[str]:
        out: list[str] = []
        for m in _GATE.finditer(self.body or ""):
            out += [g.strip() for g in m.group(1).split(",") if g.strip()]
        return out

    @property
    def lane_imports(self) -> str:
        m = _IMPORTS.search(self.body or "")
        return m.group(1).strip() if m else ""

    @property
    def status(self) -> str:
        for s in STATES:
            if label_for(s) in self.labels:
                return s
        return QUEUED  # a task issue with no status label is new work

    @property
    def slug(self) -> str:
        s = re.sub(r"[^a-z0-9]+", "-", self.title.lower()).strip("-")
        return s[:40].rstrip("-") or "task"

    @property
    def branch(self) -> str:
        return f"claude/{PREFIX}-{self.number}-{self.slug}"


@dataclass
class Readiness:
    ready: list[Task]
    waiting: dict[int, str]  # task number -> why it is not ready (for the status issue)


def find_cycles(tasks: dict[int, Task]) -> set[int]:
    """Numbers of every task that sits on a dependency cycle (only among open tasks)."""
    on_cycle: set[int] = set()
    WHITE, GREY, BLACK = 0, 1, 2
    colour = {n: WHITE for n in tasks}
    stack: list[int] = []

    def visit(n: int) -> None:
        colour[n] = GREY
        stack.append(n)
        for d in tasks[n].depends_on:
            if d not in tasks:
                continue
            if colour[d] == GREY:
                on_cycle.update(stack[stack.index(d):])
            elif colour[d] == WHITE:
                visit(d)
        stack.pop()
        colour[n] = BLACK

    for n in tasks:
        if colour[n] == WHITE:
            visit(n)
    return on_cycle


def readiness(tasks: dict[int, Task], known_closed: set[int] | None = None) -> Readiness:
    """Which queued tasks may run now. `tasks` are the open `foreman` issues; `known_closed` are issue numbers that are
    closed (dependencies that are done). A dependency that is neither open-and-known nor closed is treated as missing."""
    known_closed = known_closed or set()
    cycles = find_cycles(tasks)
    ready: list[Task] = []
    waiting: dict[int, str] = {}
    for n, t in sorted(tasks.items()):
        if t.status != QUEUED:
            continue
        if n in cycles:
            waiting[n] = "dependency cycle"
            continue
        open_deps, missing = [], []
        for d in t.depends_on:
            if d in known_closed:
                continue
            if d in tasks:
                open_deps.append(d)
            else:
                missing.append(d)
        if missing:
            waiting[n] = "depends on missing issue " + ", ".join(f"#{d}" for d in missing)
        elif open_deps:
            waiting[n] = waits_text(tasks, open_deps)
        else:
            ready.append(t)
    ready.sort(key=lambda t: (t.created_at, t.number))
    return Readiness(ready, waiting)


def waits_text(tasks: dict[int, Task], deps: list[int]) -> str:
    return "waits for " + ", ".join(f"#{d} ({_dep_state(tasks[d])})" for d in deps)


def refresh_waits(tasks: dict[int, Task], waiting: dict[int, str]) -> dict[int, str]:
    """Re-describe the dependencies of "waits for" reasons from `tasks` as they are now (a dependency may have opened its
    PR since the reason was written earlier in the pass)."""
    out = dict(waiting)
    for n, why in waiting.items():
        if why.startswith("waits for ") and n in tasks:
            deps = [d for d in tasks[n].depends_on if d in tasks]
            if deps:
                out[n] = waits_text(tasks, deps)
    return out


def _dep_state(t: Task) -> str:
    """Where a dependency is, in words, so a blocker is never just a bare number (LifeShare #780 waited in the Merge
    list as PR #920 while the queue only said "waits for #780", 2026-11-03)."""
    if t.status == PR:
        return f"PR #{t.pr_number} waiting to merge" if t.pr_number else "PR in the lane"
    if t.status == RUNNING:
        return "being worked on"
    if t.status == BLOCKED:
        return "blocked, needs you"
    return "queued"


def unblocks(tasks: dict[int, Task], number: int) -> list[int]:
    """Every open task that waits, directly or through others, on `number`: what merging its PR sets moving."""
    found: set[int] = set()
    todo = [number]
    while todo:
        n = todo.pop()
        for t in tasks.values():
            if n in t.depends_on and t.number not in found and t.number != number:
                found.add(t.number)
                todo.append(t.number)
    return sorted(found)


_ROUND = re.compile(r"^round[\s:_-]*(\d+)$", re.IGNORECASE)


def merge_round(labels: list[str]) -> str:
    """A release round a PR was put in, from a `round:7` / `round-7` label on the PR ("" when none)."""
    for l in labels:
        m = _ROUND.match(l.strip())
        if m:
            return m.group(1)
    return ""


def pick(ready: list[Task], free_slots: int) -> list[Task]:
    return ready[: max(0, free_slots)]
