"""Turn a tool call into one searchable line: the command, never its output."""

from __future__ import annotations

import json
import re

from .text import squash

# --- Tool calls ----------------------------------------------------------------
# Only the command and its short arguments are kept, never tool output and never
# file contents written by a tool (Write/Edit bodies, patches).
# This much of each command is stored and searched. Agents chain many steps into
# one command, so the part that answers "which command did we use" is often
# several hundred characters in; what lies beyond this limit is mostly the body
# of an inline script or a here-document, which is content rather than command.
MAX_TOOL_TEXT = 2000

# Commands are redacted up to this length before they are cut to MAX_TOOL_TEXT.
MAX_TOOL_SCAN = 20000

TOOL_ARGUMENT_KEYS = (
    "command",
    "cmd",
    "file_path",
    "path",
    "notebook_path",
    "pattern",
    "glob",
    "url",
    "query",
    "description",
    "title",
    "task_name",
    "skill",
    "args",
    "subagent_type",
    "server",
    "tool",
)

# Bookkeeping calls that never carry anything worth finding.
SKIPPED_TOOLS = {
    "todowrite",
    "todo",
    "todo_write",
    "update_plan",
    "exitplanmode",
    "wait",
    "wait_agent",
    "sleep",
    "list_agents",
    "js_reset",
    "write_stdin",
    "request_user_input_async",
}


def tool_text(name: object, arguments: object) -> str:
    """One searchable line for a tool call: `$ command` or `Tool arg …`."""
    name = name if isinstance(name, str) else ""
    if name.lower() in SKIPPED_TOOLS:
        return ""
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            arguments = {"command": arguments} if name in {"shell", "bash", "exec_command"} else {}
    if not isinstance(arguments, dict):
        arguments = {}
    command = arguments.get("command", arguments.get("cmd"))
    if isinstance(command, list):
        command = " ".join(str(part) for part in command)
        # ["bash", "-lc", "real command"] is how Codex wraps shell calls.
        command = re.sub(r"\A(?:ba|z)?sh -l?c ", "", command)
    if isinstance(command, str) and command.strip():
        text = f"$ {command.strip()}"
    else:
        parts = [
            str(arguments[key])
            for key in TOOL_ARGUMENT_KEYS
            if key in arguments and isinstance(arguments[key], (str, int, float)) and str(arguments[key]).strip()
        ]
        if not parts and not name:
            return ""
        text = " ".join([name, *parts])
    return squash(text[:MAX_TOOL_SCAN])


# The ways this tool is called: the entry script (from a clone, a copied skill
# folder or a plugin: any path ending in sessions.py), the `find-session`
# command (on PATH, through npx or uvx, with or without a version) and
# `python -m find_session`.
_LOOKUP_COMMANDS = r"(?:search|inspect|recent|index|brief-input)\b"
_LOOKUP_OPTIONS = r"(?:\s+--(?:no-cache|(?:home|book)(?:=|\s+)\S+))*\s+"

# A command that looked something up in the history: a call of this tool, or a
# read of the session files themselves. `sessions.py` may be named anywhere
# before the subcommand, because the skill has agents put its path in a
# variable first (`HELPER=…/sessions.py; python3 "$HELPER" search …`). The
# command name has to be followed by the subcommand, so a path that merely
# contains "find-session" (`cd ~/code/find-session && pytest`) is kept.
HISTORY_LOOKUP = re.compile(
    rf"\bsessions\.py\b[\s\S]*?\b{_LOOKUP_COMMANDS}"
    rf"|(?<![\w.-])find-session(?:@[\w.-]+)?[\"']?{_LOOKUP_OPTIONS}{_LOOKUP_COMMANDS}"
    rf"|-m\s+find_session{_LOOKUP_OPTIONS}{_LOOKUP_COMMANDS}"
    r"|\.claude/projects|\.codex/sessions|\.omp/agent/sessions"
)

PATCH_FILE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", re.M)

EXEC_INPUT_CMD = re.compile(r'"?cmd"?\s*:\s*"((?:[^"\\]|\\.)*)"')


def codex_custom_tool_text(name: object, raw: object) -> str:
    """Codex freeform tools: apply_patch (file names only) or exec (its commands)."""
    if not isinstance(raw, str):
        return ""
    if name == "apply_patch" or raw.lstrip().startswith("*** Begin Patch"):
        files = PATCH_FILE.findall(raw)
        return squash("apply_patch " + " ".join(files)) if files else ""
    commands = []
    for match in EXEC_INPUT_CMD.finditer(raw):
        try:
            commands.append(json.loads(f'"{match.group(1)}"'))
        except ValueError:
            commands.append(match.group(1))
    if commands:
        return tool_text(name, {"command": " ; ".join(commands)})
    return tool_text(name, {"command": raw})
