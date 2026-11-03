"""API keys for the agents, kept in Windows Credential Manager (generic credentials named "Fletchery/<ENV_NAME>"),
never in the profile, the repository or the logs. A session gets a key as an environment variable only when its agent
lists that variable in `keyEnv` (agents.py). The dashboard can set and clear keys and shows only the last 4 characters.

Elsewhere than Windows (tests, a Linux box) keys live in this process's memory only.
"""
from __future__ import annotations

import os
import re

PREFIX = "Fletchery/"
NAME = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")

# Keys the dashboard offers by default; any other ENV_NAME can be added.
KNOWN = {
    "ANTHROPIC_API_KEY": "Anthropic (Claude, when sessions run on the API instead of the plan)",
    "OPENAI_API_KEY": "OpenAI (Codex on an API key instead of ChatGPT)",
    "DEEPSEEK_API_KEY": "DeepSeek's own API (OpenCode provider deepseek/...)",
    "OPENROUTER_API_KEY": "OpenRouter (OpenCode provider openrouter/...)",
    "MOONSHOT_API_KEY": "Moonshot / Kimi (OpenCode provider moonshotai/...)",
}

_memory: dict[str, str] = {}


def _check(name: str) -> None:
    if not NAME.match(name or ""):
        raise ValueError("A key's name is an environment variable name, like DEEPSEEK_API_KEY.")


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    class _CRED(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
                    ("Comment", wintypes.LPWSTR), ("LastWritten", wintypes.FILETIME),
                    ("CredentialBlobSize", wintypes.DWORD), ("CredentialBlob", ctypes.POINTER(ctypes.c_char)),
                    ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
                    ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR)]

    _adv = ctypes.WinDLL("advapi32", use_last_error=True)
    _adv.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(_CRED))]
    _adv.CredWriteW.argtypes = [ctypes.POINTER(_CRED), wintypes.DWORD]
    _adv.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    _adv.CredFree.argtypes = [ctypes.c_void_p]
    _GENERIC, _LOCAL_MACHINE = 1, 2

    def get(name: str) -> str | None:
        _check(name)
        p = ctypes.POINTER(_CRED)()
        if not _adv.CredReadW(PREFIX + name, _GENERIC, 0, ctypes.byref(p)):
            return None
        try:
            c = p.contents
            return ctypes.string_at(c.CredentialBlob, c.CredentialBlobSize).decode("utf-16-le")
        finally:
            _adv.CredFree(p)

    def put(name: str, value: str) -> None:
        _check(name)
        blob = value.encode("utf-16-le")
        buf = ctypes.create_string_buffer(blob, len(blob))
        c = _CRED(Type=_GENERIC, TargetName=PREFIX + name, CredentialBlobSize=len(blob),
                  CredentialBlob=ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)), Persist=_LOCAL_MACHINE,
                  UserName="fletchery", Comment="Set from the Fletchery dashboard")
        if not _adv.CredWriteW(ctypes.byref(c), 0):
            raise OSError(f"Credential Manager refused the key (error {ctypes.get_last_error()})")

    def delete(name: str) -> None:
        _check(name)
        _adv.CredDeleteW(PREFIX + name, _GENERIC, 0)
else:
    def get(name: str) -> str | None:
        _check(name)
        return _memory.get(name)

    def put(name: str, value: str) -> None:
        _check(name)
        _memory[name] = value

    def delete(name: str) -> None:
        _check(name)
        _memory.pop(name, None)


def set_key(name: str, value: str) -> None:
    value = (value or "").strip()
    if not value or len(value) > 4096 or any(ch in value for ch in "\r\n"):
        raise ValueError("Paste the key on its own: one line, nothing else.")
    put(name, value)


def env_for(names: list[str] | None) -> dict[str, str]:
    """The stored keys among `names`, ready to merge into a session's environment."""
    out = {}
    for n in names or []:
        try:
            v = get(n)
        except ValueError:
            continue
        if v:
            out[n] = v
    return out


def listing(extra: list[str] | None = None) -> list[dict]:
    """Every known or used key name: whether it is stored, the last 4 characters if so. Never the key."""
    names = list(KNOWN) + [n for n in (extra or []) if n not in KNOWN]
    rows = []
    for n in names:
        try:
            v = get(n)
        except ValueError:
            continue
        rows.append({"name": n, "what": KNOWN.get(n, ""), "set": bool(v), "hint": ("…" + v[-4:]) if v else ""})
    return rows
