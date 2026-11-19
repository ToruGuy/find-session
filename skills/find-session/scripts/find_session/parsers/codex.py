"""Codex rollout files."""

from __future__ import annotations

import re
from pathlib import Path

from ..jsonl import Progress, iter_json_lines
from ..model import Session
from ..text import clean_assistant_text, clean_user_text, content_text, short_project
from ..tools import codex_custom_tool_text, tool_text

# Context Codex injects as user-role input. Only these exact envelopes,
# anchored at the start of a content block, are treated as injected; the same
# words or tags inside a user's own prose are kept.
CODEX_AGENTS_SCAFFOLD = re.compile(
    r"\A\s*# AGENTS\.md instructions(?: for [^\n]*)?[ \t]*\n\s*<INSTRUCTIONS>(?:.*?</INSTRUCTIONS>|.*\Z)",
    re.S,
)

CODEX_ENVELOPE = re.compile(
    r"\A\s*<(environment_context|user_instructions|recommended_plugins|skill"
    r"|context_refresh|assistant_context)>.*?</\1>",
    re.S,
)

CODEX_INNER_CONTEXT = re.compile(r"\A\s*<(context|environment_context)>.*?</\1>", re.S)

CODEX_USER_MESSAGE = re.compile(r"\A\s*<user_message>(.*?)</user_message>\s*\Z", re.S)


def strip_codex_injection(text: str) -> str:
    """Drop leading injected envelopes from one Codex block, keep user prose."""
    stripped = False
    while True:
        match = CODEX_AGENTS_SCAFFOLD.match(text) or CODEX_ENVELOPE.match(text)
        if not match:
            break
        text = text[match.end() :]
        stripped = True
    if stripped:
        # A refresh/context envelope can wrap the real prompt in <user_message>,
        # which may itself open with an injected <context> block.
        wrapped = CODEX_USER_MESSAGE.match(text)
        if wrapped:
            text = wrapped.group(1)
            while True:
                match = CODEX_INNER_CONTEXT.match(text)
                if not match:
                    break
                text = text[match.end() :]
    return text


def codex_user_text(content: object) -> str:
    """User prose from a Codex message, cleaned block by block.

    Codex packs AGENTS.md and environment context into the same user message
    as separate blocks; joining first would let the scaffold pose as the
    user's words (or hide prose placed after an envelope).
    """
    if isinstance(content, str):
        blocks: list[object] = [content]
    elif isinstance(content, list):
        blocks = [
            block.get("text")
            for block in content
            if isinstance(block, dict) and block.get("type") in {"input_text", "text"}
        ]
    else:
        blocks = []
    parts = [clean_user_text(strip_codex_injection(block)) for block in blocks if isinstance(block, str)]
    return " ".join(part for part in parts if part)


def parse_codex(path: Path, *, stop_after_first_user: bool = False, resume: Session | None = None) -> Session:
    """Parse a Codex rollout file.

    `resume` is an earlier parse of the same file whose `checkpoint` has been
    checked against the file (see `cache.resumable`): only the lines added
    since are read, and the result is the same as parsing the whole file.
    """
    # The last assistant message that was not marked final: (text, timestamp,
    # already redacted). It becomes the answer when the file has no final one.
    fallback: tuple[str, object, bool] | None = None
    if resume is not None:
        session = resume
        offset, crc, state = session.checkpoint
        session.mtime = path.stat().st_mtime
        progress = Progress(offset, crc)
        meta_seen = state["meta"]
        seen_tools: set = set(state["tools"])
        if state["fallback"] is not None:
            # The earlier parse ended by using the fallback as the answer: take
            # that step back, the file may have a final answer now.
            fallback = (state["fallback"][0], state["fallback"][1], True)
            session.turns.pop()
            session.times.pop()
            session.assistant_messages.pop()
    else:
        session = Session(
            source="Codex",
            session_id=path.stem.removeprefix("rollout-"),
            project="unknown",
            path=path,
            mtime=path.stat().st_mtime,
        )
        progress = Progress()
        meta_seen = False
        seen_tools = set()
    session.checkpoint = None
    kept_tools: set = set(seen_tools)
    for obj in iter_json_lines(path, progress):
        stamp = obj.get("timestamp", "")
        if stamp:
            session.last_stamp = stamp
        payload = obj.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        if obj.get("type") == "session_meta":
            # Forked and spawned sessions copy their parent's history, including
            # the parent's session_meta; only the first one describes this file.
            if meta_seen:
                continue
            meta_seen = True
            session.session_id = str(payload.get("id") or payload.get("session_id") or session.session_id)
            session.cwd = str(payload.get("cwd") or "")
            session.project = short_project(session.cwd)
            session.kind, session.parent_id = codex_kind(payload)
            continue
        if obj.get("type") != "response_item":
            continue
        item_type = payload.get("type")
        if item_type in {"function_call", "custom_tool_call", "local_shell_call"}:
            if stop_after_first_user:
                continue
            key = payload.get("call_id") or payload.get("id") or id(payload)
            if key in seen_tools:
                continue
            seen_tools.add(key)
            if payload.get("call_id") or payload.get("id"):
                kept_tools.add(key)
            if item_type == "function_call":
                session.add_tool(tool_text(payload.get("name"), payload.get("arguments")))
            elif item_type == "custom_tool_call":
                session.add_tool(codex_custom_tool_text(payload.get("name"), payload.get("input")))
            else:
                action = payload.get("action")
                session.add_tool(tool_text("shell", action if isinstance(action, dict) else {}))
            continue
        if item_type != "message":
            continue
        role = payload.get("role")
        if role == "user":
            text = codex_user_text(payload.get("content", ""))
            if text:
                # A spawned subagent's first prompt is written by its parent agent.
                delegated = session.kind != "main" and not session.turns
                session.add("delegated" if delegated else "user", text, stamp)
                if stop_after_first_user:
                    break
        elif role == "assistant" and not stop_after_first_user:
            text = clean_assistant_text(content_text(payload.get("content", ""), {"output_text", "text"}))
            if not text:
                continue
            fallback = (text, stamp, False)
            if payload.get("phase") in {"final", "final_answer"}:
                session.add("assistant", text, stamp)
    if stop_after_first_user:
        return session
    fallback_used = not session.assistant_messages and fallback is not None
    if fallback_used:
        text, stamp, redacted = fallback
        if redacted:
            session.append("assistant", text, stamp)
        else:
            session.add("assistant", text, stamp)
    if progress.resumable and all(type(key) is str for key in kept_tools):
        session.checkpoint = (
            progress.offset,
            progress.crc,
            {
                "meta": meta_seen,
                "tools": sorted(kept_tools),
                # Only needed while it stands in for the answer; kept as the
                # session has it, that is redacted.
                "fallback": (session.turns[-1][1], session.times[-1]) if fallback_used else None,
            },
        )
    return session


def codex_kind(meta: dict) -> tuple[str, str]:
    """("main", "") for a user conversation; ("child", parent) for a spawned
    subagent; ("review", "") for an automatic approval review thread."""
    source = meta.get("source")
    sub = source.get("subagent") if isinstance(source, dict) else None
    if meta.get("thread_source") == "guardian_review" or (isinstance(sub, dict) and sub.get("other") == "guardian"):
        return "review", ""
    if sub is not None or meta.get("thread_source") == "subagent":
        spawn = sub.get("thread_spawn") if isinstance(sub, dict) else None
        parent = spawn.get("parent_thread_id") if isinstance(spawn, dict) else ""
        return "child", parent if isinstance(parent, str) else ""
    return "main", ""
