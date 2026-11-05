"""Read-only reservation checks. Never delete leases, signal owners or acquire resources."""
from pathlib import Path


def wait_reason(path: str, earliest: str, now: str) -> str:
    if now < earliest:
        return "waiting before checking the heavy-work reservation again"
    if not path:
        return "heavy-work lease configuration is missing"
    try:
        Path(path).read_bytes()
    except FileNotFoundError:
        return ""
    except OSError:
        return "heavy-work reservation cannot be checked; preserving the wait"
    return "waiting for the heavy-work reservation to be released"
