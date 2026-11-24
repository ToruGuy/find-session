"""Sessions as the commands get them: parsed now, or served from the index."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from .discovery import omp_roots, roots
from .model import MAX_INDEX_TEXT, Candidate, Session
from .searchtext import SearchText

if False:  # typing only; the index (and sqlite3) is loaded when a cache is in use
    from typing import Iterable, Iterator

    from .index import SessionIndex


def load_candidate(candidate: Candidate, *, first_only: bool = False) -> Session:
    """Parsed session; served from the index when one is active (always full)."""
    return entry_for(candidate, first_only=first_only).session()


def parse_candidate(candidate: Candidate, *, first_only: bool = False, resume: Session | None = None) -> Session:
    """Parse a session file; `resume` continues an earlier parse (see `resumable`)."""
    if candidate.source == "omp":
        from .parsers.omp import parse_omp

        session = parse_omp(
            candidate.path,
            session_id=candidate.session_id if candidate.kind == "child" else None,
            kind=candidate.kind,
            parent_id=candidate.parent_id,
            stop_after_first_user=first_only,
        )
    elif candidate.source == "agy":
        from .parsers.agy import parse_agy

        session = parse_agy(candidate.path, session_id=candidate.session_id, stop_after_first_user=first_only)
    elif candidate.source == "Claude" and candidate.kind == "child":
        from .parsers.claude import parse_claude

        session = parse_claude(
            candidate.path,
            stop_after_first_user=first_only,
            resume=resume,
            session_id=candidate.session_id,
            parent_id=candidate.parent_id,
        )
    else:
        session = load_session(candidate.source, candidate.path, first_only=first_only, resume=resume)
    if not first_only:
        date_from_records(session)
    return session


def date_from_records(session: Session) -> None:
    """Take the session's time from its last record instead of from the file.

    The parser set `mtime` to the file's modification time, which is the time
    of the copy when a history was copied or synced from another machine. The
    records say when the conversation really happened. A file without a
    readable timestamp keeps the file's time. (Whether a file changed is a
    separate matter: the index compares the file's size and mtime itself.)
    """
    stamp = session.last_stamp
    if not isinstance(stamp, str) or not stamp:
        return
    from .text import local_time

    moment = local_time(stamp)
    if moment is not None:
        try:
            session.mtime = moment.timestamp()
        except (OverflowError, OSError, ValueError):
            pass


def load_session(source: str, path: Path, *, first_only: bool = False, resume: Session | None = None) -> Session:
    # Parsers are imported when a file really has to be parsed: a search served
    # from the index needs none of them.
    if source == "Codex":
        from .parsers.codex import parse_codex

        return parse_codex(path, stop_after_first_user=first_only, resume=resume)
    if source == "omp":
        from .parsers.omp import parse_omp

        return parse_omp(path, stop_after_first_user=first_only)
    if source == "agy":
        from .parsers.agy import parse_agy

        return parse_agy(path, stop_after_first_user=first_only)
    from .parsers.claude import parse_claude

    return parse_claude(path, stop_after_first_user=first_only, resume=resume)


# Sources whose files only grow and whose parsers can continue where they stopped.
RESUMABLE_SOURCES = ("Claude", "Codex")


def resumable(path: Path, record: dict, checkpoint: object) -> Session | None:
    """An earlier parse to continue from, or None when the file has to be read again.

    A live session file is appended to between searches, and reading a large
    one from the start costs more than the whole search. Continuing is allowed
    only when the file still begins with exactly the bytes the earlier parse
    read (checked with their CRC-32), so a file that was rewritten, truncated
    or replaced is always parsed from the start.
    """
    from .jsonl import prefix_crc

    if not (isinstance(checkpoint, tuple) and len(checkpoint) == 3):
        return None
    offset, crc, state = checkpoint
    if not (isinstance(offset, int) and isinstance(crc, int) and isinstance(state, dict)):
        return None
    if prefix_crc(path, offset) != crc:
        return None
    session = session_from_record(path, record)
    session.checkpoint = checkpoint
    return session


# Bump when parsing changes what a Session contains, so old indexes are rebuilt.
PARSER_VERSION = 11


class Entry:
    """One discovered session as the commands see it first: a handful of facts.

    Listing, filtering and ranking thousands of sessions needs only these
    facts and the search text; the conversation itself (`session()`) is loaded
    for the few sessions that are printed. An entry comes either from a file
    parsed in this run or from a row of the index, and behaves the same.
    """

    __slots__ = (
        "candidate",
        "source",
        "session_id",
        "kind",
        "parent_id",
        "project",
        "cwd",
        "mtime",
        "started",
        "messages",
        "first_user",
        "title",
        "_session",
        "_text",
        "_index",
        "_row",
    )

    def __init__(self, candidate: Candidate):
        self.candidate = candidate
        self._session: Session | None = None
        self._text: SearchText | None = None
        self._index: SessionIndex | None = None
        self._row: tuple | None = None

    @classmethod
    def of(cls, candidate: Candidate, session: Session) -> Entry:
        entry = cls(candidate)
        entry.adopt(session)
        return entry

    def adopt(self, session: Session) -> None:
        self._session = session
        self._text = None
        self._row = None
        self.source = session.source
        self.session_id = session.session_id
        self.kind = session.kind
        self.parent_id = session.parent_id
        self.project = session.project
        self.cwd = session.cwd
        self.mtime = session.mtime
        self.started = session.started
        self.messages = len(session.turns)
        self.first_user = session.user_messages[0][:MAX_INDEX_TEXT] if session.user_messages else ""
        self.name(session.title)
        session.title = self.title

    def name(self, parsed: str) -> None:
        """Set the title: the one parsed from the session file, or the one kept outside it."""
        if self.source in TITLED_ELSEWHERE:
            titles = self.candidate.titles
            self.title = external_titles(titles).get(self.session_id, "") if titles else ""
        else:
            self.title = parsed

    def session(self) -> Session:
        """The whole conversation (read from the index, or parsed, on first use)."""
        if self._session is None:
            self._index.load_session(self)
            self._session.title = self.title
        return self._session

    def search_text(self) -> SearchText:
        if self._text is not None:
            return self._text
        if self._session is None:
            text = self._index.load_text(self)
            if text is not None:
                return text
        text = SearchText.of(self.session())
        if self._index is not None and self._row is None:
            # Parsed in this run: the index stores the same text when it saves.
            self._text = text
        return text


def entry_for(candidate: Candidate, *, first_only: bool = False) -> Entry:
    """Facts about one session file; raises OSError when it cannot be read."""
    if CACHE is not None:
        return CACHE.entry(candidate)
    return Entry.of(candidate, parse_candidate(candidate, first_only=first_only))


def entries(candidates: Iterable[Candidate], *, first_only: bool = False) -> Iterator[Entry]:
    """An entry per readable session file, in the order given."""
    if CACHE is not None:
        yield from CACHE.entries(candidates)
        return
    for candidate in candidates:
        try:
            yield entry_for(candidate, first_only=first_only)
        except OSError:
            continue


def latest_copies(candidates: Iterable[Candidate]) -> list[Candidate]:
    """`candidates` with the copies of one conversation reduced to one file.

    A session continued from another project folder is written there again
    under the same id, so one conversation has two files that begin alike.
    The one whose last record is the latest stands for the conversation (the
    longer one among equals) and takes the place of the first; the others are
    named in its `copies`. Nearly always there is nothing to do, and no file
    is looked into.
    """
    candidates = list(candidates)
    places: dict[tuple[str, str, str], list[int]] = {}
    for place, candidate in enumerate(candidates):
        places.setdefault((candidate.source, candidate.kind, candidate.session_id), []).append(place)
    if len(places) == len(candidates):
        return candidates
    dropped: set[int] = set()
    for same in places.values():
        if len(same) < 2:
            continue
        dated = []
        for place in same:
            try:
                entry = entry_for(candidates[place])
            except OSError:
                continue  # unreadable: not a choice
            # Among equals the first in path order, like everywhere else.
            dated.append((entry.mtime, entry.messages, -place, candidates[place]))
        dropped.update(same[1:])
        if dated:
            dated.sort(key=lambda row: row[:3], reverse=True)
            latest = dated[0][3]
            latest.copies = tuple(row[3] for row in dated[1:])
            candidates[same[0]] = latest
    return [candidate for place, candidate in enumerate(candidates) if place not in dropped]


def with_search_text(found: Iterable[Entry]) -> Iterator[tuple[Entry, SearchText]]:
    """Each entry with its search text; the index reads many texts per query."""
    if CACHE is not None:
        yield from CACHE.with_text(found)
        return
    for entry in found:
        yield entry, entry.search_text()


def note_present(candidates: Iterable[Candidate]) -> None:
    """Tell the index which files discovery saw, so it does not check them again."""
    if CACHE is not None:
        CACHE.present.update(candidate.location for candidate in candidates)


def session_record(session: Session) -> dict:
    return {
        "source": session.source,
        "session_id": session.session_id,
        "project": session.project,
        "mtime": session.mtime,
        "kind": session.kind,
        "parent_id": session.parent_id,
        "cwd": session.cwd,
        "turns": [[role, text, stamp] for (role, text), stamp in zip(session.turns, session.times)],
        "tools": [[after, text] for after, text in session.tools],
        "tool_cuts": sorted(session.tool_cuts.items()),
        "title": "" if session.source in TITLED_ELSEWHERE else session.title,
        "last_stamp": session.last_stamp,
    }


# Sources whose titles are not in the session file. Such a title is attached
# when an entry is made (`Entry.name`) and never stored with the parsed
# session: it changes without the session file changing. Codex keeps them in
# `session_index.jsonl`, agy in `conversation_summaries.db`.
TITLED_ELSEWHERE = ("Codex", "agy")

# Without an index: titles file -> ((size, mtime_ns) when read, its titles).
_TITLES_READ: dict[str, tuple[tuple[int, int] | None, dict[str, str]]] = {}


def file_stamp(location: str) -> tuple[int, int] | None:
    """Size and mtime of a file, the two facts that say whether it changed; None when missing.

    A SQLite database (agy's titles) may hold its latest writes in a `-wal`
    file next to it: that file's size and mtime are folded in.
    """
    try:
        stat = os.stat(location)
    except OSError:
        return None
    size, mtime_ns = stat.st_size, stat.st_mtime_ns
    if location.endswith(".db"):
        try:
            wal = os.stat(location + "-wal")
        except OSError:
            pass
        else:
            size, mtime_ns = size + wal.st_size, max(mtime_ns, wal.st_mtime_ns)
    return size, mtime_ns


def external_titles(location: str) -> dict[str, str]:
    """Session id -> title from a titles file (redacted), read again only when it changed."""
    if CACHE is not None:
        return CACHE.titles(location)
    stamp = file_stamp(location)
    known = _TITLES_READ.get(location)
    if known is None or known[0] != stamp:
        from .titles import read_titles

        known = _TITLES_READ[location] = (stamp, read_titles(location) if stamp else {})
    return known[1]


def session_from_record(path: Path, record: dict) -> Session:
    session = Session(
        source=record["source"],
        session_id=record["session_id"],
        project=record["project"],
        path=path,
        mtime=float(record["mtime"]),
        kind=record["kind"],
        parent_id=record["parent_id"],
        cwd=record["cwd"],
        title=record.get("title", ""),
    )
    session.last_stamp = record.get("last_stamp", "")
    # Stored text is already redacted; append() skips the second pass.
    for role, text, stamp in record["turns"]:
        session.append(role, text, stamp)
    session.tools = [(int(after), text) for after, text in record.get("tools", [])]
    session.tool_cuts = {int(place): int(cut) for place, cut in record.get("tool_cuts", ())}
    return session


CACHE: SessionIndex | None = None


def cache_path(home: Path) -> Path | None:
    """Index file for this set of history roots, or None when disabled."""
    configured = os.environ.get("FIND_SESSION_CACHE", "")
    if configured.lower() in {"0", "off", "no", "false", "none"}:
        return None
    if configured:
        base = Path(configured).expanduser()
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache").expanduser() / "find-session"
    claude_root, codex_root = roots(home)
    # The agy root is not part of the name: adding it would have left every
    # existing index behind under its old name.
    key = "\n".join(str(path.expanduser().resolve()) for path in [claude_root, codex_root, *omp_roots(home)])
    # Only a name for the file: every row is keyed by the full path of its
    # session file, so two sets of roots sharing a name would still be correct.
    import zlib

    digest = f"{zlib.crc32(key.encode('utf-8', 'surrogatepass')):08x}"
    # One file per interpreter version: stored records are marshal data, which
    # is version-specific, and two Pythons on one machine must not keep
    # rebuilding each other's index.
    return base / f"index-{digest}-py{sys.version_info[0]}{sys.version_info[1]}.sqlite3"
