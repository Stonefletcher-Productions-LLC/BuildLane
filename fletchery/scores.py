"""Scores: how good each agent is at each kind of task, and how cheap it is, on a 0-100 scale.

Where a score comes from, strongest first:
  1. you: the profile's `scores` on the agent (set on its card), e.g. {"cpp": 70}
  2. Fletchery's own results: every task an agent passes or fails moves its score for that kind of task toward
     what it achieved (a pass at a level is worth that level's bar + 15, a fail the bar - 25)
  3. public benchmarks for the model (model_scores.json: Terminal-Bench 4.0, SWE-bench Pro, SWE-bench Verified),
     the same seed for every kind of task, since benchmarks don't split C++ from Python
  4. the agent's Strength (1-5), when no benchmark names its model

The router (agents.AgentPool, routing mode "scores") works out a task's kind and difficulty, keeps the agents whose
score for that kind clears the difficulty's bar, and takes the cheapest of those, spreading work as agents get busy.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

SKILLS = ["cpp", "scripts", "ui", "docs", "review"]
SKILL_NAMES = {"cpp": "C++ / Unreal", "scripts": "Python & tools", "ui": "UI", "docs": "Docs & data", "review": "Review"}
BARS = {"easy": 45, "medium": 60, "hard": 75}
PRIOR_WEIGHT = 6   # how many tasks the seed counts as
PASS_BONUS, FAIL_MALUS = 15, 25
BUSY_PENALTY = 10  # cost points per session an agent is already running, so work spreads out

# signs of what kind of task it is; the kind with most hits wins, ties go to the profile's defaultSkill
SKILL_PATTERNS = {
    "cpp": [r"\.(cpp|h|hpp)\b", r"\bC\+\+", r"\bSource/", r"\bU(CLASS|FUNCTION|PROPERTY)\b", r"\bactor\b", r"\bcomponent\b",
            r"\bsubsystem\b", r"\breplicat", r"\bnav(mesh)?\b"],
    "scripts": [r"\.py\b", r"\bpython\b", r"\bTools/", r"\bscript", r"\bBlender\b", r"\.ps1\b", r"\bPowerShell\b",
                r"\bcommandlet\b"],
    "ui": [r"\bUI\b", r"\bUMG\b", r"\bwidget", r"\bHUD\b", r"\bmenu\b", r"\bbutton", r"\btooltip", r"\bscreen\b",
           r"\bpanel\b", r"\bfont\b", r"\bcard\b"],
    "docs": [r"\bdocs?/", r"\.md\b", r"\bREADME\b", r"\bstring table\b", r"\bLSStrings\b", r"\.csv\b", r"\blocali[sz]",
             r"\bdocumentation\b", r"\bwording\b", r"\btypo\b"],
}
_SKILL_LINE = re.compile(r"^\s*skill\s*:\s*(cpp|scripts|ui|docs)\b", re.IGNORECASE | re.MULTILINE)

_HERE = Path(__file__).resolve().parent
CATALOG_FILE = _HERE / "model_scores.json"


def classify_skill(title: str, body: str, labels: list[str], default: str = "cpp") -> tuple[str, str]:
    """(kind, why) for a task: a `skill:<kind>` label or `Skill:` line wins, else the most hits."""
    for l in labels:
        if l.lower().startswith("skill:") and l.split(":", 1)[1].lower() in SKILL_PATTERNS:
            return l.split(":", 1)[1].lower(), f"label {l}"
    m = _SKILL_LINE.search(body or "")
    if m:
        return m.group(1).lower(), "Skill line"
    text = f"{title}\n{body or ''}"
    hits = {k: sum(1 for p in pats if re.search(p, text, re.IGNORECASE)) for k, pats in SKILL_PATTERNS.items()}
    best = max(hits.values())
    if best == 0:
        return default, "no signs; the project's default"
    top = [k for k, v in hits.items() if v == best]
    kind = default if default in top else top[0]
    return kind, f"{best} sign{'s' if best > 1 else ''} of {SKILL_NAMES[kind]}"


# ---- the benchmark catalog ----

def load_catalog(override: Path | None = None) -> dict:
    """The shipped catalog, or a refreshed copy (state dir) when it is newer."""
    cat = _read(CATALOG_FILE)
    if override and override.exists():
        newer = _read(override)
        if newer.get("models") and str(newer.get("updated", "")) >= str(cat.get("updated", "")):
            return newer
    return cat


def _read(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def model_key(kind: str, model: str, catalog: dict) -> str:
    """'opencode-go/claude-haiku-5-5' -> 'claude-haiku-5-5'; Claude's or Codex's default model by alias."""
    tail = (model or "").split("/")[-1].lower()
    tail = re.sub(r"\[.*\]$", "", tail)  # claude-opus-5-5[1m]
    if not tail:
        return (catalog.get("aliases") or {}).get(kind, "")
    return tail


def seed(kind: str, model: str, tier: int, catalog: dict) -> dict:
    """{"coding": n, "cost": n|None, "source": text, "date": iso, "from": "benchmarks"|"strength", "detail": [...]}."""
    key = model_key(kind, model, catalog)
    entry = (catalog.get("models") or {}).get(key)
    benches = catalog.get("benchmarks") or {}
    sources = catalog.get("sources") or {}
    out = {"model_key": key, "coding": None, "cost": None, "from": "strength", "detail": [], "date": ""}
    if entry:
        total = weight = 0.0
        dates = []
        for b, spec in benches.items():
            if b in entry:
                val, src = entry[b][0], entry[b][1]
                w = float(spec.get("weight", 1))
                total += w * (float(val) + float(spec.get("offset", 0)))
                weight += w
                s = sources.get(src, {})
                out["detail"].append({"bench": spec.get("name", b), "value": val, "source": s.get("title", src),
                                      "url": s.get("url", ""), "date": s.get("date", "")})
                dates.append(s.get("date", ""))
        if weight:
            out["coding"] = _clip(total / weight)
            out["from"] = "benchmarks"
            out["date"] = min(d for d in dates if d) if any(dates) else ""
        if "price" in entry:
            out_price = float(entry["price"][1])
            out["cost"] = _clip(100 - 30 * math.log10(max(out_price, 0.01) / 0.5))
            out["price"] = entry["price"][:2]
        if "tokens_per_s" in entry:
            out["tokens_per_s"] = entry["tokens_per_s"][0]
    if out["coding"] is None:
        out["coding"] = _clip(20 + 15 * int(tier or 1))
    if out["cost"] is None:  # no price known: assume stronger tiers cost more
        out["cost"] = _clip(100 - 15 * (int(tier or 1) - 1))
    return out


def _clip(v: float) -> int:
    return int(round(max(0.0, min(100.0, v))))


# ---- Fletchery's own results ----

def results(attempts: list[dict]) -> dict:
    """(agent, skill) -> [evidence sum, count, passed]. An attempt without a skill (older ledgers) counts for every
    kind of coding."""
    out: dict = {}
    for a in attempts:
        bar = BARS.get(a.get("difficulty") or "medium", BARS["medium"])
        ev = bar + PASS_BONUS if a.get("ok") else bar - FAIL_MALUS
        role = a.get("role", "worker")
        skills = ["review"] if role == "reviewer" else ([a["skill"]] if a.get("skill") else ["cpp", "scripts", "ui", "docs"])
        for s in skills:
            c = out.setdefault((a.get("agent", "?"), s), [0.0, 0, 0])
            c[0] += ev
            c[1] += 1
            c[2] += 1 if a.get("ok") else 0
    return out


def effective(spec, catalog: dict, res: dict | None = None) -> dict:
    """Everything the card shows and the router uses for one agent."""
    sd = seed(spec.kind, spec.model, spec.tier, catalog)
    mine = getattr(spec, "scores", None) or {}
    skills = {}
    for sk in SKILLS:
        base = sd["coding"]
        row = {"seed": base, "score": base, "from": sd["from"], "tasks": 0, "passed": 0}
        ev = (res or {}).get((spec.name, sk))
        if ev and ev[1]:
            row["score"] = _clip((base * PRIOR_WEIGHT + ev[0]) / (PRIOR_WEIGHT + ev[1]))
            row["tasks"], row["passed"] = ev[1], ev[2]
            row["from"] = "results"
        if mine.get(sk) is not None:
            row["score"] = _clip(float(mine[sk]))
            row["from"] = "you"
        skills[sk] = row
    cost = sd["cost"] if mine.get("cost") is None else _clip(float(mine["cost"]))
    overall = _clip(sum(skills[s]["score"] for s in ("cpp", "scripts", "ui", "docs")) / 4)
    return {"skills": skills, "cost": cost, "cost_from": "you" if mine.get("cost") is not None else
            ("price" if "price" in sd else "strength"), "overall": overall, "seed": sd}
