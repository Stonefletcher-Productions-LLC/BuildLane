"""Which coding agent does a task: the agents a project offers (profile `workers.agents`), the router that picks one per
task (`workers.routing`), escalation to a stronger agent when one fails, a reviewer from another vendor, and the success
rates that let the router learn.

    "agents": [
      {"name": "claude",   "kind": "claude",   "tier": 4, "vendor": "anthropic"},
      {"name": "codex",    "kind": "codex",    "tier": 3, "vendor": "openai"},
      {"name": "deepseek-pro", "kind": "opencode", "model": "opencode-go/deepseek-v4-pro",     "tier": 2, "vendor": "deepseek"},
      {"name": "deepseek",     "kind": "opencode", "model": "opencode-go/deepseek-v4.1-flash", "tier": 1, "vendor": "deepseek"}
    ],
    "routing": {"easy": ["deepseek", "deepseek-pro"], "medium": ["codex", "claude"], "hard": ["claude", "codex"],
                "reviewers": ["claude", "codex"]}

A profile with no `agents` block has one agent, Claude, and behaves exactly as Fletchery always has.

Choosing an agent for a task, in order:
1. An override on the issue: a label `agent:<name>` or a line `Agent: <name>`.
2. The task's difficulty (`classify`): a `Difficulty: easy|medium|hard` line or `difficulty:<level>` label, else the
   routing patterns and the issue's shape. Each difficulty has a preference list.
3. Learning (`Stats`, from the ledger): once a cheaper agent has passed enough tasks of this difficulty it is tried
   first; an agent that keeps failing them drops to the back of the list.
4. Availability: an agent paused by a usage limit or a lost sign-in, or already running its `maxParallel` tasks, is
   skipped.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

EASY, MEDIUM, HARD = "easy", "medium", "hard"
LEVELS = (EASY, MEDIUM, HARD)


@dataclass
class AgentSpec:
    name: str
    kind: str = "claude"            # claude | codex | opencode | fake
    model: str = ""                 # "" = the agent's own default
    tier: int = 4                   # higher is stronger (and usually dearer); escalation goes up
    vendor: str = ""                # reviewers come from a different vendor than the worker
    enabled: bool = True
    max_parallel: int = 0           # 0 = no limit of its own (the project's maxParallel still applies)
    command: str = ""               # path to the CLI; "" = found automatically
    roles: list = None              # roles it may take; None = all
    timeout_minutes: dict = None    # role -> minutes, for CLIs without a turn limit
    key_env: list = None            # stored keys (keys.py) this agent's sessions get, e.g. ["DEEPSEEK_API_KEY"]

    @property
    def label(self) -> str:
        return f"{self.name} ({self.model})" if self.model else self.name

    def can(self, role: str) -> bool:
        return self.enabled and (self.roles is None or role in self.roles)


_SPEC_KEYS = {"name": "name", "kind": "kind", "model": "model", "tier": "tier", "vendor": "vendor", "enabled": "enabled",
              "maxParallel": "max_parallel", "command": "command", "roles": "roles", "timeoutMinutes": "timeout_minutes",
              "keyEnv": "key_env"}
DEFAULT_VENDOR = {"claude": "anthropic", "codex": "openai"}


def specs_from(raw: list | None, default_model: str = "") -> list[AgentSpec]:
    """The profile's `agents` list; none means one Claude agent (Fletchery before agents existed)."""
    if not raw:
        return [AgentSpec("claude", "claude", default_model, 4, "anthropic")]
    out = []
    for item in raw:
        s = AgentSpec(name=str(item.get("name") or item.get("kind") or "agent"))
        for k, v in item.items():
            if k in _SPEC_KEYS:
                setattr(s, _SPEC_KEYS[k], v)
        if not s.vendor:
            s.vendor = DEFAULT_VENDOR.get(s.kind) or (s.model.split("/")[-1].split("-")[0] if s.model else s.kind)
        if s.kind == "claude" and not s.model:
            s.model = default_model
        out.append(s)
    return out


# ---- difficulty ----

# Signs that a task is hard: work a cheap model gets subtly wrong and the Build Lane catches late. LifeShare-flavoured
# defaults; a profile replaces them with `routing.hardPatterns` / `routing.easyPatterns`.
HARD_PATTERNS = [
    r"\bsave (version|format|game)|\bmigration\b|SaveVersion",
    r"\breplicat|\bmultiplayer\b|\bnetwork(ing)?\b|\bRPC\b|\bonline\b|\bsession\b",
    r"\bnav(mesh|check)\b|\bNavCheck\b|\bpathfinding\b",
    r"\bthread(s|ing)?\b|\basync\b|\brace\b|\bdeadlock\b",
    r"\bperformance\b|\bperf\b|\bmemory leak\b|\bGC\b",
    r"\b(Folk|Folks) (sim|AI|brain|needs|autonomy)\b|\bautonomy\b|\butility AI\b",
    r"\bLane-Imports\s*:",
    r"\barchitecture\b|\brefactor\b|\bredesign\b",
]
EASY_PATTERNS = [
    r"\btypo\b|\bspelling\b|\bwording\b|\bcopy (change|edit)\b|\brename\b",
    r"\bdocs?\b|\bdocumentation\b|\bREADME\b|\.md\b",
    r"\bstring table\b|\bLSStrings\b|\.csv\b|\blocali[sz]",
    r"\b(unit )?tests? (for|only)\b|\badd (a )?tests?\b|\bspec(s)? only\b",
    r"\bcomment(s)?\b|\blog(ging)? (line|message)\b",
    r"\b(python|\.py)\b.*\b(script|tool)\b|\bTools/\w+\.py\b",
]
_LEVEL_LINE = re.compile(r"^\s*difficulty\s*:\s*(easy|medium|hard)\b", re.IGNORECASE | re.MULTILINE)
_AGENT_LINE = re.compile(r"^\s*agent\s*:\s*([\w.\-]+)", re.IGNORECASE | re.MULTILINE)
_CODE_REF = re.compile(r"\b[\w/]+\.(cpp|h|hpp|cs|ts|tsx|js|cjs|py|ps1)\b")


def classify(title: str, body: str, labels: list[str], hard: list[str] | None = None,
             easy: list[str] | None = None, depends: int = 0) -> tuple[str, str]:
    """(difficulty, why). Rules in order: an explicit level, a hard sign, a big issue, easy signs, else medium."""
    for l in labels:
        if l.lower().startswith("difficulty:") and l.split(":", 1)[1].lower() in LEVELS:
            return l.split(":", 1)[1].lower(), f"label {l}"
    m = _LEVEL_LINE.search(body or "")
    if m:
        return m.group(1).lower(), "Difficulty line"
    text = f"{title}\n{body or ''}"
    for p in (hard if hard is not None else HARD_PATTERNS):
        hit = re.search(p, text, re.IGNORECASE)
        if hit:
            return HARD, f"mentions \"{hit.group(0).strip()}\""
    files = {m.group(0) for m in _CODE_REF.finditer(text)}
    native = [f for f in files if f.endswith((".cpp", ".h", ".hpp"))]
    if len(text) > 4000 or len(native) >= 4 or depends >= 3:
        return HARD, f"big task ({len(text)} chars, {len(native)} C++ files, {depends} dependencies)"
    easy_hits = [h.group(0).strip() for p in (easy if easy is not None else EASY_PATTERNS)
                 for h in [re.search(p, text, re.IGNORECASE)] if h]
    if easy_hits and not native and len(text) < 2500:
        return EASY, "looks small: " + ", ".join(f'"{h}"' for h in easy_hits[:3])
    return MEDIUM, "ordinary change" + (f" ({len(native)} C++ files)" if native else "")


def override(body: str, labels: list[str]) -> str:
    for l in labels:
        if l.lower().startswith("agent:"):
            return l.split(":", 1)[1].strip()
    m = _AGENT_LINE.search(body or "")
    return m.group(1) if m else ""


# ---- learning ----

@dataclass
class Stats:
    """(agent, difficulty) -> [passed, tried], from the ledger's `attempts`. An attempt passes when its work became a
    PR (worker) or was pushed (fixer); it fails when Fletchery had to escalate or block."""
    counts: dict = field(default_factory=dict)

    @staticmethod
    def from_ledger(path: Path, last: int = 3000) -> "Stats":
        s = Stats()
        if not path.exists():
            return s
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-last:]:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            for a in e.get("attempts") or []:
                s.add(a.get("agent", "?"), a.get("difficulty", MEDIUM), bool(a.get("ok")))
        return s

    def add(self, agent: str, level: str, ok: bool) -> None:
        c = self.counts.setdefault((agent, level), [0, 0])
        c[0] += 1 if ok else 0
        c[1] += 1

    def rate(self, agent: str, level: str) -> tuple[float, int]:
        ok, n = self.counts.get((agent, level), (0, 0))
        return (ok / n if n else 0.0), n

    def table(self) -> list[dict]:
        rows: dict[str, dict] = {}
        for (agent, level), (ok, n) in sorted(self.counts.items()):
            r = rows.setdefault(agent, {"agent": agent, "ok": 0, "tried": 0})
            r[level] = {"ok": ok, "tried": n}
            r["ok"] += ok
            r["tried"] += n
        return list(rows.values())


# ---- the pool ----

@dataclass
class Choice:
    agent: str
    difficulty: str
    why: str


class AgentPool:
    """The project's agents, their runtimes (made on first use), who is paused, who is busy, and the router."""

    def __init__(self, specs: list[AgentSpec], routing: dict | None, make_runtime: Callable[[AgentSpec], object]):
        self.specs = {s.name: s for s in specs}
        self.routing = routing or {}
        self._make = make_runtime
        self._runtimes: dict[str, object] = {}
        self.paused: dict[str, dict] = {}   # name -> {"until": iso, "reason": text}
        self.busy: dict[str, int] = {}
        self.accounts: dict[str, dict] = {}  # name -> {"ok": bool, "message": text, "at": iso}: is it signed in

    # ---- basics ----
    @property
    def multi(self) -> bool:
        return len([s for s in self.specs.values() if s.enabled]) > 1

    def spec(self, name: str) -> AgentSpec:
        return self.specs[name]

    def runtime(self, name: str):
        if name not in self._runtimes:
            self._runtimes[name] = self._make(self.specs[name])
        return self._runtimes[name]

    def ordered(self) -> list[AgentSpec]:
        return sorted((s for s in self.specs.values() if s.enabled), key=lambda s: (s.tier, s.name))

    def top(self) -> AgentSpec | None:
        o = self.ordered()
        return o[-1] if o else None

    # ---- availability ----
    def available(self, name: str, role: str = "worker", at: datetime | None = None) -> bool:
        s = self.specs.get(name)
        if not s or not s.can(role):
            return False
        p = self.paused.get(name)
        if p and (at or _now()) < _parse(p.get("until", "")):
            return False
        if self.accounts.get(name, {}).get("ok") is False:  # its last sign-in check failed
            return False
        if role in ("worker", "fixer") and s.max_parallel and self.busy.get(name, 0) >= s.max_parallel:
            return False
        return True

    def pause(self, name: str, minutes: float, reason: str) -> None:
        until = _now() + timedelta(minutes=minutes)
        self.paused[name] = {"until": until.isoformat(timespec="seconds"), "reason": reason[:200]}

    def any_available(self, role: str = "worker", besides: str = "") -> bool:
        return any(self.available(s.name, role) for s in self.ordered() if s.name != besides)

    def clear_expired(self) -> None:
        n = _now()
        for k in [k for k, v in self.paused.items() if n >= _parse(v.get("until", ""))]:
            del self.paused[k]

    def set_enabled(self, name: str, on: bool) -> None:
        self.specs[name].enabled = bool(on)
        self._runtimes.pop(name, None)

    def acquire(self, name: str) -> None:
        self.busy[name] = self.busy.get(name, 0) + 1

    def release(self, name: str) -> None:
        self.busy[name] = max(0, self.busy.get(name, 0) - 1)

    # ---- routing ----
    def preferences(self, level: str) -> list[str]:
        names = [n for n in (self.routing.get(level) or []) if n in self.specs and self.specs[n].enabled]
        if not names:  # no list for this level: weakest first for easy, strongest first otherwise
            o = [s.name for s in self.ordered()]
            names = o if level == EASY else list(reversed(o)) if level == HARD else o[len(o) // 2:] + o[:len(o) // 2]
        rest = [s.name for s in sorted(self.ordered(), key=lambda s: -s.tier) if s.name not in names]
        return names + rest  # everything else after, strongest first: a fallback when the preferred are all busy

    def learned(self, names: list[str], level: str, stats: Stats | None) -> tuple[list[str], str]:
        """Re-order by results: an agent that keeps failing this level goes to the back; a cheaper one that has
        proven itself on it comes first."""
        if not stats or self.routing.get("learn", True) is False:
            return names, ""
        need = int(self.routing.get("minSamples", 5))
        promote = float(self.routing.get("promoteAt", 0.8))
        demote = float(self.routing.get("demoteBelow", 0.4))
        notes = []
        good = []
        for n in names:
            r, k = stats.rate(n, level)
            if k >= need and r < demote:
                notes.append(f"{n} passes {r:.0%} of {level} tasks, so it goes last")
            else:
                good.append(n)
        bad = [n for n in names if n not in good]
        head = good[0] if good else None
        cheaper = [n for n in good if head and self.specs[n].tier < self.specs[head].tier]
        for n in sorted(cheaper, key=lambda n: self.specs[n].tier):
            r, k = stats.rate(n, level)
            if k >= need and r >= promote:
                good.remove(n)
                good.insert(0, n)
                notes.append(f"{n} passes {r:.0%} of {level} tasks, so it goes first")
                break
        return good + bad, "; ".join(notes)

    def choose(self, title: str, body: str, labels: list[str], depends: int = 0, stats: Stats | None = None,
               role: str = "worker") -> Choice | None:
        level, why = classify(title, body, labels, self.routing.get("hardPatterns"), self.routing.get("easyPatterns"),
                              depends)
        forced = override(body, labels)
        if forced and forced in self.specs:
            if self.available(forced, role):
                return Choice(forced, level, f"asked for on the issue ({why})")
            # asked for but paused or busy: wait for it rather than quietly using another
            return None
        order, learned = self.learned(self.preferences(level), level, stats)
        for n in order:
            if self.available(n, role):
                bits = [f"{level}: {why}"] + ([learned] if learned else [])
                if n != order[0]:
                    bits.append(f"{order[0]} is paused or busy")
                return Choice(n, level, "; ".join(bits))
        return None

    def escalate(self, name: str, role: str = "worker") -> str | None:
        """The next stronger available agent, or None when `name` is already the strongest there is."""
        cur = self.specs[name].tier if name in self.specs else 0
        for s in self.ordered():
            if s.tier > cur and self.available(s.name, role):
                return s.name
        return None

    def reviewer_for(self, worker: str) -> str | None:
        """A reviewer from another vendor than the worker, so nobody marks their own homework. Preference: the
        profile's `routing.reviewers`, then strongest first. With one agent, that agent reviews itself."""
        if not self.multi:
            return worker if self.available(worker, "reviewer") else None
        wv = self.specs[worker].vendor if worker in self.specs else ""
        min_tier = int(self.routing.get("reviewMinTier", 2))
        names = [n for n in (self.routing.get("reviewers") or []) if n in self.specs]
        names += [s.name for s in sorted(self.ordered(), key=lambda s: -s.tier) if s.name not in names]
        names = [n for n in names if self.specs[n].tier >= min_tier or n in (self.routing.get("reviewers") or [])]
        for n in names:
            if self.specs[n].vendor != wv and self.available(n, "reviewer"):
                return n
        for n in names:  # no other vendor on and free: another agent, else a fresh session of the same one
            if n != worker and self.available(n, "reviewer"):
                return n
        return worker if worker and self.available(worker, "reviewer") else None

    def view(self, stats: Stats | None = None) -> list[dict]:
        """For the dashboard and the status issue."""
        rows = []
        n = _now()
        for s in sorted(self.specs.values(), key=lambda s: (-s.tier, s.name)):
            p = self.paused.get(s.name)
            paused = p if p and n < _parse(p.get("until", "")) else None
            acct = self.accounts.get(s.name, {})
            row = {"name": s.name, "kind": s.kind, "model": s.model, "tier": s.tier, "vendor": s.vendor,
                   "enabled": s.enabled, "busy": self.busy.get(s.name, 0), "max_parallel": s.max_parallel,
                   "paused_until": paused["until"] if paused else "", "paused_reason": paused["reason"] if paused else "",
                   "signed_in": acct.get("ok"), "account": acct.get("message", ""), "checked": acct.get("at", ""),
                   "key_env": list(s.key_env or []), "available": self.available(s.name)}
            if stats:
                for lv in LEVELS:
                    r, k = stats.rate(s.name, lv)
                    row[lv] = {"rate": round(r, 2), "tried": k}
            rows.append(row)
        return rows


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(iso: str) -> datetime:
    try:
        d = datetime.fromisoformat(iso)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)
