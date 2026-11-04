"""Explicit per-task asset authoring grants from the operator's project profile.
Never read permissions from a worker-controlled checkout or its issue text.
"""
from pathlib import Path,PurePosixPath
import re

def grant_for(grants,worktree_root,cwd):
    if not grants or not worktree_root:return None
    path=Path(cwd).resolve()
    if path.parent!=Path(worktree_root).resolve():return None
    match=re.fullmatch(r"([1-9][0-9]*)-[a-z0-9-]+",path.name)
    if not match:return None
    grant=grants.get(match.group(1)) if isinstance(grants,dict) else None
    if not isinstance(grant,dict) or not isinstance(grant.get('paths'),list) or not grant['paths']:return None
    for name in grant['paths']:
        if not isinstance(name,str) or not name or '\\' in name or ':' in name:return None
        if PurePosixPath(name).is_absolute() or '..' in PurePosixPath(name).parts:return None
        if any(c in name for c in '*?['):return None
    if grant.get('mode','author') not in ('author','delete'):return None
    return grant

def permitted(grant,path):
    if not grant or PurePosixPath(path).is_absolute() or '..' in PurePosixPath(path).parts:return False
    return any(path.startswith(prefix) if prefix.endswith('/') else path==prefix for prefix in grant['paths'])

def prompt_note(grant):
    if not grant:return ''
    return ("\n\n## Operator-authorized asset task\n"
            "The operator explicitly authorized this task's asset work in the project profile. "
            "This overrides generic text-only/binary-escalation wording for the following paths only: "
            + ', '.join(grant['paths']) + '. Mode: '+grant.get('mode','author')+'. '
            "Review correctness and evidence normally; binary file type alone does not require a human decision. "
            "Never change unrelated assets or remove a production replacement before validation. "
            "Use official Blender Lab MCP only. For owned scenes use the canonical call.py stdio helper "
            "and an explicit private bridge port; inspect before editing. Native import/runtime checks remain Build Lane gates. "
            "If a tool or execution policy refuses an action, report it; do not bypass it. "
            "Fletchery publishes authorized LFS outputs through the project's existing delivery helper.\n")
