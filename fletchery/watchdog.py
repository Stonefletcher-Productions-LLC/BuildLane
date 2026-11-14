"""Merge rounds that have nothing to take: notice, recover, and only then alert.

LifeShare 2026-11-14: no round ran for hours after round 179 although 27 PRs carried `ready` and the backend, both
machines and every worker were healthy. Each PR was left out at its current head (15 only because GitHub had once
called them conflicting with Test-Staging), parked, or held by a prerequisite. Nothing crashed, so the restart logic had
nothing to restart, and the dashboard's "waiting for prerequisite PRs" looked like a normal wait. Alex found it by hand.

A pass in which `ready` PRs exist but none can join a round is a starve. While it lasts, each pass:

  1. rechecks, at most every RECHECK_CONFLICT_MINUTES per PR, the PRs left out only for a conflict with the base. The
     base moves on with every round, so a conflict can go away without a new head; when GitHub now merges the PR
     cleanly its exclusion is dropped and the next round takes it.
  2. after RETRY_FAILED_AFTER_MINUTES, gives each PR left out for a failure one more round at that same head (the
     failure may have been the base's or the machine's). Once per head, ever: a second failure keeps it out.
  3. after ALERT_AFTER_MINUTES with nothing recovered, comments once on the "Fletchery round alerts" issue with each
     PR and what holds it, so GitHub notifies people. When a round can start again it says so there too.

Paused merging, a code reload, a running round or the failed-round cooldown are not starves."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

ALERT_AFTER_MINUTES = 60
RETRY_FAILED_AFTER_MINUTES = 180
RECHECK_CONFLICT_MINUTES = 30
MARKER = "<!-- fletchery-rounds-starved -->"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(at: datetime) -> str:
    return at.isoformat(timespec="seconds")


def _age_minutes(stamp: str, now: datetime) -> float:
    try:
        return (now - datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))).total_seconds() / 60
    except (TypeError, ValueError):
        return 0.0


def reasons(labelled: list, st, cross_repo_pending: dict | None = None) -> dict[str, str]:
    """PR number -> what keeps it out of rounds now, in a person's words."""
    out: dict[str, str] = {}
    pending = cross_repo_pending or {}
    for p in labelled:
        key = str(p.number)
        ex = st.merge_excluded.get(key) or {}
        park = st.merge_parked.get(key) or {}
        if ex.get("head") == p.head_sha:
            kind = "conflicts with the base" if ex.get("conflict") else f"failed in round {ex.get('round') or '?'}"
            out[key] = f"left out at its head ({kind}): {str(ex.get('why', ''))[:160]}"
        elif park.get("head") == p.head_sha and park.get("parked"):
            out[key] = f"parked at its head: {str(park.get('why', ''))[:160]}"
        elif pending.get(p.number):
            out[key] = "waits on " + ", ".join(sorted(pending[p.number]))
        else:
            out[key] = "waits on a prerequisite PR or issue that is not eligible yet"
    return out


def starved(engine, labelled: list, cross_repo_pending: dict | None = None, now: datetime | None = None) -> bool:
    """One pass of a starve. Returns True when it freed a PR (the caller runs another pass straight away)."""
    st, now = engine.state, now or _now()
    rec = st.merge_starved
    if not rec.get("since"):
        rec["since"] = _iso(now)
        engine.ctx.log(f"merge rounds: {len(labelled)} ready PR(s) but none can join a round; rechecking them")
    rec["blocked"] = reasons(labelled, st, cross_repo_pending)
    freed = _recheck_conflicts(engine, labelled, now) + _retry_failures(engine, labelled, now)
    if freed:
        rec["freed"] = sorted(set(rec.get("freed", [])) | set(freed))
        engine.ctx.log("merge rounds: retrying " + ", ".join(f"#{n}" for n in freed) + " (starve recovery)")
        return True
    if not rec.get("alerted") and _age_minutes(rec["since"], now) >= ALERT_AFTER_MINUTES:
        _alert(engine, rec, now)
    return False


def resumed(engine) -> None:
    """A round can take a PR again: end the starve, and say so where the alert went."""
    st = engine.state
    rec = st.merge_starved
    if not rec.get("since"):
        return
    if rec.get("alerted"):
        try:
            import rounds
            rounds.alert_comment(engine.ctx.gh, engine.state_dir,
                                 f"Merge rounds can take PRs again (starved since {rec['since']}).\n\n{MARKER}")
        except Exception as e:
            engine.ctx.log(f"merge rounds: could not post the recovery note: {type(e).__name__}: {e}")
    engine.ctx.log(f"merge rounds: starve since {rec['since']} is over")
    keep = rec.get("retried") or {}
    st.merge_starved = {"retried": keep} if keep else {}


def _recheck_conflicts(engine, labelled: list, now: datetime) -> list[int]:
    lookup = getattr(engine.ctx.gh, "pr_conflicts", None)
    if lookup is None or engine.dry_run:
        return []
    freed = []
    for p in labelled:
        ex = engine.state.merge_excluded.get(str(p.number)) or {}
        if not ex.get("conflict") or ex.get("head") != p.head_sha:
            continue
        if ex.get("rechecked") and _age_minutes(ex["rechecked"], now) < RECHECK_CONFLICT_MINUTES:
            continue
        ex["rechecked"] = _iso(now)
        try:
            conflicts = lookup(p.number)
        except Exception as e:
            engine.ctx.log(f"PR #{p.number}: starve conflict recheck deferred: {type(e).__name__}: {e}")
            continue
        if conflicts is False:  # None: GitHub is still working it out; ask again later
            engine.state.merge_excluded.pop(str(p.number), None)
            freed.append(p.number)
    return freed


def _retry_failures(engine, labelled: list, now: datetime) -> list[int]:
    rec = engine.state.merge_starved
    if _age_minutes(rec.get("since", ""), now) < RETRY_FAILED_AFTER_MINUTES:
        return []
    retried = rec.setdefault("retried", {})
    heads = {str(p.number): p.head_sha for p in labelled}
    for key in [k for k in retried if heads.get(k) != retried[k]]:
        del retried[key]  # a new head or no longer ready: the once-per-head record is spent
    freed = []
    for p in labelled:
        key = str(p.number)
        ex = engine.state.merge_excluded.get(key) or {}
        if ex.get("conflict") or ex.get("head") != p.head_sha or retried.get(key) == p.head_sha:
            continue
        retried[key] = p.head_sha
        engine.state.merge_excluded.pop(key, None)
        freed.append(p.number)
    return freed


def _alert(engine, rec: dict, now: datetime) -> None:
    import rounds
    lines = [f"- #{n}: {why}" for n, why in sorted(rec.get("blocked", {}).items(), key=lambda kv: int(kv[0]))]
    text = (f"Merge rounds have had nothing they can take since {rec['since']} "
            f"({int(_age_minutes(rec['since'], now))} min). Fletchery rechecked conflicts and retried failed heads "
            "where it could; these need a new head, a repair, or a person:\n\n" + "\n".join(lines[:60])
            + (f"\n- and {len(lines) - 60} more" if len(lines) > 60 else "") + "\n\n" + MARKER)
    number = rounds.alert_comment(engine.ctx.gh, engine.state_dir, text)
    try:
        with open(engine.state_dir / "alerts.jsonl", "a", encoding="utf-8") as f:
            import json
            f.write(json.dumps({"at": _iso(now), "result": "starved", "alert": text[:2000]}) + "\n")
    except OSError:
        pass
    if number:
        rec["alerted"] = _iso(now)
        rec["issue"] = number
        engine.ctx.log(f"merge rounds: starved for {ALERT_AFTER_MINUTES}+ min; alert on #{number}")


# ---- the backend itself: a hung pass loop (Alex 2026-11-14: "It should be able to recover") ----
# Crashes already restart (Start-Fletchery.ps1, the desktop app). A backend whose event loop stops turning (a blocking
# call that never returns, a deadlock) stays up, holds its lock and does nothing: no round, no worker, no dashboard
# change. The guard is a thread outside that loop. When the loop has not beaten for `hang_minutes` it writes every
# thread's stack to <state>/hang.txt, frees the lock and exits with HANG_EXIT, which the supervisor restarts.

import faulthandler
import os
import threading
import time

HANG_EXIT = 76


class HangGuard:
    def __init__(self, minutes: float, on_hang, clock=time.monotonic):
        self.limit = max(0.0, float(minutes or 0)) * 60
        self.on_hang = on_hang
        self.clock = clock
        self.last = clock()
        self._stop = threading.Event()

    def beat(self) -> None:
        self.last = self.clock()

    def hung(self) -> bool:
        return bool(self.limit) and self.clock() - self.last > self.limit

    def start(self, every: float = 30) -> None:
        if not self.limit:
            return

        def watch():
            while not self._stop.wait(every):
                if self.hung():
                    self.on_hang(int((self.clock() - self.last) / 60))
                    return
        threading.Thread(target=watch, name="fletchery-hang-guard", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()


def exit_hung(state_dir, log, minutes: int) -> None:
    """Record why, free the lock (a stale lock is taken over anyway, but say so plainly) and exit for a restart."""
    try:
        with open(os.path.join(state_dir, "hang.txt"), "w", encoding="utf-8") as f:
            f.write(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}: the pass loop made no progress for "
                    f"{minutes} min; exiting with {HANG_EXIT} so the supervisor restarts Fletchery.\n\n")
            f.flush()
            faulthandler.dump_traceback(file=f, all_threads=True)
    except OSError:
        pass
    try:
        log(f"backend hung: no pass progress for {minutes} min; restarting (stacks in hang.txt)")
    except Exception:
        pass
    try:
        lock = os.path.join(state_dir, "fletchery.lock")
        with open(lock, encoding="utf-8") as f:
            if f.read().split()[0] == str(os.getpid()):
                os.remove(lock)
    except (OSError, IndexError):
        pass
    os._exit(HANG_EXIT)
