"""`python -m fletchery doctor`: what Fletchery is doing, why nothing new may be starting, what failed recently and what
to do about it, in plain words. Read-only: it reads the running dashboard's /api/status and the ledger, so an agent
(or a person in a terminal) can diagnose Fletchery without clicking through the dashboard or writing anything."""
from __future__ import annotations

import json
import re
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Failures worth a hint: (pattern in the ledger detail, what it means / what to do)
HINTS = [
    (r"usage limit|hit your usage", "An agent's plan hit its usage limit; Fletchery pauses it until the reset."),
    (r"Validation Failed \(HTTP 422\)", "GitHub refused a PR write (fixed in Fletchery #108: update if this is recent)."),
    (r"WinError 2", "A program Fletchery starts was not found (fixed for pwsh in #109)."),
    (r"maximum number of turns", "A writer ran out of turns; Settings > This project > Workers > Writer turns."),
    (r"no progress for", "A session went silent and was stopped by the stall watchdog; its task was queued again."),
    (r"No space left", "The disk is full; Building > Storage, or free space on the work drive."),
    (r"no agent free|waiting for a worker rank", "Every agent that may take it is busy, paused or off (Agents page)."),
    (r"There's an issue with the selected model", "A model id is wrong or not on the plan; switch that agent off on the Agents page."),
]


def fetch_status(port: int, timeout: float = 10) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def read_ledger(path: Path, hours: float = 24) -> list[dict]:
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    rows = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return rows
    for line in lines:
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if str(e.get("at", "")) >= since:
            rows.append(e)
    return rows


def why_idle(s: dict) -> list[str]:
    """Every reason new feature work is not starting, with where to change it."""
    out = []
    if s.get("signed_out"):
        out.append(f"Claude is signed out ({s['signed_out']}): run `claude`, then /login.")
    if s.get("held"):
        out.append("Paused from the dashboard: press Resume (top right) to start new work.")
    if s.get("limit_until"):
        out.append(f"Feature workers are paused by a usage limit until {s['limit_until']}.")
    if not s.get("workers_enabled", True):
        out.append("Workers are off: Settings > This project > Workers > Workers on.")
    if s.get("max_parallel") == 0:
        out.append("All workers is set to 0 on the Overview page.")
    backlog = (s.get("merge_auto") or {}).get("backlog") or {}
    if backlog.get("paused"):
        out.append(f"{backlog.get('count')} ready PRs are waiting to merge; new tasks start again at {backlog.get('low')} "
                   "(Settings > This project > Merging).")
    if s.get("update_pending"):
        out.append("An update is installed; Fletchery reloads once running work (and any merge round) finishes.")
    if s.get("sync_error"):
        out.append(f"GitHub sync is failing: {s['sync_error']}")
    return out


def report(s: dict, ledger: list[dict]) -> str:
    lines = [f"Fletchery: {s.get('project', '?')} ({s.get('repo', '?')})"]
    live = s.get("live") or []
    lines.append(f"Running: {len(live)} session(s) of {s.get('max_parallel')} worker slots"
                 + (": " + "; ".join(f"#{a.get('task')} {a.get('stage', '')}" + (f" ({a['agent']})" if a.get("agent") else "") for a in live) if live else ""))
    ma = s.get("merge_auto") or {}
    run = ma.get("running") or {}
    lines.append(f"Merging: round {run['round']} running on {run.get('agent')} with PRs {run.get('prs')}" if run.get("round")
                 else f"Merging: {ma.get('why') or 'idle'}")
    reasons = why_idle(s)
    lines.append("Why new tasks may not start:" if reasons else "Nothing is holding new tasks back.")
    lines += [f"  - {r}" for r in reasons]
    agents = ((s.get("agents") or {}).get("list")) or []
    on = [a.get("name") for a in agents if a.get("enabled")]
    paused = [f"{a.get('name')} until {a.get('paused_until')}" for a in agents if a.get("enabled") and a.get("paused_until")]
    if agents:
        lines.append(f"Agents on: {', '.join(on) or 'none'}" + (f"; paused: {', '.join(paused)}" if paused else ""))
    results = Counter(e.get("result") for e in ledger)
    spent = sum(e.get("cost_usd") or 0 for e in ledger)
    lines.append(f"Last 24 h: {sum(results.values())} results, ${spent:.2f}: "
                 + ", ".join(f"{k} {n}" for k, n in results.most_common()) if ledger else "Last 24 h: no results recorded.")
    bad = [e for e in ledger if e.get("result") in ("requeue", "blocked", "gave_up")]
    seen, hints = Counter(), {}
    for e in bad:
        detail = str(e.get("detail") or "").strip() or f"{e.get('result')} with no detail"
        key = re.sub(r"\d+", "N", detail.splitlines()[0])[:100]
        seen[key] += 1
        hints.setdefault(key, next((h for pat, h in HINTS if re.search(pat, detail, re.I)), ""))
    if seen:
        lines.append("Most common failures (24 h):")
        for key, n in seen.most_common(5):
            lines.append(f"  - {n}x {key}" + (f"\n      -> {hints[key]}" if hints[key] else ""))
    return "\n".join(lines)


def main(cfg) -> int:
    try:
        s = fetch_status(int(getattr(cfg, "dashboard_port", 8770) or 8770))
    except Exception as e:
        print(f"Fletchery's dashboard is not answering on port {getattr(cfg, 'dashboard_port', 8770)}: {e}\n"
              "Is Fletchery running? Start it from the Start menu, or `python -m fletchery run`.")
        return 1
    state_dir = Path(getattr(cfg, "state_dir", "") or ".")
    print(report(s, read_ledger(state_dir / "ledger.jsonl")))
    return 0
