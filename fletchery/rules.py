"""A project's own rules for its workers, kept in its repository so they are versioned with the code they describe:

    .fletchery/conventions.md   what every worker, reviewer and fixer must follow (naming, UI rules, saves, ...)
    .fletchery/gates.json       which checks Fletchery runs on a worker's change, by changed path
    .fletchery/worker.md        optional: added to the worker prompt (also reviewer.md, fixer.md, planner.md)

gates.json:

    {
      "always": ["python3 .fletchery/check_conventions.py"],
      "rules": [
        {"paths": "^Source/LSUI/|^Content/Strings/", "run": "python3 Tools/ScanUIText.py"},
        {"paths": "^\\.github/workflows/lane\\.yml$", "run": ["node Tools/CI/TestLaneCut.cjs", "node Tools/CI/TestLaneRelease.cjs"]}
      ]
    }

Rules are read from the task's branch first and from the project's main checkout when the branch does not have them
yet (a project adopting Fletchery: the rules are still on their way to the base branch)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

RULES_DIR = ".fletchery"
NO_GATE = {"", "none", "n/a", "na", "-", "no", "nothing"}


@dataclass
class Rules:
    conventions: str = ""
    always: list[str] = field(default_factory=list)
    by_path: list[tuple[str, list[str]]] = field(default_factory=list)
    additions: dict[str, str] = field(default_factory=dict)  # role -> extra prompt text
    source: str = ""                                          # where they were read from, for the log

    def checks_for(self, changed: list[str], extra: list[str] = ()) -> list[str]:
        cmds = list(self.always)
        for pattern, runs in self.by_path:
            rx = re.compile(pattern)
            if any(rx.search(p) for p in changed):
                cmds += [r for r in runs if r not in cmds]
        for e in extra:
            e = e.strip().strip("`").strip()
            if e.lower().rstrip(".") in NO_GATE:
                continue  # "Gate: none" means no extra check; it used to run `node none` and block (LifeShare #846-#848)
            if e and e not in cmds:
                cmds.append(e if e.split()[0] in ("python3", "python", "node") else f"python3 {e}" if e.endswith(".py") else f"node {e}")
        return cmds


def load(*roots: Path | str | None) -> Rules:
    """The first of `roots` that has a .fletchery folder wins; no folder anywhere means no project rules."""
    for root in roots:
        if not root:
            continue
        d = Path(root) / RULES_DIR
        if not d.is_dir():
            continue
        r = Rules(source=str(d))
        conv = d / "conventions.md"
        if conv.exists():
            r.conventions = conv.read_text(encoding="utf-8-sig")
        g = d / "gates.json"
        if g.exists():
            data = json.loads(g.read_text(encoding="utf-8-sig"))
            r.always = [str(c) for c in data.get("always", [])]
            for rule in data.get("rules", []):
                runs = rule.get("run", [])
                r.by_path.append((str(rule["paths"]), [runs] if isinstance(runs, str) else [str(x) for x in runs]))
        for role in ("worker", "reviewer", "fixer", "planner"):
            f = d / f"{role}.md"
            if f.exists():
                r.additions[role] = f.read_text(encoding="utf-8-sig")
        return r
    return Rules()
