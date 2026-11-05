"""Coding agents decisions knows how to pick back up.

Each agent says how `decide create` recognizes a session of it (an env var the agent sets for the
commands it runs), how the resumer continues that session headlessly, how a person resumes it by
hand, and where it reads skills from. Any other agent works too: set DECISIONS_AGENT and
DECISIONS_SESSION_ID (or pass --agent/--session), and give the resumer a command for it with
`--agent-command NAME=CMD`.
"""
from __future__ import annotations

import os
import shlex
from dataclasses import dataclass


@dataclass(frozen=True)
class Agent:
    name: str
    binary: str
    session_env: str        # set by the agent in the environment of the commands it runs
    resume_command: str     # headless; gets $RESUME_SESSION_ID, $RESUME_PROMPT, $RESUME_CWD, $DECISION_*
    manual_command: str     # what a person pastes into a terminal; {session} is shell-quoted
    skills_dir: str


AGENTS = {a.name: a for a in [
    Agent("claude-code", "claude", "CLAUDE_CODE_SESSION_ID",
          'claude --resume "$RESUME_SESSION_ID" -p "$RESUME_PROMPT" '
          '--permission-mode acceptEdits --allowedTools "Bash(decide:*)"',
          "claude --resume {session}", "~/.claude/skills"),
    # Workspace-write sandbox, plus network so `decide` can reach the server.
    Agent("codex", "codex", "CODEX_THREAD_ID",
          'codex exec resume -c sandbox_mode=workspace-write -c sandbox_workspace_write.network_access=true '
          '"$RESUME_SESSION_ID" "$RESUME_PROMPT"',
          "codex resume {session}", "~/.agents/skills"),
]}

# The cross-agent skills location, for agents not listed above.
SHARED_SKILLS_DIR = "~/.agents/skills"


def detect_session() -> tuple[str, str] | None:
    """(agent, session_id) of the agent session this process runs in, if any."""
    if sid := os.environ.get("DECISIONS_SESSION_ID"):
        return os.environ.get("DECISIONS_AGENT") or "agent", sid
    for a in AGENTS.values():
        if sid := os.environ.get(a.session_env):
            return a.name, sid
    return None


def detect_agent() -> str | None:
    return os.environ.get("DECISIONS_AGENT") or (s[0] if (s := detect_session()) else None)


def resume_command(agent: str, overrides: dict[str, str] | None = None) -> str | None:
    if overrides and agent in overrides:
        return overrides[agent]
    return AGENTS[agent].resume_command if agent in AGENTS else None


def manual_command(resume: dict | None) -> str | None:
    """`cd <cwd> && <agent> resume <session>`, for the GUI's "Copy resume command"."""
    if not resume:
        return None
    a = AGENTS.get(resume["agent"])
    if not a:
        return None
    cmd = a.manual_command.format(session=shlex.quote(resume["session_id"]))
    return f"cd {shlex.quote(resume['cwd'])} && {cmd}" if resume.get("cwd") else cmd
