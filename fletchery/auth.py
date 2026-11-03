"""Who pays for sessions: the Claude CLI's own plan sign-in ("subscription") or an API key ("api"). Checked before
Fletchery starts and whenever the dashboard switches it; a switch applies to the next session, running ones finish."""
from __future__ import annotations

import json
import os
import subprocess


def plan_status() -> tuple[bool, str]:
    """(ok, message) for the Claude plan sign-in."""
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return True, "Using the Claude plan token in CLAUDE_CODE_OAUTH_TOKEN."
    try:
        p = subprocess.run(["claude", "auth", "status"], capture_output=True, text=True, timeout=30)
        status = json.loads(p.stdout or "{}")
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        return False, f"Could not ask the Claude CLI how it is signed in ({e}). Is `claude` on PATH?"
    if not status.get("loggedIn") or status.get("authMethod") != "claude.ai":
        return False, "The Claude CLI is not signed in with a Claude plan. Run `claude`, then /login."
    return True, f"Using the {status.get('subscriptionType', 'Claude')} plan of {status.get('email', '?')}."


def api_key(saved: str | None = None) -> str | None:
    """The API key from this process, from what Fletchery set aside at start, or (Windows) the user's environment as it
    is now, so a key set after Fletchery started still counts."""
    key = os.environ.get("ANTHROPIC_API_KEY") or saved
    if key:
        return key
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                return winreg.QueryValueEx(k, "ANTHROPIC_API_KEY")[0] or None
        except OSError:
            return None
    return None


def check(auth: str, saved_key: str | None = None) -> tuple[bool, str]:
    if auth == "api":
        if api_key(saved_key):
            return True, "Using the API key (billed per token)."
        return False, "No API key: set ANTHROPIC_API_KEY for your Windows user first, then switch again."
    if auth == "subscription":
        return plan_status()
    return False, f'auth must be "subscription" or "api", not "{auth}".'
