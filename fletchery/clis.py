"""Runtimes for coding agents other than Claude: OpenAI's Codex CLI and OpenCode (which reaches DeepSeek, Kimi, GLM,
Qwen and others, e.g. on an OpenCode Go plan). Each runs one headless session in the task's worktree, streams its tool
calls to the live view, and returns the same `RunResult` the Claude runtime does, so the pipeline cannot tell them apart.

Both run on their own sign-in (ChatGPT for Codex, OpenCode's `auth login` for OpenCode); Fletchery passes no keys.
Cost is reported when the CLI reports it (OpenCode does, Codex on a ChatGPT plan does not), else 0.

What each role may do, as with Claude (runtime.TOOLS):
- worker, fixer: edit files in the worktree and run commands; no web.
- reviewer, planner: read only.
Codex workers use workspace-write and reviewers/planners read-only. Merger sessions use danger-full-access with
approval_policy=never for native builds, shared inputs and GitHub. OpenCode uses a role-specific permission
config, passed in OPENCODE_CONFIG_CONTENT. Neither may write the repository's .git from inside the sandbox on every
platform, so the role prompts say a failed `git commit` is fine: Fletchery commits what is left (pipeline.py).
"""
from __future__ import annotations

import asyncio
import copy
import glob
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable

from runtime import SCHEMAS, RunResult, Runtime, is_limit, is_signed_out, parse_output

DEFAULT_TIMEOUT_MIN = {"worker": 60, "fixer": 45, "reviewer": 20, "planner": 20, "merger": 720}
READ_ONLY = ("reviewer", "planner")
GIT_NOTE = ("\n\n(If `git add` or `git commit` is refused by your sandbox, leave the changes in the working tree: "
            "Fletchery commits them after you finish. Do not try to work around the sandbox.)")


def strict_schema(schema: dict) -> dict:
    """OpenAI structured outputs want every property required and no extras; optional ones become nullable."""
    s = copy.deepcopy(schema)

    def fix(node: Any) -> None:
        if not isinstance(node, dict):
            return
        if node.get("type") == "object" and "properties" in node:
            req = set(node.get("required", []))
            for k, v in node["properties"].items():
                fix(v)
                if k not in req:
                    t = v.get("type")
                    if isinstance(t, str):
                        v["type"] = [t, "null"]
            node["required"] = list(node["properties"])
            node["additionalProperties"] = False
        if node.get("type") == "array" or (isinstance(node.get("type"), list) and "array" in node["type"]):
            fix(node.get("items"))

    fix(s)
    return s


def drop_nulls(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


def _newest(pattern: str) -> str:
    hits = sorted(glob.glob(pattern), key=lambda p: os.path.getmtime(p), reverse=True)
    return hits[0] if hits else ""


def find_codex(explicit: str = "") -> str:
    """The Codex CLI. On Windows the Codex app bundles it under %LOCALAPPDATA%\\OpenAI\\Codex\\bin\\<build>\\, a folder
    that changes with every app update, so the newest one wins."""
    if explicit:
        return explicit
    for name in ("codex.exe", "codex"):
        p = shutil.which(name)
        if p and not p.lower().endswith((".ps1", ".cmd")):
            return p
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        p = _newest(os.path.join(local, "OpenAI", "Codex", "bin", "*", "codex.exe"))
        if p:
            return p
    return shutil.which("codex") or "codex"


_models_cache: dict = {}


def opencode_models(command: str = "", max_age: float = 600) -> list[str]:
    """`opencode models`: every provider/model OpenCode can reach right now. [] if it can't be asked."""
    import subprocess
    import time
    hit = _models_cache.get(command)
    if hit and time.time() - hit[0] < max_age:
        return hit[1]
    try:
        p = subprocess.run([find_opencode(command), "models"], capture_output=True, text=True, timeout=60,
                           encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return []
    out = re.sub(r"\x1b\[[0-9;]*m", "", p.stdout or "")
    models = [l.strip() for l in out.splitlines() if re.match(r"^[\w.\-]+/[\w.:\-]+$", l.strip())]
    if models:
        _models_cache[command] = (time.time(), models)
    return models


def find_opencode(explicit: str = "") -> str:
    """OpenCode's own binary, not npm's .cmd/.ps1 shims (cmd.exe would re-parse the arguments)."""
    if explicit:
        return explicit
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        p = os.path.join(appdata, "npm", "node_modules", "@opencode", "cli", "bin", "opencode.exe")
        if os.path.exists(p):
            return p
    for name in ("opencode.exe", "opencode"):
        p = shutil.which(name)
        if p and not p.lower().endswith((".ps1", ".cmd")):
            return p
    return shutil.which("opencode") or "opencode"


def short_command(cmd: str) -> str:
    """Codex on Windows wraps every command in `powershell.exe -Command "..."`: show what ran inside."""
    m = re.match(r'^\s*"?[^"]*powershell(\.exe)?"?\s+(-NoProfile\s+)?-Command\s+(.*)$', cmd, re.IGNORECASE | re.DOTALL)
    inner = m.group(3) if m else cmd
    inner = inner.strip()
    if len(inner) >= 2 and inner[0] == inner[-1] and inner[0] in "'\"":
        inner = inner[1:-1]
    return " ".join(inner.split())


class CliRuntime(Runtime):
    """One CLI process per session. Subclasses build the command and read its JSON lines."""

    kind = "cli"

    def __init__(self, spec, env: dict[str, str] | None = None, low_priority: bool = True):
        self.spec = spec
        self.env = env or {}
        self.low_priority = low_priority

    # subclasses
    def command(self, role: str, prompt_file: Path, cwd: Path, workdir: Path) -> tuple[list[str], str | None, dict]:
        """(argv, text for stdin or None, extra env)."""
        raise NotImplementedError

    def on_event(self, ev: dict, state: dict, cwd: str, on_action) -> None:
        raise NotImplementedError

    def finish(self, state: dict, workdir: Path) -> str:
        """The session's final answer text."""
        return state.get("last_text", "")

    async def run(self, role, prompt, cwd, budget_usd, max_turns, on_action=None):
        result = RunResult(role, False)
        workdir = Path(tempfile.mkdtemp(prefix=f"fletchery-{self.kind}-"))
        prompt_file = workdir / "prompt.md"
        prompt_file.write_text(prompt + (GIT_NOTE if role in ("worker", "fixer") else ""), encoding="utf-8")
        argv, stdin_text, extra_env = self.command(role, prompt_file, Path(cwd), workdir)
        minutes = (self.spec.timeout_minutes or {}).get(role) or DEFAULT_TIMEOUT_MIN.get(role, 30)
        import keys
        env = {**os.environ, "GIT_LFS_SKIP_SMUDGE": "1", **self.env, **keys.env_for(self.spec.key_env), **extra_env}
        if self.spec.auth == "key":  # run on the stored API key instead of the CLI's plan sign-in
            name = self.spec.api_key_name
            value = keys.get(name) if name else None
            if not value:
                result.error = f"{self.spec.name} is set to run on an API key, but {name or 'no key'} is not stored"
                result.signed_out = True
                return result
            env[name] = value
            if self.kind == "codex":
                env["CODEX_API_KEY"] = value  # what `codex exec` reads for key sign-in
        env.pop("ANTHROPIC_API_KEY", None)  # other vendors never need it; keep it out of their sessions
        flags = 0x00004000 if os.name == "nt" and self.low_priority else 0  # BELOW_NORMAL_PRIORITY_CLASS
        state: dict = {"last_text": "", "errors": [], "cost": 0.0, "turns": 0, "session": ""}
        stderr_tail: list[str] = []
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=str(cwd), env=env, stdin=asyncio.subprocess.PIPE if stdin_text is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=16 * 1024 * 1024,
                **({"creationflags": flags} if flags else {}))
        except (OSError, ValueError) as e:
            result.error = f"could not start {self.kind} ({argv[0]}): {e}"
            return result

        async def feed():
            if stdin_text is not None and proc.stdin:
                proc.stdin.write(stdin_text.encode("utf-8"))
                await proc.stdin.drain()
                proc.stdin.close()

        async def read_out():
            assert proc.stdout
            async for raw in proc.stdout:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("{"):
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                try:
                    self.on_event(ev, state, str(cwd), on_action)
                except Exception:
                    pass  # an event shape we do not know must never break a session

        async def read_err():
            assert proc.stderr
            async for raw in proc.stderr:
                stderr_tail.append(raw.decode("utf-8", errors="replace").rstrip()[:500])
                del stderr_tail[:-40]

        try:
            await asyncio.wait_for(asyncio.gather(feed(), read_out(), read_err(), proc.wait()), timeout=minutes * 60)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            result.error = f"{self.kind} session ran over {minutes} minutes and was stopped"
            return result
        finally:
            result.cost_usd = round(state["cost"], 4)
            result.turns = state["turns"]
            result.session_id = state["session"]

        text = self.finish(state, workdir)
        result.text = text
        errs = "; ".join(state["errors"])
        if proc.returncode != 0 or (state["errors"] and not text):
            detail = errs or "\n".join(stderr_tail[-5:]) or f"exit code {proc.returncode}"
            result.error = f"{self.kind} failed: {detail[:600]}"
            blob = errs + "\n" + "\n".join(stderr_tail)
            result.signed_out = is_signed_out(blob) or bool(_CLI_SIGNED_OUT.search(blob))
            result.limited = not result.signed_out and is_limit(blob)
            return result
        out = parse_output(None, text)
        result.output = drop_nulls(out) if out else {}
        result.ok = bool(result.output)
        if not result.ok:
            result.error = f"no JSON answer from {self.kind}: {text[-300:]!r}"
        return result


_CLI_SIGNED_OUT = re.compile(r"codex login|log ?in again|refresh token|token (has )?expired|opencode auth login|"
                             r"no (credentials|api key) (found|configured)|unauthori[sz]ed", re.IGNORECASE)


class CodexRuntime(CliRuntime):
    """`codex exec --json`: JSON lines on stdout (thread.started, item.started/completed, turn.completed, error)."""

    kind = "codex"

    def command(self, role, prompt_file, cwd, workdir):
        schema = workdir / "schema.json"
        schema.write_text(json.dumps(strict_schema(SCHEMAS[role])), encoding="utf-8")
        argv = [find_codex(self.spec.command), "exec", "--json", "--skip-git-repo-check",
                "--output-schema", str(schema), "-o", str(workdir / "last.txt"), "-C", str(cwd),
                "-s", "danger-full-access" if role == "merger" else "read-only" if role in READ_ONLY else "workspace-write"]
        if role == "merger":
            argv += ["-c", 'approval_policy="never"']
        if self.spec.model:
            argv += ["-m", self.spec.model]
        argv.append("-")  # the prompt comes on stdin: a long prompt does not fit a Windows command line
        return argv, prompt_file.read_text(encoding="utf-8"), {}

    def on_event(self, ev, state, cwd, on_action):
        t = ev.get("type", "")
        if t == "thread.started":
            state["session"] = ev.get("thread_id", "")
        elif t == "turn.completed":
            state["turns"] += 1
        elif t in ("error", "turn.failed"):
            msg = ev.get("message") or (ev.get("error") or {}).get("message") or json.dumps(ev)[:300]
            state["errors"].append(str(msg))
        elif t == "item.completed":
            item = ev.get("item") or {}
            it = item.get("type")
            if it == "agent_message":
                state["last_text"] = item.get("text", "")
            elif it == "error":  # warnings such as missing model metadata; not fatal
                pass
            elif on_action and it == "command_execution":
                on_action(f"$ {short_command(item.get('command', ''))[:120]}")
            elif on_action and it == "file_change":
                for c in item.get("changes") or []:
                    on_action(f"Edit {_rel(c.get('path', ''), cwd)}")
            elif on_action and it in ("mcp_tool_call", "web_search"):
                on_action(it)

    def finish(self, state, workdir):
        last = workdir / "last.txt"
        if last.exists():
            text = last.read_text(encoding="utf-8", errors="replace").strip()
            if text:
                return text
        return state.get("last_text", "")


# OpenCode permissions per role. Everything not named keeps OpenCode's default (read, grep, glob, list: allowed).
OPENCODE_PERMISSIONS = {
    "merge": {"edit": "allow", "bash": "allow", "webfetch": "deny", "websearch": "deny", "external_directory": "allow"},
    "write": {"edit": "allow", "bash": "allow", "webfetch": "deny", "websearch": "deny", "external_directory": "deny"},
    "read": {"edit": "deny", "webfetch": "deny", "websearch": "deny", "external_directory": "deny",
             "bash": {"*": "deny", "git diff*": "allow", "git log*": "allow", "git show*": "allow",
                      "git status*": "allow", "cat *": "allow", "grep *": "allow", "rg *": "allow", "ls*": "allow"}},
}


RESUME_MESSAGE = ("You stopped before the end. Carry on with the task from where you are, finish every step it asks "
                  "for, and end with the JSON answer it asks for.")


class OpenCodeRuntime(CliRuntime):
    """`opencode run --format json`: JSON lines with a `type` (text, tool_use, step_finish, error) and a `part`."""

    kind = "opencode"

    def command(self, role, prompt_file, cwd, workdir):
        perms = OPENCODE_PERMISSIONS["merge" if role == "merger" else "read" if role in READ_ONLY else "write"]
        config = {"$schema": "https://opencode.ai/config.json", "permission": perms, "share": "disabled",
                  "autoupdate": False}
        # --standalone: a private server, never the OpenCode app's background service. On the service a session
        # outlived Fletchery's CLI and kept working in a worktree Fletchery had moved on from (LifeShare, 2026-11-04).
        resume = getattr(self, "resume_session", "")
        argv = [find_opencode(self.spec.command), "run", "--standalone", "--format", "json"]
        if resume:
            argv += ["-s", resume]
        else:
            argv += ["--title", f"Fletchery {role}", "-f", str(prompt_file)]
        if self.spec.model:
            argv += ["-m", self.spec.model]
        argv.append(RESUME_MESSAGE if resume else
                    "Do the task in the attached prompt.md exactly as it says, and answer with the JSON it asks for.")
        return argv, None, {"OPENCODE_CONFIG_CONTENT": json.dumps(config)}

    def on_event(self, ev, state, cwd, on_action):
        t = ev.get("type", "")
        part = ev.get("part") or {}
        if ev.get("sessionID"):
            state["session"] = ev["sessionID"]
        if t == "text":
            text = part.get("text", "")
            if text.strip():
                state["last_text"] = text
        elif t in ("step_finish", "step-finish"):
            state["turns"] += 1
            try:
                state["cost"] += float(part.get("cost") or 0)
            except (TypeError, ValueError):
                pass
        elif t == "error":
            err = ev.get("error") or {}
            msg = (err.get("data") or {}).get("message") if isinstance(err, dict) else None
            state["errors"].append(str(msg or err or ev)[:400])
        elif t in ("tool_use", "tool") and on_action:
            tool = str(part.get("tool", "tool"))
            inp = ((part.get("state") or {}).get("input")) or {}
            path = inp.get("filePath") or inp.get("path") or ""
            if tool == "bash":
                on_action(f"$ {' '.join(str(inp.get('command', '')).split())[:120]}")
            elif tool in ("edit", "write", "patch", "multiedit"):
                on_action(f"Edit {_rel(path, cwd)}")
            elif tool == "read":
                on_action(f"Read {_rel(path, cwd)}")
            elif tool in ("grep", "glob"):
                on_action(f"{tool.capitalize()} {inp.get('pattern', '')}"[:120])
            else:
                on_action(tool)


def _rel(p: str, root: str) -> str:
    p = str(p).replace("\\", "/")
    r = str(root).replace("\\", "/").rstrip("/") + "/"
    return p[len(r):] if p.lower().startswith(r.lower()) else p


LOGIN = {"claude": ["claude", "/login"], "codex": ["codex", "login"], "opencode": ["opencode", "auth", "login"]}


def open_login(spec) -> str:
    """Open a console window on this PC running the agent's own sign-in, for the dashboard's Sign in button."""
    import subprocess
    if os.name != "nt":
        raise ValueError("Sign in from a terminal: " + " ".join(LOGIN.get(spec.kind, [spec.kind])))
    if spec.kind == "claude":
        argv = [shutil.which("claude") or "claude"]  # then type /login
    elif spec.kind == "codex":
        argv = [find_codex(spec.command), "login"]
    elif spec.kind == "opencode":
        argv = [find_opencode(spec.command), "auth", "login"]
    else:
        raise ValueError(f"{spec.kind} has no sign-in")
    subprocess.Popen(argv, creationflags=0x00000010)  # CREATE_NEW_CONSOLE
    return "A sign-in window opened" + (": type /login there." if spec.kind == "claude" else ".")


def make_runtime(spec, cfg, repo_root: Path, plugin_path: Path | None, fake: Runtime | None = None) -> Runtime:
    """The runtime for one agent of the profile."""
    if fake is not None or spec.kind == "fake":
        return fake
    if spec.kind == "codex":
        return CodexRuntime(spec)
    if spec.kind == "opencode":
        return OpenCodeRuntime(spec)
    if spec.kind == "claude":
        from runtime import AgentSdkRuntime
        return AgentSdkRuntime(repo_root, spec.model or cfg.model or None, plugin_path=plugin_path,
                               auth="api" if spec.auth == "key" else "subscription")
    raise ValueError(f'agent "{spec.name}": unknown kind "{spec.kind}" (claude, codex or opencode)')


UNREACHABLE_RE = re.compile(r"provider\.no-route|model unavailable|model[^\n]{0,40}not (found|available|supported)|"
                            r"unknown model|no such model|ProviderModelNotFound", re.IGNORECASE)


def unreachable(error: str) -> bool:
    """The agent's model can't be reached at all (not a limit, not the task): another agent should take the work."""
    return bool(error and UNREACHABLE_RE.search(error))


def check(spec, auth_mode: str = "subscription") -> tuple[bool, str]:
    """Is this agent's CLI there and signed in? Quick, no session."""
    import subprocess
    import keys
    stored = keys.env_for(spec.key_env)
    if spec.kind == "claude":
        import auth
        return auth.check("api" if spec.auth == "key" else "subscription")
    if spec.auth == "key":
        name = spec.api_key_name
        if not name:
            return False, "No API key is known for this model's provider: add one to the agent's keys."
        return (True, f"Runs on the API key {name}") if keys.get(name) else (False, f"Runs on an API key, but {name} is not stored.")
    if stored and spec.kind == "opencode":
        known = opencode_models(spec.command)
        if known and spec.model and spec.model not in known:
            return False, f"OpenCode doesn't offer {spec.model} right now (`opencode models`): pick another model."
        return True, "Using the stored key " + ", ".join(stored)
    try:
        if spec.kind == "codex":
            if stored:
                return True, "Using the stored key " + ", ".join(stored)
            p = subprocess.run([find_codex(spec.command), "login", "status"], capture_output=True, text=True, timeout=30,
                               encoding="utf-8", errors="replace")
            msg = (p.stdout + p.stderr).strip().splitlines()
            ok = p.returncode == 0 and any("logged in" in l.lower() for l in msg)
            line = next((l for l in msg if "logged in" in l.lower()), msg[-1] if msg else "no answer")
            return ok, line if ok else "Codex is not signed in: run `codex login`."
        if spec.kind == "opencode":
            p = subprocess.run([find_opencode(spec.command), "auth", "list"], capture_output=True, text=True, timeout=30,
                               encoding="utf-8", errors="replace")
            out = re.sub(r"\x1b\[[0-9;]*m", "", p.stdout + p.stderr)
            if p.returncode != 0:
                return False, f"OpenCode: {out.strip()[:200]}"
            provider = (spec.model or "").split("/")[0]
            # `auth list` names providers by their display name ("OpenCode Go"); compare without spaces and dashes
            flat = re.sub(r"[\s\-_]", "", out.lower())
            if provider and re.sub(r"[\-_]", "", provider.lower()) not in flat:
                return False, f"OpenCode has no sign-in for {provider}: run `opencode auth login`, or store its key."
            known = opencode_models(spec.command)
            if known and spec.model and spec.model not in known:  # signed in, but this model isn't offered (any more)
                return False, f"OpenCode doesn't offer {spec.model} right now (`opencode models`): pick another model."
            return True, f"OpenCode signed in ({provider or 'default provider'})"
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"{spec.kind} CLI not found or not answering: {e}"
    return True, ""
