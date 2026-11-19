"""omp (oh-my-pi) session trees and subagent sidecars."""

from __future__ import annotations

import re
from collections import namedtuple
from pathlib import Path

from ..jsonl import iter_json_lines
from ..model import Candidate, Session
from ..text import clean_assistant_text, clean_user_text, content_text, short_project, squash
from ..tools import tool_text

# omp artifact directories are named after their session file:
# <timestamp>_<sessionId>/ next to <timestamp>_<sessionId>.jsonl.
OMP_SESSION_STEM = re.compile(r"^\d{4}-\d{2}-\d{2}T[^_/]*_.+$")


def omp_stem(path: Path) -> str:
    name = path.name
    for suffix in (".jsonl.gz", ".jsonl"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return path.stem


def omp_file_id(path: Path) -> str:
    stem = omp_stem(path)
    return stem.split("_", 1)[1] if "_" in stem else stem


def omp_active_path(entries: list) -> list:
    """Root->leaf path of the omp session tree, like buildSessionContext.

    Branch rewinds leave abandoned entries in the file; only the path from the
    last appended entry back to the root is the current conversation. Legacy v1
    files without ids are linear in file order. Entries are dicts or
    OmpEntry tuples; only their id/parentId are used.
    """

    def ids(entry) -> tuple[object, object]:
        if isinstance(entry, dict):
            return entry.get("id"), entry.get("parentId")
        return entry.entry_id, entry.parent_id

    by_id: dict[str, object] = {}
    leaf = None
    for entry in entries:
        entry_id, _ = ids(entry)
        if isinstance(entry_id, str):
            by_id.setdefault(entry_id, entry)
            leaf = entry
    if leaf is None:
        return entries
    chain: list = []
    seen: set[str] = set()
    current = leaf
    while current is not None:
        entry_id, parent = ids(current)
        if not isinstance(entry_id, str) or entry_id in seen:
            break
        seen.add(entry_id)
        chain.append(current)
        current = by_id.get(parent) if isinstance(parent, str) else None
    chain.reverse()
    return chain


# Slim tree node: the parsed record is dropped, only ids and text stay.
OmpEntry = namedtuple(
    "OmpEntry",
    ("entry_id", "parent_id", "role", "text", "timestamp", "tools"),
    defaults=("", "", "", ()),
)


def omp_entry_turn(obj: dict) -> tuple[str, str]:
    """(role, text) for one omp entry; ("", "") when it is not conversation.

    Verified against oh-my-pi v18.2.8 source: prompts written by an agent
    (subagent task, auto-continue) persist as role "user" with attribution
    "agent"; synthetic reminders as role "developer"; async results as role
    "custom" with customType "async-result". Only real user prompts count as
    user; agent-written prompts are kept but labelled "delegated".
    """
    entry_type = obj.get("type")
    if entry_type == "session_init":
        task = obj.get("task")
        return ("init", squash(task)) if isinstance(task, str) and task.strip() else ("", "")
    if entry_type != "message":
        return "", ""
    message = obj.get("message")
    if not isinstance(message, dict):
        return "", ""
    role = message.get("role")
    if role == "user":
        text = clean_user_text(content_text(message.get("content", ""), {"text"}))
        if not text:
            return "", ""
        return ("delegated" if message.get("attribution") == "agent" else "user"), text
    if role == "assistant":
        text = clean_assistant_text(content_text(message.get("content", ""), {"text"}))
        return ("assistant", text) if text else ("", "")
    return "", ""


def omp_entry_tools(obj: dict) -> tuple:
    """Commands of the toolCall blocks in an omp assistant message."""
    message = obj.get("message") if obj.get("type") == "message" else None
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return ()
    content = message.get("content")
    texts = [
        tool_text(block.get("name"), block.get("arguments"))
        for block in (content if isinstance(content, list) else [])
        if isinstance(block, dict) and block.get("type") == "toolCall"
    ]
    return tuple(text for text in texts if text)


def read_omp_header(path: Path) -> dict:
    """The session header (after the optional title slot), or {} if absent."""
    for index, obj in enumerate(iter_json_lines(path)):
        if obj.get("type") == "session":
            return obj
        if index >= 2:
            break
    return {}


def omp_header_id(path: Path) -> str:
    header_id = read_omp_header(path).get("id")
    return header_id if isinstance(header_id, str) and header_id else omp_file_id(path)


def parse_omp(
    path: Path,
    *,
    session_id: str | None = None,
    kind: str = "main",
    parent_id: str = "",
    stop_after_first_user: bool = False,
) -> Session:
    """Parse an omp session JSONL (title slot, header, SessionEntry tree).

    Only user/assistant text is kept; thinking, toolCall, toolResult,
    developer, bash/python executions, custom messages (async results),
    summaries and credential pins are ignored. Each entry is reduced to an
    OmpEntry as it is read, so large tool payloads are never retained.
    """
    session = Session(
        source="omp",
        session_id=session_id or omp_file_id(path),
        project="unknown",
        path=path,
        mtime=path.stat().st_mtime,
        kind=kind,
        parent_id=parent_id,
    )
    # Imported here: discovery loads this module on every search to list omp
    # files, and cleaning a title needs the redaction patterns.
    from ..titles import clean_title

    header_seen = False
    entries: list[OmpEntry] = []
    for obj in iter_json_lines(path):
        entry_type = obj.get("type")
        if entry_type == "title":
            # The fixed-width slot at the start of the file holds the current title.
            session.title = clean_title(obj.get("title")) or session.title
            continue
        if entry_type == "session":
            if not header_seen:
                header_seen = True
                # Files written before the slot existed carry the title here.
                session.title = session.title or clean_title(obj.get("title"))
                header_id = obj.get("id")
                # Child ids stay parent-scoped: Task.jsonl repeats across parents.
                if kind == "main" and session_id is None and isinstance(header_id, str) and header_id:
                    session.session_id = header_id
                session.cwd = str(obj.get("cwd") or "")
                session.project = short_project(session.cwd)
            continue
        role, text = omp_entry_turn(obj)
        stamp = obj.get("timestamp")
        stamp = stamp if isinstance(stamp, str) else ""
        if stamp:
            session.last_stamp = stamp
        if stop_after_first_user and role == "user":
            session.add("user", text, stamp)
            return session
        # Every entry keeps its place in the tree, but only messages carry text.
        entries.append(OmpEntry(obj.get("id"), obj.get("parentId"), role, text, stamp, omp_entry_tools(obj)))
    if stop_after_first_user:
        return session
    init_tasks: list[OmpEntry] = []
    for entry in omp_active_path(entries):
        if entry.role == "init":
            init_tasks.append(entry)
        elif entry.role in ("user", "assistant", "delegated"):
            session.add(entry.role, entry.text, entry.timestamp)
        for tool in entry.tools:
            session.add_tool(tool)
    # A subagent whose prompt never persisted still has its task in session_init.
    if init_tasks and not session.delegated_messages and not session.user_messages:
        tail = list(zip(session.turns, session.times))
        session.turns.clear()
        session.times.clear()
        for task in init_tasks:
            session.add("delegated", task.text, task.timestamp)
        for (role, text), stamp in tail:
            session.turns.append((role, text))
            session.times.append(stamp)
        shift = len(session.turns) - len(tail)
        session.tools = [(after + shift, text) for after, text in session.tools]
    return session


def omp_candidates(root: Path) -> list[Candidate]:
    """Main sessions and their subagent sidecars under one omp sessions root."""
    if not root.is_dir():
        return []
    files = [*root.rglob("*.jsonl"), *root.rglob("*.jsonl.gz")]
    names = {path for path in files}
    found: dict[tuple[str, str], Candidate] = {}
    header_ids: dict[Path, str] = {}

    def header_id(path: Path) -> str:
        # The printed selector uses the header id, so discovery must too (a
        # moved or forked file can keep an older filename).
        if path not in header_ids:
            try:
                header_ids[path] = omp_header_id(path)
            except OSError:
                header_ids[path] = omp_file_id(path)
        return header_ids[path]

    def owner_id(directory: Path) -> str:
        for suffix in (".jsonl", ".jsonl.gz"):
            session_file = directory.with_name(directory.name + suffix)
            if session_file in names:
                return header_id(session_file)
        return directory.name.split("_", 1)[1] if "_" in directory.name else directory.name

    for path in sorted(files):
        if path.name.startswith("__advisor") or not path.is_file():
            continue
        # Prefer the live file over a cold archive of the same session.
        if path.name.endswith(".gz") and path.with_name(path.name[:-3]) in names:
            continue
        owner: Path | None = None
        for ancestor in reversed(path.relative_to(root).parents):
            if ancestor == Path("."):
                continue
            directory = root / ancestor
            if (
                OMP_SESSION_STEM.match(directory.name)
                or directory.with_name(directory.name + ".jsonl") in names
                or directory.with_name(directory.name + ".jsonl.gz") in names
            ):
                owner = directory
                break
        if owner is None:
            candidate = Candidate("omp", path, header_id(path))
        else:
            parent = owner_id(owner)
            relative = path.relative_to(owner)
            child = "/".join([*relative.parts[:-1], omp_stem(path)])
            candidate = Candidate("omp", path, f"{parent}/{child}", "child", parent)
        found.setdefault((candidate.kind, candidate.session_id), candidate)
    return list(found.values())
