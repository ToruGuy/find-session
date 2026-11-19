"""Claude Code session files."""

from __future__ import annotations

from pathlib import Path

from ..jsonl import Progress, iter_json_lines
from ..model import Session
from ..text import clean_assistant_text, clean_user_text, content_text, short_project
from ..titles import clean_title
from ..tools import tool_text


def claude_project(dirname: str) -> str:
    parts = [part for part in dirname.split("-") if part]
    if len(parts) >= 3 and parts[0] in {"Users", "home", "root"}:
        return "~/" + "/".join(parts[2:])
    return "/".join(parts[-3:]) if parts else dirname


COMPACTION_PREFIX = "This session is being continued from a previous conversation"

# {"type": "custom-title", "customTitle": …} is the name the user gave the
# session, {"type": "ai-title", "aiTitle": …} the one Claude Code generated.
TITLE_FIELDS = {"custom-title": "customTitle", "ai-title": "aiTitle"}


# Copies of what was sent to the model, kept by Claude Code next to the
# conversation: {"type": "api-request-blob", "message": {...}} and its
# relatives. They repeat messages, instructions files and injected context,
# and are no part of what was said.
REQUEST_RECORDS = "api-request"
# As Claude Code writes them, they are recognised before the line is decoded:
# they are up to a fifth of a session file. One written with its keys in
# another order is decoded and dropped by its type just the same.
REQUEST_LINE = b'{"type":"api-request'


def parse_claude(
    path: Path,
    *,
    stop_after_first_user: bool = False,
    resume: Session | None = None,
    session_id: str = "",
    parent_id: str = "",
) -> Session:
    """Parse a Claude Code session file.

    `resume` is an earlier parse of the same file whose `checkpoint` has been
    checked against the file (see `cache.resumable`): only the lines added
    since are read, and the result is the same as parsing the whole file.

    With `parent_id` the file is a subagent's transcript (`session_id` is its
    agent id): everything said to it was written by an agent, the task first.
    """
    if resume is not None:
        session = resume
        offset, crc, state = session.checkpoint
        session.mtime = path.stat().st_mtime
        progress = Progress(offset, crc)
        cwd_seen = bool(session.cwd)
        seen_tools: set = set(state["tools"])
        titles = {"custom-title": state.get("custom", ""), "ai-title": state.get("ai", "")}
    else:
        session = Session(
            source="Claude",
            session_id=session_id or path.stem,
            project="unknown" if parent_id else claude_project(path.parent.name),
            path=path,
            mtime=path.stat().st_mtime,
            kind="child" if parent_id else "main",
            parent_id=parent_id,
        )
        progress = Progress()
        cwd_seen = False
        seen_tools = set()
        titles = {"custom-title": "", "ai-title": ""}
    session.checkpoint = None
    said_by_user = "delegated" if session.kind == "child" else "user"
    # Title records are written again and again, at any point of the file; the
    # last one of each kind that has text counts. Only those are cleaned.
    renamed: dict[str, str] = {}
    # Tool-call ids worth remembering for a later continuation.
    kept_tools: set = set(seen_tools)
    for obj in iter_json_lines(path, progress, skip=REQUEST_LINE):
        kind = obj.get("type")
        if isinstance(kind, str) and kind.startswith(REQUEST_RECORDS):
            continue
        stamp = obj.get("timestamp", "")
        if stamp:
            session.last_stamp = stamp
        # The encoded directory name loses dots and hyphens; the recorded cwd
        # is exact, so it wins whenever a record carries it.
        cwd = obj.get("cwd")
        if not cwd_seen and isinstance(cwd, str) and cwd:
            session.project = short_project(cwd)
            session.cwd = cwd
            cwd_seen = True
        if kind in TITLE_FIELDS:
            title = obj.get(TITLE_FIELDS[kind])
            if isinstance(title, str) and title.strip():
                renamed[kind] = title
            continue
        message = obj.get("message") or {}
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role == "user":
            raw = content_text(message.get("content", ""), {"text"})
            if obj.get("isCompactSummary") is True or raw.lstrip().startswith(COMPACTION_PREFIX):
                # Written by Claude Code when the context was compacted: it
                # retells the conversation, it is not something the user said.
                if not stop_after_first_user:
                    text = clean_assistant_text(raw)
                    if text:
                        session.add("summary", text, stamp)
                continue
            text = clean_user_text(raw)
            if text:
                session.add(said_by_user, text, stamp)
                if stop_after_first_user:
                    break
        elif role == "assistant" and not stop_after_first_user:
            text = clean_assistant_text(content_text(message.get("content", ""), {"text"}))
            if text:
                session.add("assistant", text, stamp)
            blocks = message.get("content")
            for block in blocks if isinstance(blocks, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    # Streaming can record the same call more than once.
                    key = block.get("id") or id(block)
                    if key not in seen_tools:
                        seen_tools.add(key)
                        if block.get("id"):
                            kept_tools.add(key)
                        session.add_tool(tool_text(block.get("name"), block.get("input")))
    for kind, title in renamed.items():
        titles[kind] = clean_title(title)
    # A name the user chose wins over the generated one, whichever came last.
    session.title = titles["custom-title"] or titles["ai-title"]
    if not stop_after_first_user and progress.resumable and all(type(key) is str for key in kept_tools):
        session.checkpoint = (
            progress.offset,
            progress.crc,
            # Both titles, as cleaned: a later record of one kind replaces only that kind.
            {"tools": sorted(kept_tools), "custom": titles["custom-title"], "ai": titles["ai-title"]},
        )
    return session
