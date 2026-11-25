"""Antigravity CLI (agy) conversation transcripts.

agy keeps one folder per conversation, `brain/<conversation id>/`, below its
data root (`~/.gemini/antigravity-cli`). The transcript is
`.system_generated/logs/transcript_full.jsonl`: one step per line, with the
tool calls as structured arguments. `transcript.jsonl` next to it holds the
same steps with long fields cut and argument values JSON-encoded; it is read
only when the full one is missing. The working directory, the title and the
parent of a conversation are not in the transcript but in the data root's
`conversation_summaries.db` (see `titles.agy_summaries`).
"""

from __future__ import annotations

import re
from pathlib import Path

from ..discovery import AGY_SUMMARIES
from ..jsonl import iter_json_lines
from ..model import Session
from ..text import clean_assistant_text, clean_user_text, short_project
from ..tools import tool_text

# The user's own words; agy wraps them with metadata it adds itself
# (<ADDITIONAL_METADATA>, <USER_SETTINGS_CHANGE>). In `transcript.jsonl` a long
# request is cut short before its closing tag.
USER_REQUEST = re.compile(r"<USER_REQUEST>\s*(.*?)\s*(?:</USER_REQUEST>|\Z)", re.S)

# agy's argument names -> the names `tools.tool_text` knows. Anything else
# (file bodies, replacement chunks, timeouts) is left out.
AGY_ARGUMENTS = {
    "commandline": "command",
    "command": "command",
    "path": "path",
    "absolutepath": "path",
    "targetfile": "path",
    "directorypath": "path",
    "searchpath": "path",
    "searchdirectory": "path",
    "file": "path",
    "pattern": "pattern",
    "query": "query",
    "url": "url",
    "toolsummary": "description",
}


def agy_summaries_path(transcript: Path) -> Path:
    """`conversation_summaries.db` of the data root a transcript belongs to."""
    # <root>/brain/<id>/.system_generated/logs/<transcript>
    return transcript.parents[4] / AGY_SUMMARIES


def agy_user_text(content: object) -> str:
    """The user's request from a USER_INPUT step, without agy's metadata."""
    if not isinstance(content, str):
        return ""
    requests = USER_REQUEST.findall(content)
    return clean_user_text("\n".join(requests)) if requests else clean_user_text(content)


def agy_value(value: object) -> object:
    """An argument value; `transcript.jsonl` stores strings JSON-encoded ('"ls -la"')."""
    if isinstance(value, str) and len(value) >= 2 and value[0] == value[-1] == '"':
        import json

        try:
            decoded = json.loads(value)
        except ValueError:
            return value
        return decoded if isinstance(decoded, str) else value
    return value


def agy_tool_calls(value: object) -> list:
    """The tool calls of a step: a list, or (cut short) a string holding one."""
    if isinstance(value, str):
        import ast

        try:
            value = ast.literal_eval(value)
        except (ValueError, SyntaxError, MemoryError, RecursionError):
            return []
    return value if isinstance(value, list) else []


def agy_tool_text(call: object) -> str:
    """One searchable line for an agy tool call (its command, never its output)."""
    if not isinstance(call, dict):
        return ""
    raw = call.get("args")
    arguments: dict[str, object] = {}
    for key, value in raw.items() if isinstance(raw, dict) else ():
        name = AGY_ARGUMENTS.get(str(key).lower())
        if name and name not in arguments:
            arguments[name] = agy_value(value)
    return tool_text(call.get("name"), arguments)


def parse_agy(path: Path, *, session_id: str | None = None, stop_after_first_user: bool = False) -> Session:
    """Parse an agy transcript.

    Kept: the user's requests (USER_INPUT) and the model's visible answers and
    tool calls (PLANNER_RESPONSE). Left out: reasoning (`thinking`), tool
    output (GENERIC steps), harness reminders (SYSTEM_MESSAGE) and every
    other kind of step. A conversation with a parent is a subagent: its
    first request was written by the parent agent.
    """
    session = Session(
        source="agy",
        session_id=session_id or path.parents[2].name,
        project="unknown",
        path=path,
        mtime=path.stat().st_mtime,
    )
    from ..titles import agy_summaries

    summary = agy_summaries(str(agy_summaries_path(path))).get(session.session_id)
    if summary is not None:
        _, session.cwd, parent = summary
        session.project = short_project(session.cwd)
        if parent:
            session.kind, session.parent_id = "child", parent
    for step in iter_json_lines(path):
        stamp = step.get("created_at")
        stamp = stamp if isinstance(stamp, str) else ""
        if stamp:
            session.last_stamp = stamp
        step_type = step.get("type")
        if step_type == "USER_INPUT":
            text = agy_user_text(step.get("content"))
            if not text:
                continue
            # Only a person types into agy; a subagent's request comes from its parent.
            human = step.get("source") == "USER_EXPLICIT" and not (session.kind != "main" and not session.turns)
            session.add("user" if human else "delegated", text, stamp)
            if stop_after_first_user:
                break
        elif step_type == "PLANNER_RESPONSE" and not stop_after_first_user:
            content = step.get("content")
            text = clean_assistant_text(content) if isinstance(content, str) else ""
            if text:
                session.add("assistant", text, stamp)
            for call in agy_tool_calls(step.get("tool_calls")):
                session.add_tool(agy_tool_text(call))
    return session
