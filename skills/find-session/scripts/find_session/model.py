"""Sessions and session files as the rest of the package sees them."""

from __future__ import annotations

import os
from pathlib import Path

MAX_INDEX_TEXT = 180

# What `recent` prints of a request and of an answer: enough to recognise the
# session; `inspect` shows the rest.
MAX_USER_BRIEF = 240

MAX_ASSISTANT_BRIEF = 400

MAX_INSPECT_TEXT = 2400

# What `inspect --tools` prints of one command; `--message-chars 0` prints all that is stored.
MAX_INSPECT_COMMAND = 400

# Total characters `inspect` prints by default: what an agent's harness still
# shows inline (Claude Code moved a 30,000-character result to a file).
MAX_INSPECT_TOTAL = 20000

MAX_AMBIGUOUS_LOAD = 50

SOURCES = ("Claude", "Codex", "omp", "agy")

SOURCE_KEYS = {"claude": "Claude", "codex": "Codex", "omp": "omp", "agy": "agy"}


class Session:
    """One parsed conversation.

    `turns` is the chronological transcript: (role, text) with role user,
    assistant, delegated (a prompt written by an agent, e.g. a subagent task)
    or summary (a context-compaction summary written by the harness). `times`
    holds ISO-8601 timestamps aligned with `turns` ("" when the record has
    none). `tools` holds tool-call commands as (message number they follow,
    text): they never get a number of their own, so message numbers stay the
    same with or without them; 0 means before the first message. `cwd` is the
    exact working directory recorded in the session ("" when unknown). `title`
    is the name the harness gave the conversation ("" when it has none): it is
    shown, never searched or scored. `mtime` is when the session was last
    active: the time of the last record in its file (`last_stamp`), or the
    file's own modification time when no record carries one. A history copied
    from another machine keeps its real dates that way.
    """

    # A plain class, not a dataclass: importing `dataclasses` costs more than a
    # warm search spends on scoring.
    def __init__(
        self,
        source: str,
        session_id: str,
        project: str,
        path: Path,
        mtime: float,
        user_messages: list[str] | None = None,
        assistant_messages: list[str] | None = None,
        kind: str = "main",
        parent_id: str = "",
        cwd: str = "",
        turns: list[tuple[str, str]] | None = None,
        delegated_messages: list[str] | None = None,
        summaries: list[str] | None = None,
        times: list[str] | None = None,
        tools: list[tuple[int, str]] | None = None,
        title: str = "",
    ):
        self.source = source
        self.session_id = session_id
        self.project = project
        self.path = path
        self.mtime = mtime
        self.user_messages: list[str] = [] if user_messages is None else user_messages
        self.assistant_messages: list[str] = [] if assistant_messages is None else assistant_messages
        self.kind = kind
        self.parent_id = parent_id
        self.cwd = cwd
        self.turns: list[tuple[str, str]] = [] if turns is None else turns
        self.delegated_messages: list[str] = [] if delegated_messages is None else delegated_messages
        self.summaries: list[str] = [] if summaries is None else summaries
        self.times: list[str] = [] if times is None else times
        self.tools: list[tuple[int, str]] = [] if tools is None else tools
        # Place in `tools` -> how many characters of that command were not kept.
        # Kept apart from the text, so the number is never searched.
        self.tool_cuts: dict[int, int] = {}
        # Redacted before it gets here (`titles.clean_title`), like all other text.
        self.title = title
        # Timestamp of the last record that has one, as the file gives it.
        self.last_stamp = ""
        # Set by a parser that can continue this session when its file grows:
        # (bytes read, CRC-32 of those bytes, parser state). See `Checkpoint`.
        self.checkpoint: tuple | None = None

    def __repr__(self) -> str:
        return f"Session({self.source!r}, {self.session_id!r}, {len(self.turns)} messages)"

    def add_tool(self, text: str) -> None:
        # Imported here: a search served from the index never parses, and the
        # redaction patterns take longer to compile than such a search may take.
        from .redact import redact
        from .tools import HISTORY_LOOKUP, MAX_TOOL_SCAN, MAX_TOOL_TEXT

        # Looking a topic up in the history is not working on it: a session
        # that searched for "invoice export" must not rank for it.
        if not text or HISTORY_LOOKUP.search(text):
            return
        # Redact the whole command first: a secret cut in half by the length
        # limit would no longer match its pattern.
        text = redact(text[:MAX_TOOL_SCAN])
        if len(text) > MAX_TOOL_TEXT:
            self.tool_cuts[len(self.tools)] = len(text) - MAX_TOOL_TEXT
            text = text[:MAX_TOOL_TEXT]
        self.tools.append((len(self.turns), text))

    def add(self, role: str, text: str, timestamp: object = "") -> None:
        # The only way text enters a parsed session, so nothing unredacted
        # reaches the cache or any output.
        from .redact import redact

        self.append(role, redact(text), timestamp)

    def append(self, role: str, text: str, timestamp: object = "") -> None:
        if self.turns and self.turns[-1] == (role, text) and role == "user":
            # A queued prompt is sometimes recorded twice in a row.
            if not self.times[-1] and isinstance(timestamp, str):
                self.times[-1] = timestamp
            return
        self.turns.append((role, text))
        self.times.append(timestamp if isinstance(timestamp, str) else "")
        {
            "user": self.user_messages,
            "assistant": self.assistant_messages,
            "delegated": self.delegated_messages,
            "summary": self.summaries,
        }[role].append(text)

    @property
    def started(self) -> str:
        return next((t for t in self.times if t), "")

    @property
    def ended(self) -> str:
        return next((t for t in reversed(self.times) if t), "")


class Candidate:
    """A discovered session file with deterministic source attribution.

    `location` is the file path as text; `path` builds the `Path` only when
    asked, because a search over thousands of files needs it for a handful.
    `stat` is what discovery already learned about the file (None when it did
    not look); it is not part of the candidate's identity. Neither is `titles`:
    the file that names this source's sessions when the session file itself
    does not (Codex keeps thread names next to its sessions directory), nor
    `copies`: other files holding the same conversation, which this one stands
    for (`cache.latest_copies`).
    """

    __slots__ = ("source", "location", "session_id", "kind", "parent_id", "stat", "titles", "copies", "_path")

    def __init__(
        self,
        source: str,
        path: Path | str,
        session_id: str,
        kind: str = "main",
        parent_id: str = "",
        stat: os.stat_result | None = None,
        titles: str = "",
    ):
        self.source = source
        self.location = path if isinstance(path, str) else str(path)
        self._path = None if isinstance(path, str) else path
        self.session_id = session_id
        self.kind = kind
        self.parent_id = parent_id
        self.stat = stat
        self.titles = titles
        self.copies: tuple[Candidate, ...] = ()

    @property
    def path(self) -> Path:
        if self._path is None:
            self._path = Path(self.location)
        return self._path

    @property
    def name(self) -> str:
        """File name (last path component)."""
        return self.location.rpartition("/")[2] if os.sep == "/" else self.path.name

    def _key(self) -> tuple:
        return (self.source, self.location, self.session_id, self.kind, self.parent_id)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Candidate) and self._key() == other._key()

    def __hash__(self) -> int:
        return hash(self._key())

    def __repr__(self) -> str:
        return f"Candidate{self._key()!r}"

    @property
    def selector(self) -> str:
        return f"{self.source.lower()}:{self.session_id}"
