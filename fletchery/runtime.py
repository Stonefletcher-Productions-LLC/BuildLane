"""Where a Claude session runs. `AgentSdkRuntime` runs it as a local Claude Code process through the Claude Agent SDK;
`FakeRuntime` answers from a script (tests, `--dry-run`). The loop and pipeline only see `Runtime.run`.

Swapping in Claude Managed Agents (cloud sandboxes) later means one more class here; nothing else changes.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

ROLES_DIR = Path(__file__).parent / "roles"

# What each role may do. `allowed` is auto-approved; `disallowed` cannot be used at all.
TOOLS: dict[str, dict[str, list[str]]] = {
    "worker": {
        "allowed": ["Bash(git:*)", "Bash(gh:*)", "Bash(node:*)", "Bash(python3:*)", "Bash(python:*)", "Bash(ls:*)",
                    "Bash(cat:*)", "Bash(grep:*)", "Bash(rg:*)", "Read", "Edit", "Write", "Glob", "Grep"],
        "disallowed": ["WebSearch", "WebFetch", "Agent"],
    },
    "fixer": {
        "allowed": ["Bash(git:*)", "Bash(gh:*)", "Bash(node:*)", "Bash(python3:*)", "Bash(python:*)", "Bash(ls:*)",
                    "Bash(cat:*)", "Bash(grep:*)", "Bash(rg:*)", "Read", "Edit", "Write", "Glob", "Grep"],
        "disallowed": ["WebSearch", "WebFetch", "Agent"],
    },
    "reviewer": {
        "allowed": ["Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)", "Bash(cat:*)", "Bash(grep:*)",
                    "Bash(rg:*)", "Read", "Glob", "Grep"],
        "disallowed": ["Edit", "Write", "WebSearch", "WebFetch", "Agent", "Bash(git push:*)", "Bash(git commit:*)"],
    },
    # the merge round (rounds.py): merges, builds, runs the project's tools and lands the result on the base branch
    "merger": {
        "allowed": ["Bash", "Read", "Edit", "Write", "Glob", "Grep"],
        "disallowed": ["WebSearch", "WebFetch", "Agent"],
    },
    "planner": {
        "allowed": ["Bash(gh issue view:*)", "Bash(gh pr view:*)", "Bash(gh search issues:*)",
                    "Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)", "Bash(git status:*)",
                    "Bash(cat:*)", "Bash(grep:*)", "Bash(rg:*)", "Read", "Glob", "Grep"],
        "disallowed": ["Edit", "Write", "WebSearch", "WebFetch", "Agent"],
    },
}

# The fixed shapes each role answers in. The SDK asks the model for exactly this JSON (output_format).
SCHEMAS: dict[str, dict[str, Any]] = {
    "worker": {"type": "object", "required": ["summary", "files", "build_steps", "checks", "unverified"],
               "properties": {"summary": {"type": "string"}, "files": {"type": "array", "items": {"type": "string"}},
                              "build_steps": {"type": "array", "items": {"type": "string"}},
                              "checks": {"type": "array", "items": {"type": "string"}},
                              "save_version": {"type": "string"},
                              "likely_conflicts": {"type": "array", "items": {"type": "string"}},
                              "unverified": {"type": "array", "items": {"type": "string"}},
                              "gave_up": {"type": "string"}}},
    "fixer": {"type": "object", "required": ["summary", "pushed"],
              "properties": {"summary": {"type": "string"}, "pushed": {"type": "boolean"}, "gave_up": {"type": "string"}}},
    "reviewer": {"type": "object", "required": ["verdict", "findings"],
                 "properties": {"verdict": {"type": "string", "enum": ["approve", "needs_work", "escalate"]},
                                "findings": {"type": "array", "items": {"type": "object",
                                             "required": ["file", "line", "rule", "note"],
                                             "properties": {"file": {"type": "string"}, "line": {"type": "integer"},
                                                            "rule": {"type": "string"}, "note": {"type": "string"}}}},
                                "praise": {"type": "string"}}},
    "merger": {"type": "object", "required": ["state", "landed_pr", "merged", "left_out", "summary"],
               "properties": {"state": {"type": "string", "enum": ["landed", "nothing", "failed", "interrupted"]},
                              "landed_pr": {"type": "integer"},
                              "merged": {"type": "array", "items": {"type": "integer"}},
                              "left_out": {"type": "array", "items": {"type": "object", "required": ["pr", "why"],
                                           "properties": {"pr": {"type": "integer"}, "why": {"type": "string"},
                                                          "disposition": {"type": "string", "enum": ["failed", "deferred"]}}}},
                              "deferred": {"type": "array", "items": {"type": "object", "required": ["pr", "why"],
                                           "properties": {"pr": {"type": "integer"}, "why": {"type": "string"},
                                                          "disposition": {"type": "string", "enum": ["failed", "deferred"]}}}},
                              "checkpoint": {"type": "object", "properties": {"worktree": {"type": "string"},
                                             "branch": {"type": "string"}, "head": {"type": "string"}}},
                              "summary": {"type": "string"}, "gave_up": {"type": "string"},
                              "stop_reason": {"type": "string"}}},
    "planner": {"type": "object", "required": ["issues"],
                "properties": {"issues": {"type": "array", "items": {"type": "object",
                               "required": ["title", "body", "depends_on"],
                               "properties": {"title": {"type": "string"}, "body": {"type": "string"},
                                              "depends_on": {"type": "array", "items": {"type": "integer"}}}}}}},
}

# Resource contention is explicit, never inferred from a permission/error string.
for _role in ("worker", "fixer"):
    SCHEMAS[_role]["properties"]["waiting_resource"] = {"type": "string", "enum": ["", "heavy"]}

# Blocker planning uses the read-only planner, with an explicit human-decision escape hatch.
SCHEMAS["planner"]["properties"].update({
    "retry": {"type": "string"},
    "needs_human": {"type": "string"},
    "reuse": {"type": "array", "items": {"type": "integer"}},
})


@dataclass
class RunResult:
    role: str
    ok: bool
    output: dict[str, Any] = field(default_factory=dict)
    text: str = ""
    cost_usd: float = 0.0
    turns: int = 0
    session_id: str = ""
    error: str = ""
    limited: bool = False  # hit a usage or rate limit: not a verdict on the task, the loop pauses instead
    signed_out: bool = False  # the plan sign-in expired or the key was refused: pause until someone signs in again
    agent: str = ""           # which of the project's agents ran it (set by the pipeline)


_LIMIT = re.compile(r"usage limit|rate.?limit|limit reached|limit will reset|resets at|overloaded|\b429\b|\b529\b",
                    re.IGNORECASE)


_SIGNED_OUT = re.compile(r"failed to authenticate|oauth (session|token).{0,40}(expired|invalid|revoked)|not logged in|"
                         r"invalid (x-)?api.?key|authentication_error|\b401\b|please run /login", re.IGNORECASE)


def is_signed_out(text: str, status: int | None = None) -> bool:
    return status == 401 or bool(_SIGNED_OUT.search(text or ""))


def is_limit(text: str, status: int | None = None) -> bool:
    return status in (429, 529) or bool(_LIMIT.search(text or ""))


class Runtime:
    async def run(self, role: str, prompt: str, cwd: Path, budget_usd: float, max_turns: int,
                  on_action: Callable[[str], None] | None = None) -> RunResult:
        """`on_action` gets one line per tool call the session makes (the dashboard's live view)."""
        raise NotImplementedError


PYTHON = "python" if os.name == "nt" else "python3"  # Windows: `python3` is the Microsoft Store placeholder


def role_prompt(role: str, rules=None, **fields: str) -> str:
    """The role's prompt with {placeholders} filled: the task fields, plus the project's conventions, its additions for
    this role and the checks it will run (rules.py). Braces in the files are literal unless they name a field.
    `python3 ` becomes `python ` on Windows, where python3 is not a real interpreter."""
    text = (ROLES_DIR / f"{role}.md").read_text(encoding="utf-8")
    # Keep this contract with the loaded schema, not in hot-read role files: a
    # running backend may finish a merge round while a new bundle is staged.
    if role in ("worker", "fixer"):
        text += ("\nIf another job holds the heavy-work lease, stop cleanly and return "
                 "`waiting_resource: \"heavy\"` without `gave_up`. Preserve the checkout and assets. "
                 "This is only for actual reservation contention: permission denials, missing tools "
                 "and build failures use the normal failure report. Never release another job's lease. "
                 "The controller waits for readiness before resuming.\n")
    conventions = (rules.conventions.strip() if rules and rules.conventions else "")
    fields.setdefault("conventions", "# The project's conventions\n\n" + conventions if conventions else "")
    extra = rules.additions.get(role, "").strip() if rules else ""
    fields.setdefault("additions", "# Notes from the project\n\n" + extra if extra else "")
    checks = fields.pop("checks_list", None)
    if checks is None and rules is not None:
        checks = rules.always + [c for _, runs in rules.by_path for c in runs]
    fields.setdefault("checks", "\n".join(f"   - `{c}`" for c in (checks or [])) or "   - (the project lists none)")
    fields.setdefault("project", "this project")
    fields.setdefault("freeze", "")
    for k, v in fields.items():
        text = text.replace("{" + k + "}", v)
    if PYTHON != "python3":
        text = re.sub(r"\bpython3 ", PYTHON + " ", text)
    return re.sub(r"\n{3,}", "\n\n", text)


class AgentSdkRuntime(Runtime):
    def __init__(self, repo_root: Path, model: str | None = None, env: dict[str, str] | None = None,
                 plugin_path: Path | None = None, auth: str = "subscription"):
        self.repo_root = repo_root
        self.model = model
        self.env = env or {}
        self.plugin_path = plugin_path
        self.auth = auth
        self.saved_key = os.environ.get("ANTHROPIC_API_KEY") or self.env.get("ANTHROPIC_API_KEY")
        self.set_auth(auth)

    def set_auth(self, auth: str) -> None:
        """subscription: no key anywhere a session can see it, so the CLI uses its claude.ai sign-in (the SDK merges
        `env` over the inherited environment, so the key leaves this process too). api: sessions get the key."""
        from auth import api_key
        self.auth = auth
        if auth == "subscription":
            os.environ.pop("ANTHROPIC_API_KEY", None)
            self.env.pop("ANTHROPIC_API_KEY", None)
        else:
            key = api_key(self.saved_key)
            if key:
                self.saved_key = key
                self.env["ANTHROPIC_API_KEY"] = key

    async def run(self, role, prompt, cwd, budget_usd, max_turns, on_action=None):
        from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, ToolUseBlock, query
        from live import describe

        tools = TOOLS[role]
        opts = ClaudeAgentOptions(
            cwd=str(cwd),
            model=self.model,
            permission_mode="acceptEdits",
            allowed_tools=tools["allowed"],
            disallowed_tools=tools["disallowed"],
            max_turns=max_turns,
            max_budget_usd=budget_usd,
            setting_sources=[],  # nothing from ~/.claude or the checkout: the prompt is the whole contract
            output_format={"type": "json_schema", "schema": SCHEMAS[role]},
            env={"GIT_LFS_SKIP_SMUDGE": "1", **self.env},
            plugins=[{"type": "local", "path": str(self.plugin_path)}] if self.plugin_path else [],
            # One tool result (a big log or diff read) can exceed the SDK's 1 MB default and kill the session with
            # CLIJSONDecodeError (LifeShare #929, 2026-11-06). The CLI runtimes already read 16 MB lines.
            max_buffer_size=16 * 1024 * 1024,
        )
        result = RunResult(role, False)
        try:
            async for msg in query(prompt=prompt, options=opts):
                if on_action and isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, ToolUseBlock):
                            try:
                                on_action(describe(block.name, block.input or {}, str(cwd)))
                            except Exception:
                                pass  # the live view must never break a session
                if isinstance(msg, ResultMessage):
                    result.cost_usd = msg.total_cost_usd or 0.0
                    result.turns = msg.num_turns or 0
                    result.session_id = msg.session_id or ""
                    result.text = msg.result or ""
                    if msg.is_error:
                        result.error = f"{msg.subtype}: {'; '.join(msg.errors or []) or msg.result or msg.terminal_reason}"
                        result.signed_out = is_signed_out(result.error, msg.api_error_status)
                        result.limited = not result.signed_out and is_limit(result.error, msg.api_error_status)
                    else:
                        result.output = parse_output(msg.structured_output, result.text)
                        result.ok = bool(result.output)
                        if not result.ok:
                            result.error = f"no structured answer ({msg.terminal_reason})"
        except Exception as e:  # a crashed CLI, a bad key: the task goes back to the queue, the loop goes on
            result.error = f"{type(e).__name__}: {e}"
            result.signed_out = is_signed_out(result.error)
            result.limited = not result.signed_out and is_limit(result.error)
        return result


def parse_output(structured: Any, text: str) -> dict[str, Any]:
    if isinstance(structured, dict):
        return structured
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return {}


class FakeRuntime(Runtime):
    """Answers each (role, task-number) from a script; records every call. Default answers are the happy path."""

    def __init__(self, script: dict[tuple[str, int], dict | Callable[[str], dict]] | None = None,
                 cost_usd: float = 1.0, on_run: Callable[[str, Path], None] | None = None,
                 delay_s: float = 0.0, actions: list[str] | None = None):
        self.script = script or {}
        self.cost_usd = cost_usd
        self.calls: list[tuple[str, int, str]] = []
        self.on_run = on_run
        self.delay_s = delay_s      # demo: make sessions take a while so the dashboard has something to show
        self.actions = actions or []

    async def run(self, role, prompt, cwd, budget_usd, max_turns, on_action=None):
        m = re.search(r"Issue #(\d+)", prompt)
        n = int(m.group(1)) if m else 0
        self.calls.append((role, n, prompt))
        if self.on_run:
            self.on_run(role, cwd)
        if on_action and self.actions:
            for a in self.actions:
                on_action(a)
                await asyncio.sleep(self.delay_s / max(1, len(self.actions)))
        elif self.delay_s:
            await asyncio.sleep(self.delay_s)
        await asyncio.sleep(0)
        answer = self.script.get((role, n), self.script.get((role, 0)))
        if callable(answer):
            answer = answer(prompt)
        if answer is None:
            answer = DEFAULT_ANSWERS[role]
        if "error" in answer:
            so = is_signed_out(answer["error"])
            return RunResult(role, False, error=answer["error"], cost_usd=self.cost_usd, signed_out=so,
                             limited=not so and is_limit(answer["error"]))
        return RunResult(role, True, dict(answer), cost_usd=self.cost_usd, turns=3, session_id=f"fake-{role}-{n}")


DEFAULT_ANSWERS: dict[str, dict] = {
    "worker": {"summary": "Did the task.", "files": ["Source/LSUI/Private/LSExample.cpp"],
               "build_steps": ["Tools\\Build.ps1"], "checks": ["RunSpecs LifeShare.UI.Example"], "unverified": ["compile"]},
    "fixer": {"summary": "Fixed it.", "pushed": True},
    "reviewer": {"verdict": "approve", "findings": []},
    "planner": {"issues": []},
    "merger": {"state": "nothing", "landed_pr": 0, "merged": [], "left_out": [], "summary": "Nothing to merge."},
}
