"""A Fletchery project: today's BuildLane profile (repo, status file, pause file, drive...) plus a `workers` block for
the Workers engine. One file per project, the same file the Projects screen edits.

    {
      "name": "LifeShare", "repo": "owner/LifeShare",
      "statusFile": "E:\\GitHubRunner\\status.json", "pauseFile": "E:\\GitHubRunner\\PAUSED", "drive": "E",
      "minimumFreeGB": 12,
      "workers": {
        "enabled": true, "projectRoot": "E:\\lifeshare-scratch\\foreman\\host", "baseBranch": "Test-Staging",
        "maxParallel": 2, "dailyBudgetUsd": 60, "auth": "subscription", ...
      }
    }

Project rules (conventions, which checks run, prompt additions) live in the project's own repository, in `.fletchery/`
(see rules.py), not here."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, fields
from pathlib import Path

HERE = Path(__file__).parent
PRODUCT_ROOT = HERE.parent  # this repository / install folder


def default_state_dir(name: str) -> str:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in (name or "project")).strip("-") or "project"
    return str(Path(base) / "Fletchery" / safe)


# profile key (in "workers") -> Config field
WORKER_KEYS = {
    "autoPrerequisites": "auto_prerequisites",
    "enabled": "workers_enabled", "projectRoot": "project_root", "baseBranch": "base_branch", "assignee": "assignee",
    "worktreeRoot": "worktree_root", "stateDir": "state_dir", "maxParallel": "max_parallel", "pollMinutes": "poll_minutes",
    "model": "model", "auth": "auth", "limitBackoffMinutes": "limit_backoff_minutes",
    "workerBudgetUsd": "worker_budget_usd", "reviewerBudgetUsd": "reviewer_budget_usd", "fixerBudgetUsd": "fixer_budget_usd",
    "plannerBudgetUsd": "planner_budget_usd", "dailyBudgetUsd": "daily_budget_usd",
    "workerMaxTurns": "worker_max_turns", "reviewerMaxTurns": "reviewer_max_turns", "fixerMaxTurns": "fixer_max_turns",
    "plannerMaxTurns": "planner_max_turns", "fixerAttempts": "fixer_attempts", "codeFreeze": "code_freeze",
    "gateTimeoutSeconds": "gate_timeout_s", "plugin": "plugin", "dashboardPort": "dashboard_port", "label": "label",
    "watchFailures": "watch_failures", "runnerRoot": "runner_root", "watchSkipWorkflows": "watch_skip_workflows",
    "watchMinutes": "watch_minutes", "mergeFlags": "merge_flags", "mergeRoundFile": "merge_round_file",
    "heavyLeaseFile": "heavy_lease_file", "mergeRounds": "merge_rounds", "mergeLabel": "merge_label",
    "mergeRoundMax": "merge_round_max", "mergeAgent": "merge_agent", "mergeNotes": "merge_notes",
    "mergeBudgetUsd": "merge_budget_usd", "mergeMaxTurns": "merge_max_turns", "mergeRetryMinutes": "merge_retry_minutes", "agents": "agents", "routing": "routing", "escalate": "escalate",
}
# profile key (top level) -> Config field
PROFILE_KEYS = {"name": "name", "repo": "repo", "statusFile": "lane_status_file", "pauseFile": "lane_pause_file",
                "drive": "lane_drive", "minimumFreeGB": "lane_min_free_gb"}


@dataclass
class Config:
    name: str = "project"
    repo: str = ""
    workers_enabled: bool = True
    project_root: str = ""                   # a local clone of the project; task worktrees are made from it
    base_branch: str = "main"
    label: str = "fletchery"                 # issues with this label are tasks; status labels are "<label>:queued" ...
    assignee: str = ""
    worktree_root: str = ""                  # "" = <state_dir>/worktrees
    state_dir: str = ""                      # "" = %LOCALAPPDATA%\Fletchery\<name>
    max_parallel: int = 2                    # on a Claude plan the limits are shared with your own sessions
    auto_prerequisites: bool = True          # turn actionable blocks into dependencies, not repeated human prompts
    poll_minutes: float = 10
    dashboard_port: int = 8770               # http://127.0.0.1:<port> while Fletchery runs
    model: str = ""                          # "" = the Claude CLI's default
    # "subscription": sessions use the Claude CLI's own sign-in (a Claude plan); any ANTHROPIC_API_KEY is removed from
    # their environment so nothing bills the API by accident. "api": sessions use ANTHROPIC_API_KEY.
    auth: str = "subscription"
    limit_backoff_minutes: int = 30          # after a usage-limit hit, dispatch nothing new for this long
    worker_budget_usd: float = 8.0
    reviewer_budget_usd: float = 2.0
    fixer_budget_usd: float = 5.0
    planner_budget_usd: float = 4.0
    daily_budget_usd: float = 60.0
    worker_max_turns: int = 80
    reviewer_max_turns: int = 25
    fixer_max_turns: int = 50
    planner_max_turns: int = 40
    fixer_attempts: int = 2
    code_freeze: bool = False                # True tells every worker and fixer the project is under a code freeze
    gate_timeout_s: int = 900
    plugin: str = ""                         # a Claude Code plugin folder, relative to the project root
    # the lane watcher (watcher.py): failed builds outside workers' PRs become tasks
    watch_failures: bool = True
    runner_root: str = ""                    # the local runner's run store; "" = from its runner.json, else GitHub Actions
    watch_skip_workflows: list = None        # workflow file names never filed (e.g. advisory ones)
    watch_minutes: float = 5
    # the Merge section: notes shown on a PR waiting to merge when it changes matching paths, e.g.
    # [{"paths": "^Content/Town/|Furniture", "flag": "strict NavCheck on the PC"}]
    merge_flags: list = None
    merge_round_file: str = ""               # what the merge round is doing (merge.py); "" = <state_dir>/merge-round.json
    heavy_lease_file: str = ""               # who holds the PC for heavy work (HeavyLease.ps1's heavy.json); "" = not shown
    # merge rounds (rounds.py): Fletchery merges, builds, tests and lands `ready` PRs by itself, one round at a time
    merge_rounds: bool = False
    merge_label: str = "ready"               # PRs with this label into the base branch go in the next round
    merge_round_max: int = 12                # at most this many PRs per round, oldest first
    merge_agent: str = ""                    # the agent that runs rounds; "" = the strongest available Claude/OpenCode one
    merge_notes: str = ""                    # a file with the project's round instructions when the repo has no .fletchery/merger.md
    merge_budget_usd: float = 60.0
    merge_max_turns: int = 600
    merge_retry_minutes: float = 45          # after a round that landed nothing, wait this long before trying the same PRs
    # the coding agents (agents.py): [{"name", "kind": claude|codex|opencode, "model", "tier", "vendor", "enabled",
    # "maxParallel", "keyEnv"}]; None = Claude only. `routing` says which agent takes which difficulty.
    agents: list = None
    routing: dict = None
    escalate: bool = True                    # a failed attempt goes to a stronger agent before the task is blocked
    # the Build Lane, from the profile; "" hides the panel
    lane_status_file: str = ""
    lane_pause_file: str = ""
    lane_drive: str = ""
    lane_min_free_gb: float = 12.0

    # ---- labels ----
    @property
    def queued(self) -> str: return f"{self.label}:queued"
    @property
    def running(self) -> str: return f"{self.label}:running"
    @property
    def in_pr(self) -> str: return f"{self.label}:pr"
    @property
    def blocked(self) -> str: return f"{self.label}:blocked"
    @property
    def status_label(self) -> str: return f"{self.label}-status"

    # ---- loading and saving ----
    @staticmethod
    def load(path: Path | None = None) -> "Config":
        """A profile (.json). Old Foreman configs (flat snake_case keys) load too."""
        cfg = Config()
        cfg._path = Path(path) if path else None
        if cfg._path and cfg._path.exists():
            data = json.loads(cfg._path.read_text(encoding="utf-8-sig"))
            names = {f.name for f in fields(Config)}
            for k, v in data.items():
                if k in PROFILE_KEYS:
                    setattr(cfg, PROFILE_KEYS[k], v)
                elif k in names:  # a flat Foreman config
                    setattr(cfg, k, v)
            for k, v in (data.get("workers") or {}).items():
                if k in WORKER_KEYS:
                    setattr(cfg, WORKER_KEYS[k], v)
        if not cfg.state_dir:
            cfg.state_dir = default_state_dir(cfg.name)
        if not cfg.worktree_root:
            cfg.worktree_root = str(Path(cfg.state_dir) / "worktrees")
        return cfg

    def save(self, **changes) -> None:
        """Apply `changes` (Config field names) and write them back into the profile they came from: lane fields at
        the top level, everything else in the `workers` block. Other keys are left as they are."""
        for k in changes:
            if not hasattr(self, k):
                raise KeyError(k)
        for k, v in changes.items():
            setattr(self, k, v)
        path = getattr(self, "_path", None)
        if not path:
            return
        data = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
        flat = not ("workers" in data or "repo" in data and "statusFile" in data)
        to_profile = {v: k for k, v in PROFILE_KEYS.items()}
        to_workers = {v: k for k, v in WORKER_KEYS.items()}
        for k, v in changes.items():
            if flat and k not in to_profile:
                data[k] = v
            elif k in to_profile:
                data[to_profile[k]] = v
            elif k in to_workers:
                data.setdefault("workers", {})[to_workers[k]] = v
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)

    def resolve(self, p: str) -> Path:
        """A path from the profile: absolute as is, relative to the project root otherwise."""
        q = Path(p)
        return q if q.is_absolute() else Path(self.project_root or ".") / q

    def dump(self) -> str:
        return json.dumps({f.name: getattr(self, f.name) for f in fields(Config)}, indent=2)
