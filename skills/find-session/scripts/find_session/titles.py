"""Session titles: the name a harness gave a conversation.

A title identifies a session in fewer characters than its first message. It is
only shown: it is not part of the searched text and never changes a score.

- Claude Code writes `custom-title` and `ai-title` records into the session
  file (see `parsers/claude.py`), omp a title slot and a header field (see
  `parsers/omp.py`).
- Codex keeps thread names in a file of its own, read here.
- Antigravity CLI (agy) keeps titles, with each conversation's workspace and
  parent, in its SQLite `conversation_summaries.db`, read here.

Used only while parsing: a search served from the index gets its titles from
the index and does not load this module.
"""

from __future__ import annotations

import os
from pathlib import Path

from .jsonl import iter_json_lines
from .model import MAX_INDEX_TEXT
from .redact import redact
from .text import squash

# A title is redacted up to this length before it is cut to MAX_INDEX_TEXT
# (a secret cut in half would no longer match its pattern).
MAX_TITLE_SCAN = 2000


def clean_title(text: object) -> str:
    """A title as it is stored and shown: one line, redacted, bounded."""
    if not isinstance(text, str):
        return ""
    text = squash(text[:MAX_TITLE_SCAN])
    return redact(text)[:MAX_INDEX_TEXT] if text else ""


def read_codex_titles(location: str) -> dict[str, str]:
    """Session id -> thread name from Codex's `session_index.jsonl`.

    The file gets a row per rename, `{"id", "thread_name", "updated_at"}`; the
    last row with a name wins. A missing or unreadable file names nothing.
    """
    names: dict[str, str] = {}
    try:
        for row in iter_json_lines(Path(location)):
            session_id, name = row.get("id"), row.get("thread_name")
            if isinstance(session_id, str) and session_id and isinstance(name, str) and name.strip():
                names[session_id] = name
    except OSError:
        return {}
    titles = {session_id: clean_title(name) for session_id, name in names.items()}
    return {session_id: title for session_id, title in titles.items() if title}


def read_titles(location: str) -> dict[str, str]:
    """Session id -> title from a titles file of either kind."""
    if location.endswith(".db"):
        return read_agy_titles(location)
    return read_codex_titles(location)


def read_agy_titles(location: str) -> dict[str, str]:
    """Session id -> title from agy's `conversation_summaries.db`."""
    titles = {session_id: clean_title(row[0]) for session_id, row in agy_summaries(location).items()}
    return {session_id: title for session_id, title in titles.items() if title}


# agy's summaries database as read in this run: location -> (stamp, rows).
_AGY_READ: dict[str, tuple[tuple[int, int, int, int] | None, dict[str, tuple[str, str, str]]]] = {}


def agy_summaries(location: str) -> dict[str, tuple[str, str, str]]:
    """Conversation id -> (raw title, working directory, parent conversation id)
    from agy's `conversation_summaries.db`; nothing when it cannot be read.

    agy writes the database while it runs (WAL mode). It is only ever read:
    while a `-wal` file exists (agy has it open) through SQLite's read-only
    mode, which sees what is still in the WAL; otherwise as an immutable file,
    so that reading creates nothing next to it. The working directory is the
    first workspace URI.
    """
    stamps = []
    for name in (location, location + "-wal"):
        try:
            stat = os.stat(name)
            stamps += [stat.st_size, stat.st_mtime_ns]
        except OSError:
            stamps += [-1, -1]
    if stamps[0] < 0:
        return {}
    stamp = tuple(stamps)
    known = _AGY_READ.get(location)
    if known is not None and known[0] == stamp:
        return known[1]
    rows = _query_agy(location, live=stamps[2] >= 0)
    if rows is None and stamps[2] >= 0:
        # A read-only directory (a synced copy) cannot hold SQLite's lock files.
        rows = _query_agy(location, live=False)
    found: dict[str, tuple[str, str, str]] = {}
    for conversation_id, title, workspaces, parent in rows or ():
        if isinstance(conversation_id, str) and conversation_id:
            found[conversation_id] = (
                title if isinstance(title, str) else "",
                _first_workspace(workspaces),
                parent if isinstance(parent, str) else "",
            )
    _AGY_READ[location] = (stamp, found)
    return found


def _query_agy(location: str, *, live: bool) -> list[tuple] | None:
    import sqlite3
    from urllib.parse import quote

    mode = "mode=ro" if live else "immutable=1"
    uri = f"file:{quote(location)}?{mode}"
    try:
        db = sqlite3.connect(uri, uri=True)
        try:
            return db.execute(
                "SELECT conversation_id, title, workspace_uris, parent_conversation_id FROM conversation_summaries"
            ).fetchall()
        finally:
            db.close()
    except sqlite3.Error:
        return None


def _first_workspace(value: object) -> str:
    """The path of the first `file://` URI in agy's JSON list of workspace URIs."""
    if not isinstance(value, str):
        return ""
    import json
    from urllib.parse import unquote, urlparse

    try:
        uris = json.loads(value)
    except ValueError:
        return ""
    for uri in uris if isinstance(uris, list) else ():
        if isinstance(uri, str) and uri.startswith("file://"):
            return unquote(urlparse(uri).path)
    return ""
