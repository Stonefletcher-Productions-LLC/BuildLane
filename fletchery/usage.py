"""Token and cost use, one line per agent session in `<state dir>/usage.jsonl`.

Plans (Claude Max, ChatGPT for Codex, OpenCode Go) are limited by tokens per 5 hours or per week, not by dollars, and
Codex reports no cost at all, so the ledger's dollar estimate hid where the limits went. Every session that goes
through the pipeline's guard (workers, reviewers, fixers, planners, architects, merge rounds) writes:

    {"at": iso, "task": n, "role": "worker", "agent": "codex-sol", "account": "codex", "ok": true,
     "seconds": 812.4, "cost": 0.0, "tokens": {"input": 1200000, "output": 18000, "cache_read": 950000}}

`summary()` adds them up for the dashboard, the status issue and `python -m fletchery usage`.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

TOKEN_KEYS = ("input", "output", "cache_read", "cache_write")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def record(path: Path, r, task: int = 0, pool=None) -> None:
    """Append one session. Never raises: accounting must not break a session."""
    try:
        account = ""
        if pool is not None and r.agent in getattr(pool, "specs", {}):
            account = pool.account_of(pool.specs[r.agent])
        row = {"at": _now().isoformat(timespec="seconds"), "task": task, "role": r.role, "agent": r.agent or "claude",
               "account": account, "ok": bool(r.ok), "seconds": round(float(getattr(r, "seconds", 0) or 0), 1),
               "cost": round(float(r.cost_usd or 0), 4)}
        tokens = {k: int(v) for k, v in (getattr(r, "tokens", None) or {}).items() if v}
        if tokens:
            row["tokens"] = tokens
        if r.limited:
            row["limited"] = True
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    except Exception:
        pass


def read(path: Path | None, days: float = 7, last: int = 20000) -> list[dict]:
    if not path or not Path(path).exists():
        return []
    since = (_now() - timedelta(days=days)).isoformat(timespec="seconds")
    out = []
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines()[-last:]:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if str(e.get("at", "")) >= since:
            out.append(e)
    return out


def total_tokens(t: dict | None) -> int:
    """Tokens that count against a plan: fresh input, output and cache writes in full, cache reads at a tenth (what
    providers charge for them, and roughly how the plans weigh them)."""
    t = t or {}
    return int(t.get("input", 0) + t.get("output", 0) + t.get("cache_write", 0) + 0.1 * t.get("cache_read", 0))


def runs_today(path: Path | None) -> dict:
    """agent -> sessions started today (UTC), for each agent's `maxRunsPerDay`."""
    today = _now().strftime("%Y-%m-%d")
    out: dict = {}
    for e in read(path, days=1.1):
        if str(e.get("at", ""))[:10] == today:
            out[e.get("agent", "?")] = out.get(e.get("agent", "?"), 0) + 1
    return out


def summary(path: Path | None, days: float = 7) -> dict:
    """{"days", "agents": [{agent, account, sessions, ok, tokens, weighted, cost, hours, by_role}], "accounts": [...],
    "roles": {...}}, heaviest first."""
    agents: dict = {}
    accounts: dict = {}
    roles: dict = {}
    for e in read(path, days):
        a = agents.setdefault(e.get("agent", "?"), {"agent": e.get("agent", "?"), "account": e.get("account", ""),
                                                   "sessions": 0, "ok": 0, "tokens": {}, "weighted": 0, "cost": 0.0,
                                                   "hours": 0.0, "by_role": {}})
        w = total_tokens(e.get("tokens"))
        for bucket in (a, accounts.setdefault(e.get("account") or e.get("agent", "?"),
                                               {"account": e.get("account") or e.get("agent", "?"), "sessions": 0,
                                                "ok": 0, "tokens": {}, "weighted": 0, "cost": 0.0, "hours": 0.0})):
            bucket["sessions"] += 1
            bucket["ok"] += 1 if e.get("ok") else 0
            bucket["weighted"] += w
            bucket["cost"] = round(bucket["cost"] + float(e.get("cost") or 0), 4)
            bucket["hours"] = round(bucket["hours"] + float(e.get("seconds") or 0) / 3600, 2)
            for k, v in (e.get("tokens") or {}).items():
                bucket["tokens"][k] = bucket["tokens"].get(k, 0) + int(v)
        r = a["by_role"].setdefault(e.get("role", "?"), {"sessions": 0, "weighted": 0})
        r["sessions"] += 1
        r["weighted"] += w
        rr = roles.setdefault(e.get("role", "?"), {"sessions": 0, "weighted": 0, "cost": 0.0})
        rr["sessions"] += 1
        rr["weighted"] += w
        rr["cost"] = round(rr["cost"] + float(e.get("cost") or 0), 4)
    key = lambda x: (-x["weighted"], -x["cost"])
    return {"days": days, "agents": sorted(agents.values(), key=key), "accounts": sorted(accounts.values(), key=key),
            "roles": roles}


def text(s: dict) -> str:
    """A plain-text table for the CLI and the status issue."""
    def n(v):
        return f"{v / 1e6:.1f}M" if v >= 1e6 else f"{v / 1e3:.0f}k" if v >= 1e3 else str(v)
    lines = [f"Token use, last {s['days']:g} days (weighted: input + output + cache writes + a tenth of cache reads)"]
    for a in s["agents"]:
        roles = ", ".join(f"{k} {v['sessions']}" for k, v in sorted(a["by_role"].items(), key=lambda kv: -kv[1]["weighted"]))
        lines.append(f"- {a['agent']} ({a['account'] or '?'}): {n(a['weighted'])} tokens, {a['sessions']} sessions "
                     f"({a['ok']} ok), {a['hours']:.1f} h, ${a['cost']:.2f} est. [{roles}]")
    if not s["agents"]:
        lines.append("- nothing recorded yet")
    return "\n".join(lines)
