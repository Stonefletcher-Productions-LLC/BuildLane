"""Projects and settings: what the app's Settings page reads and writes, so nobody has to hand-edit a profile.

A project is one profile file (.profile.json). New ones go in %APPDATA%\\Fletchery\\Projects. Fletchery runs one
project at a time; `active-profile.txt` beside that folder names the one to run, and `cli.py run` follows it, so a
switch made in the app survives a restart of the app.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from config import Config, default_state_dir


def home() -> Path:
    """%APPDATA%\\Fletchery (FLETCHERY_HOME overrides it, for tests)."""
    if os.environ.get("FLETCHERY_HOME"):
        return Path(os.environ["FLETCHERY_HOME"])
    base = os.environ.get("APPDATA") or str(Path.home() / ".config")
    return Path(base) / "Fletchery"


def projects_dir() -> Path:
    return home() / "Projects"


def active_file() -> Path:
    return home() / "active-profile.txt"


def active_profile() -> Path | None:
    """The project the app last switched to, if its file still exists."""
    try:
        p = Path(active_file().read_text(encoding="utf-8").strip())
    except OSError:
        return None
    return p if p.is_file() else None


def set_active(path: Path) -> None:
    active_file().parent.mkdir(parents=True, exist_ok=True)
    active_file().write_text(str(Path(path).resolve()) + "\n", encoding="utf-8")


def list_projects(current: Path | None) -> list[dict]:
    seen, out = set(), []
    paths = sorted(projects_dir().glob("*.profile.json")) if projects_dir().is_dir() else []
    if current and Path(current).is_file():
        paths = [Path(current)] + paths
    for p in paths:
        key = str(Path(p).resolve()).lower()
        if key in seen:
            continue
        seen.add(key)
        try:
            d = json.loads(Path(p).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        out.append({"name": d.get("name") or p.stem, "repo": d.get("repo", ""), "path": str(p),
                    "current": bool(current) and key == str(Path(current).resolve()).lower()})
    return out


# ---- the fields the Settings page shows ----
# key: Config field · type: text | path | int | float | bool | list · restart: takes effect after Fletchery restarts
FIELDS = [
    {"group": "Project", "key": "name", "label": "Name", "type": "text", "restart": True,
     "help": "What the app calls this project."},
    {"group": "Project", "key": "repo", "label": "GitHub repository", "type": "text", "restart": True,
     "help": "owner/name, e.g. acme/desktop. Tasks are issues here; pull requests are opened here."},
    {"group": "Project", "key": "project_root", "label": "Local clone", "type": "path", "restart": True,
     "help": "A folder with a git clone of that repository. Each task gets its own worktree made from it; your own "
             "checkout is never touched."},
    {"group": "Project", "key": "base_branch", "label": "Base branch", "type": "text",
     "help": "Workers branch from this and open pull requests into it, e.g. main or Test-Staging."},
    {"group": "Project", "key": "label", "label": "Task label", "type": "text", "restart": True,
     "help": "Issues with this label are tasks. Fletchery adds <label>:queued, :running, :pr and :blocked as it goes."},
    {"group": "Project", "key": "assignee", "label": "Assign pull requests to", "type": "text",
     "help": "A GitHub user name, or empty."},
    {"group": "Workers", "key": "workers_enabled", "label": "Workers on", "type": "bool", "restart": True,
     "help": "Off: Fletchery only shows the Build Lane."},
    {"group": "Workers", "key": "max_parallel", "label": "Workers at once", "type": "int", "min": 0, "max": 10,
     "help": "0 writes no new code; merging and builds carry on."},
    {"group": "Workers", "key": "auto_prerequisites", "label": "Handle blocked tasks automatically", "type": "bool",
     "help": "Create missing prerequisites or requeue concrete corrections. Real decisions and repeated failures stay in Needs you."},
    {"group": "Workers", "key": "daily_budget_usd", "label": "Daily cap ($)", "type": "float", "min": 0,
     "help": "No new task starts once today's estimated cost reaches this."},
    {"group": "Workers", "key": "poll_minutes", "label": "Check GitHub every (minutes)", "type": "float", "min": 1},
    {"group": "Workers", "key": "fixer_attempts", "label": "Fix attempts per PR", "type": "int", "min": 0, "max": 5,
     "help": "How often a fixer tries a pull request the Build Lane sent back, before it asks you."},
    {"group": "Workers", "key": "code_freeze", "label": "Code freeze", "type": "bool",
     "help": "Tells every worker and fixer the project is in a code freeze: fixes only."},
    {"group": "Workers", "key": "escalate", "label": "Escalate on failure", "type": "bool",
     "help": "A failed task goes to a stronger agent before it asks you."},
    {"group": "Workers", "key": "worker_budget_usd", "label": "Per task: writer ($)", "type": "float", "min": 0, "advanced": True},
    {"group": "Workers", "key": "reviewer_budget_usd", "label": "Per task: reviewer ($)", "type": "float", "min": 0, "advanced": True},
    {"group": "Workers", "key": "fixer_budget_usd", "label": "Per fix ($)", "type": "float", "min": 0, "advanced": True},
    {"group": "Workers", "key": "worker_max_turns", "label": "Writer turns", "type": "int", "min": 1, "advanced": True},
    {"group": "Workers", "key": "reviewer_max_turns", "label": "Reviewer turns", "type": "int", "min": 1, "advanced": True},
    {"group": "Workers", "key": "fixer_max_turns", "label": "Fixer turns", "type": "int", "min": 1, "advanced": True},
    {"group": "Workers", "key": "gate_timeout_s", "label": "Check timeout (seconds)", "type": "int", "min": 30, "advanced": True},
    {"group": "Workers", "key": "worktree_root", "label": "Worktrees folder", "type": "path", "restart": True, "advanced": True,
     "help": "Where task worktrees go. Empty: inside the state folder."},
    {"group": "Workers", "key": "dashboard_port", "label": "Dashboard port", "type": "int", "min": 1024, "max": 65535,
     "restart": True, "advanced": True},
    {"group": "Merging", "key": "merge_rounds", "label": "Merge rounds", "type": "bool",
     "help": "Fletchery merges, builds, tests and lands PRs with the merge label by itself, one round at a time, and "
             "never waits for approval. Workers at 0 don't stop it."},
    {"group": "Merging", "key": "merge_label", "label": "Merge label", "type": "text",
     "help": "PRs into the base branch with this label go in the next round."},
    {"group": "Merging", "key": "merge_round_max", "label": "PRs per round (most)", "type": "int", "min": 1, "max": 60},
    {"group": "Merging", "key": "merge_agent", "label": "Agent for rounds", "type": "text",
     "help": "An agent's name, or empty for the strongest available Claude or OpenCode agent."},
    {"group": "Merging", "key": "merge_notes", "label": "Round instructions file", "type": "path", "advanced": True,
     "help": "How a round builds and tests, when the repository has no .fletchery/merger.md."},
    {"group": "Merging", "key": "merge_budget_usd", "label": "Per round ($)", "type": "float", "min": 0, "advanced": True},
    {"group": "Merging", "key": "merge_max_turns", "label": "Round turns", "type": "int", "min": 10, "advanced": True},
    {"group": "Merging", "key": "merge_retry_minutes", "label": "Retry a failed round after (minutes)", "type": "float",
     "min": 5, "advanced": True},
    {"group": "Build Lane", "key": "lane_status_file", "label": "Status file", "type": "path",
     "help": "The JSON file your build runner writes as a build goes. Empty hides the Build Lane panel."},
    {"group": "Build Lane", "key": "lane_pause_file", "label": "Pause file", "type": "path",
     "help": "The file your runner checks before it starts; Pause in the app creates it."},
    {"group": "Build Lane", "key": "lane_drive", "label": "Build drive", "type": "text", "help": "e.g. E. Shown with its free space."},
    {"group": "Build Lane", "key": "lane_min_free_gb", "label": "Warn below (GB free)", "type": "float", "min": 0},
    {"group": "Build Lane", "key": "watch_failures", "label": "File failed builds as tasks", "type": "bool",
     "help": "A failed build that isn't a worker's PR becomes a task."},
    {"group": "Build Lane", "key": "runner_root", "label": "Runner folder", "type": "path", "advanced": True,
     "help": "The local runner's run store. Empty: from its runner.json, else GitHub Actions."},
    {"group": "Build Lane", "key": "heavy_lease_file", "label": "Heavy-work lease file", "type": "path", "advanced": True},
    {"group": "Build Lane", "key": "merge_round_file", "label": "Merge round file", "type": "path", "advanced": True},
]
_BY_KEY = {f["key"]: f for f in FIELDS}


def read_settings(cfg: Config) -> dict:
    return {"fields": FIELDS, "values": {f["key"]: getattr(cfg, f["key"]) for f in FIELDS},
            "profile": str(getattr(cfg, "_path", "") or ""), "state_dir": cfg.state_dir}


def _coerce(f: dict, v):
    t, label = f["type"], f["label"]
    if t == "bool":
        if isinstance(v, bool):
            return v
        if str(v).lower() in ("true", "1", "yes", "on"):
            return True
        if str(v).lower() in ("false", "0", "no", "off", ""):
            return False
        raise ValueError(f"{label} is on or off.")
    if t in ("int", "float"):
        try:
            n = int(v) if t == "int" and float(v) == int(float(v)) else float(v)
        except (TypeError, ValueError):
            raise ValueError(f"{label} is a number.")
        if t == "int" and n != int(n):
            raise ValueError(f"{label} is a whole number.")
        if "min" in f and n < f["min"] or "max" in f and n > f["max"]:
            raise ValueError(f"{label} goes from {f.get('min', '…')} to {f.get('max', '…')}.")
        return int(n) if t == "int" else float(n)
    s = str(v if v is not None else "").strip()
    if f["key"] == "repo":
        s = normalize_repo(s)
        if not re.match(r"^[\w.-]+/[\w.-]+$", s):
            raise ValueError("GitHub repository is owner/name, e.g. acme/desktop.")
    if f["key"] == "label" and not re.match(r"^[\w.-]{1,40}$", s):
        raise ValueError("Task label is one word: letters, digits, dots, dashes.")
    if f["key"] in ("name", "base_branch") and not s:
        raise ValueError(f"{label} can't be empty.")
    return s


def normalize_repo(s: str) -> str:
    s = s.strip()
    m = re.match(r"^(?:https?://)?(?:www\.)?github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", s)
    return f"{m.group(1)}/{m.group(2)}" if m else s


def apply_settings(cfg: Config, changes: dict) -> dict:
    """Validate and save. Returns {"saved": [labels], "restart": [labels that apply after a restart]}."""
    clean = {}
    for k, v in (changes or {}).items():
        if k not in _BY_KEY:
            raise ValueError(f"No setting called {k}.")
        clean[k] = _coerce(_BY_KEY[k], v)
    changed = {k: v for k, v in clean.items() if getattr(cfg, k) != v}
    if not changed:
        return {"saved": [], "restart": []}
    cfg.save(**changed)
    return {"saved": [_BY_KEY[k]["label"] for k in changed],
            "restart": [_BY_KEY[k]["label"] for k in changed if _BY_KEY[k].get("restart")]}


# ---- checks ----

def _run(argv: list[str], timeout: int = 30) -> tuple[int, str]:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace")
        return p.returncode, (p.stdout + p.stderr).strip()
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)


def check_project(repo: str, project_root: str, base_branch: str, run=_run) -> list[dict]:
    """[{ok, what, detail}] for the Settings page's Check button and for New project."""
    out = []
    repo = normalize_repo(repo)
    code, msg = run(["gh", "api", f"repos/{repo}", "--jq", ".full_name"])
    out.append({"ok": code == 0, "what": f"GitHub can see {repo}",
                "detail": "" if code == 0 else "Check the name, and that `gh auth login` is signed in to an account with access. " + msg[:160]})
    if code == 0 and base_branch:
        code, msg = run(["gh", "api", f"repos/{repo}/branches/{base_branch}", "--jq", ".name"])
        out.append({"ok": code == 0, "what": f"Branch {base_branch} exists",
                    "detail": "" if code == 0 else "Workers branch from it and open pull requests into it."})
    root = Path(project_root or "")
    if not project_root or not root.is_dir():
        out.append({"ok": False, "what": "Local clone found",
                    "detail": f"{project_root or '(empty)'} isn't a folder. Clone it first: git clone https://github.com/{repo}.git"})
    else:
        code, url = run(["git", "-C", str(root), "remote", "get-url", "origin"])
        same = code == 0 and repo.lower() in normalize_repo(url.replace("git@github.com:", "https://github.com/")).lower()
        out.append({"ok": same, "what": "Local clone is that repository",
                    "detail": "" if same else f"{root} has origin {url or 'none'}; expected {repo}."})
    return out


def create_labels(repo: str, label: str, run=_run) -> list[dict]:
    """The task label and its status labels, made (or updated) on GitHub."""
    want = [(label, "1f6feb", "A task for Fletchery's workers"),
            (f"{label}:queued", "c5def5", "Fletchery: waiting for a worker"),
            (f"{label}:running", "fbca04", "Fletchery: a worker is on it"),
            (f"{label}:pr", "0e8a16", "Fletchery: pull request open"),
            (f"{label}:blocked", "d73a4a", "Fletchery: needs you"),
            (f"{label}-status", "ededed", "Fletchery's status issue")]
    out = []
    for name, color, desc in want:
        code, msg = run(["gh", "label", "create", name, "--repo", normalize_repo(repo), "--color", color,
                         "--description", desc, "--force"])
        out.append({"ok": code == 0, "what": f"Label {name}", "detail": "" if code == 0 else msg[:160]})
    return out


def safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-") or "project"


def new_project(data: dict, current: Config | None = None, run=_run) -> dict:
    """Write a new profile in the Projects folder. Copies the current project's agents when asked."""
    name = str(data.get("name", "")).strip()
    if not name:
        raise ValueError("Give the project a name.")
    repo = _coerce(_BY_KEY["repo"], data.get("repo", ""))
    label = _coerce(_BY_KEY["label"], data.get("label") or "fletchery")
    base = _coerce(_BY_KEY["base_branch"], data.get("base_branch") or "main")
    root = str(data.get("project_root", "")).strip()
    path = projects_dir() / f"{safe_name(name)}.profile.json"
    if path.exists():
        raise ValueError(f"There is already a project file {path.name}. Pick another name.")
    workers = {"enabled": True, "projectRoot": root, "baseBranch": base, "label": label, "maxParallel": 1,
               "dailyBudgetUsd": 20}
    if data.get("copy_agents") and current is not None and current.agents:
        workers["agents"] = current.agents
        if current.routing:
            workers["routing"] = current.routing
    profile = {"name": name, "repo": repo, "workers": workers}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")
    checks = check_project(repo, root, base, run=run)
    if data.get("create_labels") and checks and checks[0]["ok"]:
        checks += create_labels(repo, label, run=run)
    return {"path": str(path), "name": name, "checks": checks, "state_dir": default_state_dir(name)}
