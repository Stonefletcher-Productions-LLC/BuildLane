"""Gates: checks Fletchery runs itself on a worker's branch, chosen by the project's rules (.fletchery/gates.json) from
the files the branch changed. A worker cannot skip them because it does not run them. They are quick checks; the
project's full build is the Build Lane's job."""
from __future__ import annotations

import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class GateResult:
    command: str
    passed: bool
    output: str


def select(changed: list[str], extra: list[str] = (), rules=None) -> list[str]:
    """The checks for a change: the project's `always` list, the ones whose paths it touched (rules.py), and the
    issue's own `Gate:` lines."""
    if rules is None:
        from rules import Rules
        rules = Rules()
    return rules.checks_for(changed, extra)


def localise(cmd: str, cwd: Path, fallback_root: Path | str | None = None) -> str:
    """A check script that is not in the task's folder runs from the project's main checkout (`fallback_root`) instead,
    still working on the task folder (the current directory): rules and their scripts can be on their way to the base
    branch while tasks already use them. Seen 2026-11-02: every LifeShare task failed its convention check because
    Test-Staging did not have the script yet."""
    parts = cmd.split()
    if len(parts) < 2 or not fallback_root:
        return cmd
    script = parts[1].strip('"')
    if script.startswith(("-", "/")) or Path(script).is_absolute() or (Path(cwd) / script).exists():
        return cmd
    mine = Path(fallback_root) / script
    if not mine.exists():
        return cmd
    parts[1] = f'"{mine}"' if " " in str(mine) else str(mine)
    return " ".join(parts)


def resolve(cmd: str) -> str:
    """Run Python checks with Fletchery's own interpreter. On Windows `python3` is usually the Microsoft Store
    placeholder, which prints "Python was not found" and fails every gate (2026-11-02, LifeShare #450)."""
    m = re.match(r"^(python3?|py)\s+", cmd)
    if not m:
        return cmd
    exe = f'"{sys.executable}"' if " " in sys.executable or "\\" in sys.executable else shlex.quote(sys.executable)
    return exe + " " + cmd[m.end():]


def run(cmds: list[str], cwd: Path, timeout: int = 900, fallback_root: Path | str | None = None) -> list[GateResult]:
    out: list[GateResult] = []
    for c in cmds:
        try:
            p = subprocess.run(resolve(localise(c, cwd, fallback_root)), shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout)
            text = (p.stdout + p.stderr).strip()
            out.append(GateResult(c, p.returncode == 0, text[-4000:]))
        except subprocess.TimeoutExpired:
            out.append(GateResult(c, False, f"timed out after {timeout}s"))
        except OSError as e:
            out.append(GateResult(c, False, str(e)))
    return out


def summary(results: list[GateResult]) -> str:
    return "\n".join(f"- {'PASS' if r.passed else 'FAIL'} `{r.command}`" for r in results) or "- (no gates selected)"
