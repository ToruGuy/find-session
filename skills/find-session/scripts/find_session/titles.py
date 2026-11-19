"""Session titles: the name a harness gave a conversation.

A title identifies a session in fewer characters than its first message. It is
only shown: it is not part of the searched text and never changes a score.

- Claude Code writes `custom-title` and `ai-title` records into the session
  file (see `parsers/claude.py`), omp a title slot and a header field (see
  `parsers/omp.py`).
- Codex keeps thread names in a file of its own, read here.

Used only while parsing: a search served from the index gets its titles from
the index and does not load this module.
"""

from __future__ import annotations

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
