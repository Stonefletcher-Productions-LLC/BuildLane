"""Start, list and stop private Blender MCP bridges for unattended sessions.

Why this exists: a worker used to start its bridge with `Start-BlenderMCP.ps1`. A Claude session cannot run that. Its
PowerShell tool refuses a script call outright ("contains multiple operations"), and its Bash tool refuses a nested
`powershell ...` unless the command matches an allow rule letter for letter. Workers spell it `powershell.exe`, quote
the path or drop a flag, so the rule in the profile almost never matched and art tasks stopped in Needs you
(LifeShare #930, #955, #1045, 2026-11-09). A plain `python <this file> start` needs no rule: the worker and fixer
roles already allow `python`.

    python blender_bridge.py start [--port N] [--owner DIR]   start a bridge (or reuse the owner's), print its port
    python blender_bridge.py list                             the bridges this file started that still run
    python blender_bridge.py stop --port N                    stop one this file started
    python blender_bridge.py reap                             stop those whose owner folder is gone

Every command prints one JSON line. Standard library only; it is run by file path from any folder.

A bridge is only ever stopped by the process id recorded when this file started it, and only while that id still has
the recorded start time, so a recycled id or somebody else's Blender is never touched.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HOST = "127.0.0.1"
PORTS = range(10100, 10900)          # private bridges; 9876 is the shared default and is never handed out
START_TIMEOUT = 120.0                # a cold Blender with add-ons takes 10-40 s to listen


def registry_dir() -> Path:
    return Path(os.environ.get("FLETCHERY_BRIDGE_DIR") or Path(tempfile.gettempdir()) / "fletchery-bridges")


def _load() -> dict:
    try:
        data = json.loads((registry_dir() / "bridges.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data: dict) -> None:
    folder = registry_dir()
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / f"bridges.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, folder / "bridges.json")


def listening(port: int, timeout: float = 0.5) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((HOST, int(port))) == 0


def _bindable(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((HOST, port))
            return True
        except OSError:
            return False


def started_at(pid: int) -> int:
    """When the process started, as an opaque number that differs for a recycled id. 0 = no such process."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        k32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = k32.OpenProcess(0x1000, False, int(pid))   # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return 0
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:   # 259 = still running
                return 0
            made, a, b, c = (wintypes.FILETIME() for _ in range(4))
            if not k32.GetProcessTimes(handle, ctypes.byref(made), ctypes.byref(a), ctypes.byref(b), ctypes.byref(c)):
                return 0
            return (made.dwHighDateTime << 32) | made.dwLowDateTime
        finally:
            k32.CloseHandle(handle)
    try:
        stat = Path(f"/proc/{int(pid)}/stat").read_text()
        fields = stat.rsplit(")", 1)[1].split()
        return 0 if fields[0] == "Z" else int(fields[19])
    except (OSError, ValueError, IndexError):
        return 0


def alive(entry: dict) -> bool:
    return bool(entry.get("pid")) and started_at(entry["pid"]) == entry.get("started_at") != 0


def find_blender(explicit: str = "") -> str:
    for candidate in (explicit, os.environ.get("FLETCHERY_BLENDER", "")):
        if candidate:
            return candidate
    roots = [os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramW6432", "")]
    found: list[str] = []
    for root in dict.fromkeys(r for r in roots if r):
        found += glob.glob(os.path.join(root, "Blender Foundation", "Blender *", "blender.exe"))

    def version(path: str) -> tuple:
        parts = os.path.basename(os.path.dirname(path)).replace("Blender", "").strip().split(".")
        return tuple(int(p) if p.isdigit() else 0 for p in parts)
    if found:
        return max(found, key=version)
    return shutil.which("blender") or ""


def _same(a: str, b: str) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _prune(data: dict) -> dict:
    return {port: e for port, e in data.items() if isinstance(e, dict) and alive(e)}


def start(port: int = 0, owner: str = "", blender: str = "", user_scripts: str = "", command: list | None = None,
          timeout: float = START_TIMEOUT) -> dict:
    """Start a bridge for `owner` (a folder, normally the task checkout). With no port, the owner's running bridge is
    reused, else a free port is picked. `command` replaces the Blender command line (tests)."""
    owner = os.path.abspath(owner or os.getcwd())
    data = _prune(_load())
    if not port:
        for p, e in data.items():
            if _same(e.get("owner", ""), owner) and listening(int(p)):
                return {"ok": True, "port": int(p), "pid": e["pid"], "started": False, "owner": owner,
                        "note": "this folder's bridge is already running"}
        port = next((p for p in PORTS if str(p) not in data and _bindable(p)), 0)
        if not port:
            return {"ok": False, "error": f"no free port in {PORTS.start}-{PORTS.stop - 1}"}
    port = int(port)
    if not 1024 <= port <= 65535:
        return {"ok": False, "error": "choose a port between 1024 and 65535"}
    if listening(port):
        entry = data.get(str(port))
        if entry and _same(entry.get("owner", ""), owner):
            return {"ok": True, "port": port, "pid": entry["pid"], "started": False, "owner": owner,
                    "note": "this folder's bridge is already running"}
        return {"ok": False, "port": port, "error": f"port {port} already has a listener that this folder does not own; "
                                                     "run start without --port to get a free one"}
    if command is None:
        exe = find_blender(blender)
        if not exe or not os.path.isfile(exe):
            return {"ok": False, "error": f"Blender not found ({exe or 'no install under Program Files'}); "
                                          "pass --blender or set FLETCHERY_BLENDER"}
        command = [exe, "--background", "--online-mode", "--addons", "bl_ext.user_default.mcp",
                   "--command", "blender_mcp", "--host", HOST, "--port", str(port)]
    folder = registry_dir()
    folder.mkdir(parents=True, exist_ok=True)
    log = folder / f"bridge-{port}.log"
    env = dict(os.environ)
    if user_scripts:
        os.makedirs(user_scripts, exist_ok=True)
        env["BLENDER_USER_SCRIPTS"] = os.path.abspath(user_scripts)
    kwargs: dict = {"stdin": subprocess.DEVNULL, "env": env, "close_fds": True}
    flags = []
    if os.name == "nt":
        # Detached and in its own group so it outlives this command and the session's console; below normal priority
        # because builds and the player's other work come first. Leaving the caller's job object is refused on some
        # setups, so try with it and then without.
        base = 0x00000008 | 0x00000200 | 0x00004000   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | BELOW_NORMAL
        flags = [base | 0x01000000, base]              # CREATE_BREAKAWAY_FROM_JOB
    else:
        kwargs["start_new_session"] = True
    proc, error = None, ""
    with open(log, "ab") as out:
        for f in flags or [0]:
            try:
                proc = subprocess.Popen(command, stdout=out, stderr=subprocess.STDOUT, creationflags=f, **kwargs) \
                    if os.name == "nt" else subprocess.Popen(command, stdout=out, stderr=subprocess.STDOUT, **kwargs)
                break
            except OSError as exc:
                error = f"{type(exc).__name__}: {exc}"
    if proc is None:
        return {"ok": False, "port": port, "error": f"could not start Blender: {error}"}
    entry = {"pid": proc.pid, "started_at": started_at(proc.pid), "owner": owner, "log": str(log),
             "time": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    data = _prune(_load())
    data[str(port)] = entry
    _save(data)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if listening(port):
            return {"ok": True, "port": port, "pid": proc.pid, "started": True, "owner": owner, "log": str(log)}
        if proc.poll() is not None:
            break
        time.sleep(0.5)
    exited = proc.poll()
    if exited is None:
        _terminate(entry)
    data = _load()
    data.pop(str(port), None)
    _save(data)
    try:
        tail = log.read_text(encoding="utf-8", errors="replace")[-600:]
    except OSError:
        tail = ""
    why = f"Blender exited with code {exited}" if exited is not None else f"no listener after {int(timeout)} s"
    return {"ok": False, "port": port, "error": f"{why}; this is a runtime failure, not a permission refusal",
            "log": str(log), "log_tail": tail}


def _terminate(entry: dict) -> bool:
    if not alive(entry):
        return False
    try:
        os.kill(int(entry["pid"]), signal.SIGTERM)   # Windows: TerminateProcess on exactly this id
    except OSError:
        return False
    for _ in range(40):
        if not alive(entry):
            return True
        time.sleep(0.25)
    return not alive(entry)


def stop(port: int) -> dict:
    data = _load()
    entry = data.get(str(int(port)))
    if not entry:
        return {"ok": False, "port": int(port), "error": "this port was not started by blender_bridge.py; left alone"}
    stopped = _terminate(entry)
    data.pop(str(int(port)), None)
    _save(_prune(data))
    return {"ok": True, "port": int(port), "stopped": stopped}


def bridges() -> list[dict]:
    return [{"port": int(p), **e, "listening": listening(int(p))} for p, e in sorted(_prune(_load()).items())]


def reap() -> dict:
    """Stop the bridges whose owner folder no longer exists (its task checkout was retired). Others are left alone."""
    data = _load()
    stopped = []
    for port, entry in list(data.items()):
        if isinstance(entry, dict) and alive(entry) and entry.get("owner") and not os.path.isdir(entry["owner"]):
            if _terminate(entry):
                stopped.append(int(port))
    _save(_prune(data))
    return {"ok": True, "stopped": stopped}


def worker_note() -> str:
    """What a session that may run commands is told (runtime.role_prompt). The path is this installed file."""
    me = Path(__file__).resolve().as_posix()
    return ("\n# Starting a Blender bridge\n\n"
            "Start your private bridge with the Bash tool, as one plain command with nothing chained to it:\n\n"
            f"    python {me} start\n\n"
            "It prints one JSON line with the `port` to pass to `call.py --port`. Run from your checkout it gives each "
            "task its own bridge and reuses the one this task already has. `stop --port <port>` stops it and `list` "
            "shows yours. Do not use `Start-BlenderMCP.ps1`, `powershell` or the PowerShell tool for this: those are "
            "refused in unattended sessions. If `start` prints `\"ok\": false`, that is a runtime failure with the "
            "reason in `error` and `log_tail`: report it; do not call it an approval refusal.\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="action", required=True)
    s = sub.add_parser("start")
    s.add_argument("--port", type=int, default=0)
    s.add_argument("--owner", default="")
    s.add_argument("--blender", default="")
    s.add_argument("--user-scripts", default="")
    s.add_argument("--timeout", type=float, default=START_TIMEOUT)
    sub.add_parser("list")
    sub.add_parser("reap")
    sub.add_parser("stop").add_argument("--port", type=int, required=True)
    args = parser.parse_args(argv)
    if args.action == "start":
        result = start(args.port, args.owner, args.blender, args.user_scripts, timeout=args.timeout)
    elif args.action == "stop":
        result = stop(args.port)
    elif args.action == "reap":
        result = reap()
    else:
        result = {"ok": True, "bridges": bridges()}
    print(json.dumps(result))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
