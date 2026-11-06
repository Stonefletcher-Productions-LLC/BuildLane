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
ROLES = ("worker", "fixer", "reviewer", "planner", "merger")


def restrictions(data: dict) -> dict:
    """Validate and copy optional role/difficulty limits; None means unrestricted, [] means none."""
    out = {}
    for key, allowed in (("levels", LEVELS), ("roles", ROLES)):
        if key in data:
            value = data[key]
            if value is not None and (not isinstance(value, list) or any(x not in allowed for x in value)):
                raise ValueError("Invalid " + key)
            out[key] = list(value) if value is not None else None
    return out


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
    levels: list = None             # optional task difficulties this agent may write (easy/medium/hard)
    timeout_minutes: dict = None    # role -> minutes, for CLIs without a turn limit
    key_env: list = None            # stored keys (keys.py) this agent's sessions get, e.g. ["DEEPSEEK_API_KEY"]
    auth: str = "plan"              # plan: the CLI's own sign-in (Claude plan, ChatGPT, OpenCode Go); key: an API key
    scores: dict = None             # your own scores (0-100) by kind of task, e.g. {"cpp": 70}; the rest are automatic

    @property
    def api_key_name(self) -> str:
        """The stored key this agent runs on when `auth` is key."""
        return api_key_name(self)

    @property
    def label(self) -> str:
        return f"{self.name} ({self.model})" if self.model else self.name

    def can(self, role: str) -> bool:
        if role == "merger" and self.never_merges:
            return False
        return self.enabled and (self.roles is None or role in self.roles)

    @property
    def never_merges(self) -> bool:
        """Fable never runs merge rounds (Alex, 2026-11-06), whatever the dropdown or its roles say."""
        return any("fable" in (x or "").lower() for x in (self.model, self.name))


_SPEC_KEYS = {"name": "name", "kind": "kind", "model": "model", "tier": "tier", "vendor": "vendor", "enabled": "enabled",
              "maxParallel": "max_parallel", "command": "command", "roles": "roles", "levels": "levels", "timeoutMinutes": "timeout_minutes",
              "keyEnv": "key_env", "auth": "auth", "scores": "scores"}
DEFAULT_VENDOR = {"claude": "anthropic", "codex": "openai"}


# model words -> the company behind it (reviews come from a different company than the writer)
VENDOR_WORDS = [("claude|haiku|sonnet|opus|fable", "anthropic"), ("gpt|codex|^o\\d", "openai"), ("deepseek", "deepseek"),
                ("kimi|moonshot", "moonshot"), ("glm|zhipu", "zhipu"), ("qwen", "alibaba"), ("gemini", "google"),
                ("grok", "xai"), ("minimax", "minimax"), ("mimo", "xiaomi"), ("mistral", "mistral")]


def guess_vendor(kind: str, model: str) -> str:
    if kind == "claude":
        return "anthropic"
    if kind == "codex":
        return "openai"
    name = (model or "").split("/")[-1].lower()
    for words, vendor in VENDOR_WORDS:
        if re.search(words, name):
            return vendor
    return ""


def tidy_name(name: str, model: str = "") -> str:
    """'Haiku' -> 'haiku'; empty -> from the model ('opencode-go/claude-haiku-5-5' -> 'claude-haiku-5-5')."""
    n = (name or "").strip() or (model or "").split("/")[-1]
    n = re.sub(r"[^a-z0-9._\-]+", "-", n.lower()).strip("-.")
    return n[:32]


# OpenCode provider -> the key its API takes
PROVIDER_KEYS = {"deepseek": "DEEPSEEK_API_KEY", "openrouter": "OPENROUTER_API_KEY", "moonshotai": "MOONSHOT_API_KEY",
                 "openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}


def api_key_name(spec: "AgentSpec") -> str:
    if spec.key_env:
        return spec.key_env[0]
    if spec.kind == "claude":
        return "ANTHROPIC_API_KEY"
    if spec.kind == "codex":
        return "OPENAI_API_KEY"
    return PROVIDER_KEYS.get((spec.model or "").split("/")[0], "")


def codex_model(model: str) -> str:
    """Codex takes a bare model name: 'openai/gpt-6.1-terra' (OpenCode's spelling) -> 'gpt-6.1-terra'. Codex refuses
    the provider/model form outright (LifeShare, 2026-11-03: every review failed and blocked its task)."""
    m = (model or "").strip()
    return m.split("/", 1)[1] if m.lower().startswith("openai/") else m


def claude_model(model: str) -> str:
    """Claude Code takes a bare model id: 'opencode-go/claude-haiku-5-5' (an OpenCode spelling) -> 'claude-haiku-5-5'.
    With the prefix every session failed at once with "There's an issue with the selected model" and escalated
    (LifeShare, 2026-11-06: the claude-haiku-5-5 agent)."""
    m = (model or "").strip()
    return m.rsplit("/", 1)[1] if "/" in m else m


def specs_from(raw: list | None, default_model: str = "") -> list[AgentSpec]:
    """The profile's `agents` list; none means one Claude agent (Fletchery before agents existed)."""
    if not raw:
        return [AgentSpec("claude", "claude", default_model, 4, "anthropic")]
    out = []
    for item in raw:
        limits = restrictions(item)
        s = AgentSpec(name=str(item.get("name") or item.get("kind") or "agent"))
        for k, v in item.items():
            if k in _SPEC_KEYS:
                setattr(s, _SPEC_KEYS[k], v)
        for key, value in limits.items():
            setattr(s, key, value)
        if any(previous.name == s.name for previous in out):
            raise ValueError(f"Duplicate agent name: {s.name}")
        if not s.vendor:
            s.vendor = DEFAULT_VENDOR.get(s.kind) or (s.model.split("/")[-1].split("-")[0] if s.model else s.kind)
        if s.kind == "claude" and not s.model:
            s.model = default_model
        if s.kind == "codex":
            s.model = codex_model(s.model)
        if s.kind == "claude":
            s.model = claude_model(s.model)
        out.append(s)
    return out


# ---- difficulty ----

# Signs that a task is hard, with weights: work a cheap model gets subtly wrong and the Build Lane catches late. A task
# is hard at HARD_SCORE points. LifeShare issues name proof steps ("strict NavCheck", "Lane-Imports:", "replicated")
# almost every time, so one such mention alone is worth 1 and does not make a task hard. Threading and multiplayer
# races are worth 2, saves and migrations 3. A profile replaces these with `routing.hardPatterns` ([regex, weight] or
# a bare regex, worth 2) and the threshold with `routing.hardScore`.
HARD_SCORE = 3
HARD_PATTERNS = [
    [r"\bsave ?(version|format)\b|\bmigration\b", 3],
    [r"\bmultiplayer\b|\btwo-player\b|\bclient and server\b|\bRPC\b", 2],
    [r"\brace condition\b|\bdeadlock\b|\bthread-?safe|\bworker thread\b", 2],
    [r"\bmemory leak\b|\bframe time\b|\bhitch\b|\bprofil(e|ing)\b", 1],
    [r"\breplicat", 1],
    [r"\bnav(mesh|check)\b|\bNavCheck\b|\bpathfinding\b", 1],
    [r"\bLane-Imports\s*:", 1],
    [r"\b(Folk|Folks) (sim|AI|brain|needs)\b|\bautonomy\b|\butility AI\b", 1],
    [r"\barchitecture\b|\brefactor\b|\bredesign\b", 1],
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


def classify(title: str, body: str, labels: list[str], hard: list | None = None,
             easy: list[str] | None = None, depends: int = 0, hard_score: int | None = None) -> tuple[str, str]:
    """(difficulty, why). An explicit level wins; otherwise hard signs and size add up to a score: hard at
    `hard_score`, easy when nothing scored and easy signs show on a short issue with no C++, else medium."""
    for l in labels:
        if l.lower().startswith("difficulty:") and l.split(":", 1)[1].lower() in LEVELS:
            return l.split(":", 1)[1].lower(), f"label {l}"
    m = _LEVEL_LINE.search(body or "")
    if m:
        return m.group(1).lower(), "Difficulty line"
    text = f"{title}\n{body or ''}"
    score, hits = 0, []
    for item in (hard if hard is not None else HARD_PATTERNS):
        pat, weight = (item[0], int(item[1])) if isinstance(item, (list, tuple)) else (item, 2)
        hit = re.search(pat, text, re.IGNORECASE)
        if hit:
            score += weight
            hits.append(hit.group(0).strip())
    files = {m.group(0) for m in _CODE_REF.finditer(text)}
    native = [f for f in files if f.endswith((".cpp", ".h", ".hpp"))]
    if len(text) > 4000:
        score += 2
        hits.append(f"{len(text)} chars")
    if len(native) >= 4:
        score += 2
        hits.append(f"{len(native)} C++ files")
    if depends >= 3:
        score += 1
        hits.append(f"{depends} dependencies")
    need = HARD_SCORE if hard_score is None else hard_score
    named = ", ".join(f'"{h}"' if not h[0].isdigit() else h for h in hits[:4])
    if score >= need:
        return HARD, f"score {score}: {named}"
    easy_hits = [h.group(0).strip() for p in (easy if easy is not None else EASY_PATTERNS)
                 for h in [re.search(p, text, re.IGNORECASE)] if h]
    if score == 0 and easy_hits and not native and len(text) < 2500:
        return EASY, "looks small: " + ", ".join(f'"{h}"' for h in easy_hits[:3])
    return MEDIUM, (f"score {score}: {named}" if score else "ordinary change") + (f" ({len(native)} C++ files)" if native and len(native) < 4 else "")


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
    attempts: list = field(default_factory=list)  # the raw attempts, for scores.results

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
                s.attempts.append(a)
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
    skill: str = ""


class AgentPool:
    """The project's agents, their runtimes (made on first use), who is paused, who is busy, and the router."""

    def __init__(self, specs: list[AgentSpec], routing: dict | None, make_runtime: Callable[[AgentSpec], object]):
        self.specs = {s.name: s for s in specs}
        self.routing = routing or {}
        self._make = make_runtime
        self._runtimes: dict[str, object] = {}
        self.paused: dict[str, dict] = {}   # name -> {"until": iso, "reason": text}
        self.worker_limits = None
        self.worker_rank_names = {}
        self.worker_slots = {}  # task -> (agent, rank at admission)
        self.busy: dict[str, int] = {}
        self.accounts: dict[str, dict] = {}  # name -> {"ok": bool, "message": text, "at": iso}: is it signed in
        self.catalog_override: Path | None = None  # a refreshed model_scores.json in the state dir
        self._results: dict = {}  # scores.results of the last Stats seen

    # ---- scores ----
    @property
    def mode(self) -> str:
        """"scores" (pick by each agent's score for the task's kind) or "lists" (the profile's per-level lists).
        A profile that names lists keeps them unless it says `"mode": "scores"`."""
        m = self.routing.get("mode")
        if m in ("scores", "lists"):
            return m
        return "lists" if any(self.routing.get(lv) for lv in LEVELS) else "scores"

    def catalog(self) -> dict:
        import scores
        return scores.load_catalog(self.catalog_override)

    def scored(self, stats: "Stats | None" = None) -> dict:
        """name -> scores.effective(...) for every agent."""
        import scores
        if stats is not None:
            self._results = scores.results(stats.attempts)
        cat = self.catalog()
        return {n: scores.effective(s, cat, self._results) for n, s in self.specs.items()}

    def bar(self, level: str) -> int:
        import scores
        return int((self.routing.get("bars") or {}).get(level, scores.BARS.get(level, 60)))

    def _choose_scored(self, level: str, why: str, skill: str, skill_why: str, stats, role: str) -> "Choice | None":
        import scores
        sc = self.scored(stats)
        bar = self.bar(level)
        free = [n for n in self.specs if self.available(n, role) and (self.specs[n].levels is None or level in self.specs[n].levels)]
        preferred = (self.routing.get("preferredKindsByLevel") or {}).get(level, [])
        preferred_free = [n for n in free if self.specs[n].kind in preferred]
        if preferred_free:
            free = preferred_free
        if not free:
            return None
        able = [n for n in free if sc[n]["skills"][skill]["score"] >= bar]
        kind = scores.SKILL_NAMES[skill]
        if able:
            pick = max(able, key=lambda n: (sc[n]["cost"] - scores.BUSY_PENALTY * self.busy.get(n, 0),
                                            sc[n]["skills"][skill]["score"]))
            note = (f"{level}, {kind} ({why}; {skill_why}): {pick} scores {sc[pick]['skills'][skill]['score']} "
                    f"(bar {bar}) and is the cheapest that clears it")
            others = [n for n in able if n != pick]
            if others:
                note += "; also able: " + ", ".join(f"{n} {sc[n]['skills'][skill]['score']}" for n in others[:3])
        else:
            pick = max(free, key=lambda n: sc[n]["skills"][skill]["score"])
            note = (f"{level}, {kind} ({why}; {skill_why}): nobody free clears the bar of {bar}, so the strongest free: "
                    f"{pick} {sc[pick]['skills'][skill]['score']}")
        if preferred_free:
            note += "; preferred provider policy"
        return Choice(pick, level, note, skill)

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
    def available(self, name: str, role: str = "worker", at: datetime | None = None,
                  *, ignore_capacity: bool = False) -> bool:
        s = self.specs.get(name)
        if not s or not s.can(role):
            return False
        p = self.paused.get(name)
        if p and (at or _now()) < _parse(p.get("until", "")):
            return False
        if self.accounts.get(name, {}).get("ok") is False:  # its last sign-in check failed
            return False
        if not ignore_capacity and role in ("worker", "fixer", "merger", "planner") and s.max_parallel and self.busy.get(name, 0) >= s.max_parallel:
            return False
        if not ignore_capacity and role == "worker" and self.worker_limits is not None:
            rank = self.worker_rank_names.get(name, "low")
            used = sum(1 for _, r in self.worker_slots.values() if r == rank)
            if used >= self.worker_limits.get(rank, 0):
                return False
        return True

    def choose_without_rank_limits(self, *args, **kwargs):
        limits = self.worker_limits
        self.worker_limits = None
        try:
            return self.choose(*args, **kwargs)
        finally:
            self.worker_limits = limits

    def escalate_without_rank_limits(self, *args, **kwargs):
        # Escalation continues an admitted task; it does not consume a new normal job.
        limits = self.worker_limits
        self.worker_limits = None
        try:
            return self.escalate(*args, **kwargs)
        finally:
            self.worker_limits = limits

    def reserve_worker(self, task, agent, allow_overflow=False):
        rank = self.worker_rank_names.get(agent, "low")
        if not allow_overflow and self.worker_limits is not None and sum(1 for _, r in self.worker_slots.values() if r == rank) >= self.worker_limits.get(rank, 0):
            return False
        self.worker_slots[task] = (agent, rank)
        return True

    def release_worker(self, task):
        self.worker_slots.pop(task, None)

    @staticmethod
    def account_of(s: AgentSpec) -> str:
        """Agents on one account share its limits: every Claude agent on the Claude plan, every Codex agent on one
        ChatGPT sign-in, every OpenCode agent on one provider (opencode-go/...); an API key is its own account."""
        if s.auth == "key":
            return "key:" + (s.api_key_name or s.name)
        if s.kind == "opencode":
            return "opencode:" + (s.model or "").split("/")[0]
        return s.kind if s.kind in ("claude", "codex") else "own:" + s.name

    def pause(self, name: str, minutes: float, reason: str, shared: bool = True) -> list[str]:
        """Pause an agent, and (shared) every agent on the same account: a plan's limit is the account's, not the
        model's. Returns who was paused."""
        until = _now() + timedelta(minutes=minutes)
        names = [name]
        if shared and name in self.specs:
            acct = self.account_of(self.specs[name])
            names += [n for n, s in self.specs.items() if n != name and self.account_of(s) == acct]
        for n in names:
            why = reason if n == name else f"same account as {name}: {reason}"
            self.paused[n] = {"until": until.isoformat(timespec="seconds"), "reason": why[:200]}
        return names

    def any_available(self, role: str = "worker", besides: str = "") -> bool:
        return any(self.available(s.name, role) for s in self.ordered() if s.name != besides)

    def unpause(self, names: list[str] | None = None) -> list[str]:
        """Lift limit pauses now (all, or the named agents), e.g. after a plan upgrade. Returns who was paused. If the
        limit still holds, the next session meets it and the agent pauses again."""
        gone = [k for k in list(self.paused) if names is None or k in names]
        for k in gone:
            del self.paused[k]
        return gone

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
                              depends, self.routing.get("hardScore"))
        import scores
        skill, skill_why = scores.classify_skill(title, body, labels, self.routing.get("defaultSkill", "cpp"))
        forced = override(body, labels)
        if forced and forced in self.specs:
            if self.available(forced, role) and (self.specs[forced].levels is None or level in self.specs[forced].levels):
                return Choice(forced, level, f"asked for on the issue ({why})", skill)
            # asked for but paused or busy: wait for it rather than quietly using another
            return None
        if self.mode == "scores":
            return self._choose_scored(level, why, skill, skill_why, stats, role)
        order, learned = self.learned(self.preferences(level), level, stats)
        eligible = [n for n in order if self.available(n, role)
                    and (self.specs[n].levels is None or level in self.specs[n].levels)]
        kinds = (self.routing.get("preferredKindsByLevel") or {}).get(level, [])
        preferred = [n for n in eligible if self.specs[n].kind in kinds]
        for n in preferred or eligible:
            bits = [f"{level}: {why}"] + ([learned] if learned else [])
            if preferred:
                bits.append("preferred provider policy")
            elif n != order[0]:
                bits.append(f"{order[0]} is paused or busy")
            return Choice(n, level, "; ".join(bits), skill)
        return None

    def escalate(self, name: str, role: str = "worker", skill: str = "", level: str = "") -> str | None:
        """Choose a stronger agent without bypassing task-level, role or provider policy.
        With no known difficulty, agents restricted to explicit levels are ineligible."""
        def eligible(n: str) -> bool:
            return (n != name and self.available(n, role)
                    and (self.specs[n].levels is None or level in self.specs[n].levels))

        def preferred(names: list[str]) -> list[str]:
            kinds = (self.routing.get("preferredKindsByLevel") or {}).get(level, [])
            native = [n for n in names if self.specs[n].kind in kinds]
            return native or names

        if self.mode == "scores":
            sc = self.scored()
            val = (lambda n: sc[n]["skills"][skill]["score"]) if skill in ("cpp", "scripts", "ui", "docs") else \
                  (lambda n: sc[n]["overall"])
            cur = val(name) if name in self.specs else -1
            up = preferred([n for n in self.specs if val(n) > cur and eligible(n)])
            return min(up, key=val) if up else None
        cur = self.specs[name].tier if name in self.specs else 0
        up = preferred([s.name for s in self.ordered() if s.tier > cur and eligible(s.name)])
        return up[0] if up else None

    def reviewer_for(self, worker: str, besides: set[str] | frozenset = frozenset()) -> str | None:
        """A reviewer from another vendor than the worker, so nobody marks their own homework. Preference: the
        profile's `routing.reviewers`, then strongest first. With one agent, that agent reviews itself. `besides`:
        reviewers that already failed to run on this task, so the next pick is someone else."""
        def ok(n: str) -> bool:
            return bool(n) and n not in besides and self.available(n, "reviewer")
        if not self.multi:
            return worker if ok(worker) else None
        wv = self.specs[worker].vendor if worker in self.specs else ""
        if self.mode == "scores":  # best reviewer first, at least reviewMin
            sc = self.scored()
            need = int(self.routing.get("reviewMin", 50))
            names = sorted((n for n in self.specs if self.specs[n].enabled), key=lambda n: -sc[n]["skills"]["review"]["score"])
            names = [n for n in names if sc[n]["skills"]["review"]["score"] >= need]
        else:
            min_tier = int(self.routing.get("reviewMinTier", 2))
            names = [n for n in (self.routing.get("reviewers") or []) if n in self.specs]
            names += [s.name for s in sorted(self.ordered(), key=lambda s: -s.tier) if s.name not in names]
            names = [n for n in names if self.specs[n].tier >= min_tier or n in (self.routing.get("reviewers") or [])]
        for n in names:
            if self.specs[n].vendor != wv and ok(n):
                return n
        for n in names:  # no other vendor on and free: another agent, else a fresh session of the same one
            if n != worker and ok(n):
                return n
        return worker if ok(worker) else None

    def merger(self, prefer: str = "") -> str | None:
        """Use the preferred eligible agent, otherwise the best available C++ agent, regardless of provider."""
        if prefer and prefer in self.specs and self.available(prefer, "merger"):
            return prefer
        sc = self.scored()
        ok = [s for s in self.specs.values() if s.kind in ("claude", "codex", "opencode", "fake") and self.available(s.name, "merger")]
        if not ok:
            return None
        return max(ok, key=lambda s: sc[s.name]["skills"]["cpp"]["score"]).name

    def view(self, stats: Stats | None = None) -> list[dict]:
        """For the dashboard and the status issue."""
        rows = []
        n = _now()
        sc = self.scored(stats)
        for s in sorted(self.specs.values(), key=lambda s: (-s.tier, s.name)):
            p = self.paused.get(s.name)
            paused = p if p and n < _parse(p.get("until", "")) else None
            acct = self.accounts.get(s.name, {})
            row = {"name": s.name, "kind": s.kind, "model": s.model, "tier": s.tier, "vendor": s.vendor,
                   "enabled": s.enabled, "busy": self.busy.get(s.name, 0), "max_parallel": s.max_parallel,
                   "paused_until": paused["until"] if paused else "", "paused_reason": paused["reason"] if paused else "",
                   "signed_in": acct.get("ok"), "account": acct.get("message", ""), "checked": acct.get("at", ""),
                   "key_env": list(s.key_env or []), "available": self.available(s.name),
                   "auth": s.auth, "api_key": s.api_key_name, "scores": sc[s.name], "roles": s.roles, "levels": s.levels}
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
