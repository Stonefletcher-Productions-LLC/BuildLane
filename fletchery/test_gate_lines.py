"""Gate: lines written as prose must never run prose as a command (LifeShare #991, 2026-11-07)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from graph import Task, gate_items  # noqa: E402
import rules  # noqa: E402


def check(got, want, label):
    assert got == want, f"{label}: got {got!r}, want {want!r}"


def main() -> int:
    check(gate_items("python Tools/a.py, node Tools/b.cjs"), ["python Tools/a.py", "node Tools/b.cjs"], "plain list")
    check(gate_items("`python -I Tools/AuditHair.py` and `python Tools/test_meshy_hair_export.py`"),
          ["python -I Tools/AuditHair.py", "python Tools/test_meshy_hair_export.py"], "backticks")
    check(gate_items("git diff --check"), ["git diff --check"], "git check")
    check(gate_items("Tools/CheckEnvironmentExports.py"), ["Tools/CheckEnvironmentExports.py"], "bare script")
    check(gate_items("none"), ["none"], "none kept for rules to drop")
    prose = ("python Tools/test_meshy_hair_export.py must pass again. Gate: python -I Tools/AuditHair.py remains "
             "required; the saved attempt failed importing bpy and is not a pass. Resolve/document that invocation "
             "accurately and run the actual audit against all eight fresh FBXs through official MCP in a bpy-capable "
             "context, using the compatible owned-scene reset; retain file-specific results. A no-input usage message "
             "does not satisfy acceptance, and a substitute invocation must not be reported as the literal gate passing.")
    check(gate_items(prose), ["python Tools/test_meshy_hair_export.py", "python -I Tools/AuditHair.py"], "prose line")
    check(gate_items("python Tools/x.py."), ["python Tools/x.py"], "trailing full stop")
    check(gate_items("python .fletchery/check_conventions.py when supplied by the harness; record it."),
          ["python .fletchery/check_conventions.py"], "condition clause")

    body = ("Scope...\n- Gate: `python -I Tools/AuditHair.py` and `python Tools/test_meshy_hair_export.py`\n\n"
            "Gate: " + prose + "\n")
    task = Task(number=991, title="Hair 2/4", body=body)
    check(task.gates, ["python Tools/test_meshy_hair_export.py", "python -I Tools/AuditHair.py"], "task dedupes")
    cmds = rules.Rules().checks_for([], task.gates)
    assert all(not c.startswith("node ") for c in cmds), cmds
    print("gate line checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
