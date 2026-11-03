"""What Fletchery is doing right now, for the dashboard: each running task's stage and latest action, and a log tail.
Written by the loop (asyncio thread), read by the dashboard server (its own thread), so everything goes through a lock."""
from __future__ import annotations

import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Live:
    def __init__(self, log_lines: int = 400):
        self._lock = threading.Lock()
        self._activity: dict[int, dict] = {}
        self._log: deque[tuple[str, str]] = deque(maxlen=log_lines)

    # ---- written by the pipeline ----
    def stage(self, task: int, stage: str, title: str = "", kind: str = "task") -> None:
        with self._lock:
            a = self._activity.setdefault(task, {"task": task, "title": title, "kind": kind, "since": now_iso(),
                                                 "actions": 0, "last": "", "last_at": "", "stage": ""})
            if title:
                a["title"] = title
            a["stage"] = stage
            a["stage_since"] = now_iso()
        self.log(f"#{task} {stage}")

    def action(self, task: int, text: str) -> None:
        with self._lock:
            a = self._activity.get(task)
            if a is None:
                return
            a["actions"] += 1
            a["last"] = text[:160]
            a["last_at"] = now_iso()

    def done(self, task: int) -> None:
        with self._lock:
            self._activity.pop(task, None)

    def log(self, line: str) -> None:
        with self._lock:
            self._log.append((now_iso(), line))

    # ---- read by the dashboard ----
    def read(self) -> dict:
        with self._lock:
            return {"activity": [dict(a) for a in sorted(self._activity.values(), key=lambda a: a["since"])],
                    "log": [{"at": at, "line": l} for at, l in self._log]}


def describe(tool: str, args: dict, root: str = "") -> str:
    """One line for a tool call: `Edit Source/LSUI/Private/LSClock.cpp`, `$ git commit -m ...`."""
    def rel(p: str) -> str:
        p = str(p).replace("\\", "/")
        r = root.replace("\\", "/").rstrip("/") + "/" if root else ""
        return p[len(r):] if r and p.startswith(r) else p
    if tool in ("Edit", "Write", "Read", "NotebookEdit"):
        return f"{tool} {rel(args.get('file_path', ''))}"
    if tool == "Bash":
        cmd = " ".join(str(args.get("command", "")).split())
        return f"$ {cmd[:120]}"
    if tool in ("Grep", "Glob"):
        return f"{tool} {args.get('pattern', '')}"[:120]
    return tool


def ledger_tail(path: Path, n: int = 40) -> list[dict]:
    import json
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()[-n:]
    out = []
    for l in reversed(lines):
        try:
            out.append(json.loads(l))
        except ValueError:
            pass
    return out


def wall_clock() -> float:
    return time.time()
