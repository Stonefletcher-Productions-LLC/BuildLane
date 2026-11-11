"""PR checks on a test node: the PC check (prcheck.py) run by a node's `pr-check` recipe instead of the PC.

Profile, inside workers.machines:
    "prCheck": {"labels": ["unreal:5.8.3"]}     # extra labels a node needs to take PR checks (e.g. an engine version)

With it set and an eligible node online, Fletchery queues one PR check at a time for the node: the base branch tip as
the job's commit, the PR number and head as parameters. The node merges the head onto the base and runs the project's
check (LifeShare: Tools/CI/PrCheck.ps1). Fletchery keeps every GitHub write: it reads the finished job and labels the PR
or posts the failure, exactly as for a check on the PC.

In shadow mode (workers.machines.shadow, the default) the node checks PRs alongside the PC and only its result is
recorded (state.node_pr_checks), so the two can be compared; the PC still decides. With shadow off the node's result
counts and the PC leaves PR checks to the node while one is eligible.
"""
from __future__ import annotations

import re

RECIPE = "pr-check"
MERGE_STEP = "Merge the PR onto the base"   # node_worker.MERGE_STEP
CHECK_STEP = "PR check"                     # node_worker.CHECK_STEP
NO_RESULT = 2
TAIL_LINES = 80


def conf_of(coord) -> dict | None:
    """The prCheck block, or None when node PR checks are off."""
    if coord is None:
        return None
    c = (getattr(coord, "conf", None) or {}).get("prCheck")
    return c if isinstance(c, dict) else None


def need_labels(conf: dict) -> list[str]:
    import machines
    base = list(machines.RECIPES[RECIPE]["labels"])
    return base + [str(x) for x in (conf.get("labels") or []) if str(x) not in base]


def eligible(coord, conf: dict) -> list[str]:
    import machines
    return coord.machines.eligible(need_labels(conf), machines.RECIPES[RECIPE]["min_ram_gb"])


def node_takes_checks(coord) -> bool:
    """True when the PC should leave PR checks to a node: prCheck is on, shadow is off, and a node can take them."""
    conf = conf_of(coord)
    return bool(conf) and not coord.machines.shadow and bool(eligible(coord, conf))


def verdict(job: dict) -> str:
    """"pass", "fail" (the PR's fault: it does not merge, or the check failed) or "none" (try again later)."""
    if job.get("state") == "passed":
        return "pass"
    if job.get("state") != "failed":
        return "none"
    summary, code = str(job.get("summary") or ""), job.get("exit_code")
    if code == NO_RESULT:
        return "none"
    if summary.startswith(MERGE_STEP + " failed") or summary.startswith(CHECK_STEP + " failed"):
        return "fail"
    return "none"


def log_tail(text: str, step: str) -> str:
    """The failing step's output, last TAIL_LINES lines, without the worker's step markers."""
    text = (text or "").replace("\r\n", "\n")
    i = text.rfind(f"##[step] {step}\n")
    if i >= 0:
        text = text[i:]
        end = re.search(rf"##\[step\] {re.escape(step)}: exit -?\d+", text)
        if end:
            text = text[:end.start()]
    lines = [x for x in text.rstrip().split("\n") if not x.startswith("##[step]")]
    return "\n".join(lines[-TAIL_LINES:])


def pick(waiting: list, active: list[dict], done: dict) -> object | None:
    """The first waiting PR without a queued/running node check and not node-checked at its head."""
    busy = {(int(j["params"].get("pr") or 0), j["params"].get("head")) for j in active}
    for p in waiting:
        if (p.number, p.head_sha) in busy:
            continue
        if (done.get(str(p.number)) or {}).get("head") == p.head_sha:
            continue
        return p
    return None
