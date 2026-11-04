# Blender work in unattended sessions

Fletchery includes the Blender MCP requirement in every worker, fixer, reviewer,
planner and merger prompt. It applies to Claude, Codex and OpenCode. Role prompts
are read for each new session; existing sessions keep their original instructions.
Fletchery watches installed role files and queues a graceful reload when they change:
active merges and workers finish first, then the app reloads automatically. A missing
connection or a role permission denial must be reported as a concrete blocker, with
the attempted connection and error.

# Blender: official Blender Lab MCP required

For all Blender work (scene inspection, modeling, materials, renders, screenshots and
validation), use the official Blender Lab MCP. Never substitute desktop/remote control
or a direct Blender script launch. Repair an unavailable MCP connection or report the
specific blocker; do not silently fall back. Bridge installation/startup is the only
bootstrap exception. `execute_blender_code_for_cli` may run isolated background jobs
through MCP. Read-only roles may inspect through MCP but must not mutate scenes.

Canonical installation: `E:/BlenderTools/BlenderMCP`. Stdio client:
`E:/BlenderTools/BlenderMCP/.venv/Scripts/python.exe -m blmcp`.
Default bridge: `127.0.0.1:9876`. If no bridge is listening, use
`E:/BlenderTools/BlenderMCP/Start-BlenderMCP.ps1`. Inspect the connected file and scene
through MCP before edits. Preserve other agents' scenes/assets and coordinate ownership
before switching a shared scene. Concurrent projects need isolated bridge ports and an
explicit `BLENDER_MCP_PORT`; never repoint another agent's active scene.

If this unattended session has no native MCP tool surface, the installed
`E:/BlenderTools/BlenderMCP/call.py` is a stdio MCP client, not a direct Blender launcher.
Run it with the canonical venv Python, `--port <owned-port> --tool <tool-name>` and
`--arguments <JSON>` (or `--code-file <file>`). Begin with
`get_blendfile_summary_path_info` and `get_objects_summary`. This remains subject to
this role's permissions: an access denial is a blocker, never permission to bypass it.

## Check the connection without editing a scene

Use the native MCP tools when available. For CLI sessions, the local helper connects
to the same official stdio server. For example, in PowerShell:

```powershell
& E:/BlenderTools/BlenderMCP/.venv/Scripts/python.exe E:/BlenderTools/BlenderMCP/call.py --port 9876 --tool get_blendfile_summary_path_info
& E:/BlenderTools/BlenderMCP/.venv/Scripts/python.exe E:/BlenderTools/BlenderMCP/call.py --port 9876 --tool get_objects_summary
```

These are inspection calls. Do not open a project or edit a scene until its ownership
is established. Consult `sessions.json` in the canonical installation before selecting
an isolated port; a listening bridge may belong to another project. Starting a bridge
is allowed only when the chosen port has no owner/listener. Do not stop a bridge owned
by another agent. A background bridge supports scene queries and renders; an interactive
screenshot requires an owned interactive Blender session.

The profile's agent settings do not imply that the unattended runtime inherits the
desktop client's MCP tools. Claude sessions isolate their settings, and CLI permissions
may limit external access. Use the official stdio client where permitted and verify
the response; do not claim a connection from configuration alone.
