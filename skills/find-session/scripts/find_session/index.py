"""The index: parsed sessions in one SQLite file, one row per session file.

Parsing every session file on each search costs seconds per gigabyte of
history, while the conversation text is a few percent of it. The index keeps,
per session file:

- `sessions`: a few facts (source, id, kind, project, times, message count) and
  the file's size and mtime. A search reads this small table whole.
- `texts`: the folded search text (see `searchtext.py`), read for the sessions
  that pass the filters and scanned for the query words.
- `records`: the conversation as parsed (compressed), read only for sessions
  that are printed (search results, `inspect`).
- `checkpoints`: where the parser stopped, so that a file that has grown is
  read from there instead of from the start (see `cache.resumable`).

Titles kept outside the session files (Codex thread names) have two tables of
their own, `title_files` and `titles`: the titles file is read again when its
size or mtime changes, and no session row depends on it, so a renamed thread
costs one small file and no parsing.

A row is valid while its file's size and mtime are unchanged; a changed, new
or unknown file is parsed and its rows are replaced, one session at a time.
Rows are never updated in place and ids are not reused (not even after a
rebuild), so a search running next to a writer reads either a session's old
rows or finds them gone and parses the file itself. All stored text went through redaction when it was
parsed. The file is created owner-only; a missing, corrupt or foreign-version
index is rebuilt.
"""

from __future__ import annotations

import marshal
import os
import sqlite3
import sys
import time
import zlib
from pathlib import Path

from .cache import (
    PARSER_VERSION,
    RESUMABLE_SOURCES,
    TITLED_ELSEWHERE,
    Entry,
    file_stamp,
    parse_candidate,
    resumable,
    session_from_record,
    session_record,
)
from .model import Candidate, Session
from .searchtext import SearchText

if False:  # typing only
    from typing import Iterable, Iterator

# Bump when the tables change; like PARSER_VERSION, it makes old indexes rebuild.
INDEX_SCHEMA = 4

# Records are marshal data, which is tied to the interpreter version.
FORMAT = ".".join(str(part) for part in (INDEX_SCHEMA, PARSER_VERSION, marshal.version, *sys.version_info[:2]))

# The index file and the files SQLite keeps next to it, as suffixes of its name.
# `npx find-session uninstall` (npm/find-session.js, CACHE_FILE) deletes exactly
# these and the older formats below; a test holds the two together.
COMPANIONS = ("", "-wal", "-shm", "-journal")

# How long a writer waits for another process that is writing.
BUSY_SECONDS = 10


def is_lock_text(message: str) -> bool:
    return "locked" in message or "busy" in message


def is_lock_error(error: Exception) -> bool:
    return is_lock_text(str(error).lower())


# How much of the file SQLite may map instead of reading it page by page.
MAPPED_BYTES = 1 << 30

# Search texts are read this many sessions per query (SQLite allows at least
# 999 parameters per statement).
TEXT_BATCH = 500

# A first run parses for minutes: what it has parsed is written after this many
# sessions or seconds, whichever comes first, so an interrupted run (an agent's
# command timeout) is not lost, the next run continues from there, and other
# processes can use what is done.
WRITE_EVERY = 100
WRITE_SECONDS = 5.0

# Parsing that takes longer than this says so on stderr (never on stdout, which
# stays parseable): the first line after PROGRESS_AFTER seconds, then one every
# PROGRESS_EVERY. $FIND_SESSION_PROGRESS sets the first delay, or "off".
PROGRESS_AFTER = 2.0
PROGRESS_EVERY = 10.0


class Progress:
    """Lines on stderr while many session files are parsed."""

    def __init__(self, clock=time.monotonic, stream=None):
        self.clock = clock
        self.stream = stream
        self.started = clock()
        self.total = 0  # files this run looks at, when it looks at a list of them
        self.position = 0  # how many of them it has got to
        self.parsed = 0
        self.lines = 0
        configured = os.environ.get("FIND_SESSION_PROGRESS", "").strip().lower()
        self.after = PROGRESS_AFTER
        if configured in {"0", "off", "no", "false", "none"}:
            self.after = float("inf")
        elif configured:
            try:
                self.after = float(configured)
            except ValueError:
                pass
        self.next = self.started + self.after

    def say(self, text: str) -> None:
        print(f"find-session: {text}", file=self.stream or sys.stderr, flush=True)
        self.lines += 1

    def parsed_one(self) -> None:
        self.parsed += 1
        now = self.clock()
        if now < self.next:
            return
        self.next = now + PROGRESS_EVERY
        where = f"{self.position}/{self.total} session files" if self.total else f"{self.parsed} session files parsed"
        if self.lines:
            self.say(f"indexing, {where}…")
        else:
            self.say(f"indexing your history, {where} (one-time; if this is interrupted, run it again: it continues)…")

    def done(self, *, interrupted: bool = False) -> None:
        if not self.lines:
            return
        took = f"{self.clock() - self.started:.0f} s"
        if interrupted:
            where = f"{self.position} of {self.total}" if self.total else f"{self.parsed}"
            self.say(f"stopped after {where} session files ({took}); they are kept.")
        else:
            self.say(f"index ready, {self.parsed} session files parsed in {took}.")
        self.lines = 0


SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT) WITHOUT ROWID;
CREATE TABLE sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    identity TEXT NOT NULL,
    source TEXT NOT NULL,
    session_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    parent_id TEXT NOT NULL,
    project TEXT NOT NULL,
    cwd TEXT NOT NULL,
    mtime REAL NOT NULL,
    started TEXT NOT NULL,
    messages INTEGER NOT NULL,
    first_user TEXT NOT NULL,
    title TEXT NOT NULL
);
CREATE TABLE texts (id INTEGER PRIMARY KEY, layout BLOB NOT NULL, low BLOB NOT NULL);
CREATE TABLE records (id INTEGER PRIMARY KEY, record BLOB NOT NULL);
CREATE TABLE checkpoints (id INTEGER PRIMARY KEY, state BLOB NOT NULL);
CREATE TABLE title_files (path TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL) WITHOUT ROWID;
CREATE TABLE titles (
    path TEXT NOT NULL,
    session_id TEXT NOT NULL,
    title TEXT NOT NULL,
    PRIMARY KEY (path, session_id)
) WITHOUT ROWID;
"""

TABLES = ("meta", "sessions", "texts", "records", "checkpoints", "title_files", "titles")

ROW_COLUMNS = (
    "id, path, size, mtime_ns, identity, source, session_id, kind, parent_id, "
    "project, cwd, mtime, started, messages, first_user, title"
)
(
    ROW_ID,
    ROW_PATH,
    ROW_SIZE,
    ROW_MTIME_NS,
    ROW_IDENTITY,
    ROW_SOURCE,
    ROW_SESSION_ID,
    ROW_KIND,
    ROW_PARENT_ID,
    ROW_PROJECT,
    ROW_CWD,
    ROW_MTIME,
    ROW_STARTED,
    ROW_MESSAGES,
    ROW_FIRST_USER,
    ROW_TITLE,
) = range(16)


def pack(record: dict) -> bytes:
    """A parsed conversation as stored: it is read for a handful of sessions per
    search and written whenever a live session grows, so it is kept small."""
    return zlib.compress(marshal.dumps(record), 1)


def unpack(stored: bytes) -> dict:
    return marshal.loads(zlib.decompress(stored))


def identity(candidate: Candidate) -> str:
    """How discovery attributed the file; a different attribution is a different session."""
    return "\x1f".join((candidate.source, candidate.kind, candidate.session_id, candidate.parent_id))


def remove_older_formats(directory: Path) -> None:
    """Delete caches written by earlier versions (JSON, then one marshal file).

    They hold conversation text and are never read again; the first of them was
    written before redaction existed.
    """
    try:
        names = os.listdir(directory)
    except OSError:
        return
    for name in names:
        # The last one is a half-written file left by an interrupted save.
        if name.startswith(("sessions-", ".sessions-")) and name.endswith((".json", ".bin", ".tmp")):
            try:
                os.unlink(directory / name)
            except OSError:
                pass


class SessionIndex:
    def __init__(self, path: Path):
        self.path = path
        self.db: sqlite3.Connection | None = None
        # Could not be opened or created: every file is parsed, nothing is saved.
        self.unusable = ""
        # Found damaged while reading: rebuilt from scratch when saving.
        self.damaged = False
        self.rows: dict[str, tuple | None] = {}
        self.complete = False
        # Files known to exist in this run (seen by discovery or asked for).
        self.present: set[str] = set()
        # Parsed in this run: location -> (size, mtime_ns, identity, entry).
        self.parsed: dict[str, tuple[int, int, str, Entry]] = {}
        self.unsaved: set[str] = set()
        # Titles files asked for in this run: location -> its titles, and
        # location -> (size, mtime_ns) as it was then (None: no such file).
        self.title_sets: dict[str, dict[str, str]] = {}
        self.title_stamps: dict[str, tuple[int, int] | None] = {}
        # Those whose stored titles have to be replaced when saving.
        self.titles_read: set[str] = set()
        self.progress = Progress()
        self.written = self.progress.started
        remove_older_formats(path.parent)

    # --- reading -------------------------------------------------------------

    def entry(self, candidate: Candidate) -> Entry:
        """The session in `candidate`'s file: from the index when the file is
        unchanged, parsed otherwise. Raises OSError when it cannot be read."""
        location = candidate.location
        stat = candidate.stat or os.stat(location)
        self.present.add(location)
        who = identity(candidate)
        known = self.parsed.get(location)
        if known is not None and known[:3] == (stat.st_size, stat.st_mtime_ns, who):
            return known[3]
        row = self.row(location)
        if (
            row is not None
            and row[ROW_SIZE] == stat.st_size
            and row[ROW_MTIME_NS] == stat.st_mtime_ns
            and row[ROW_IDENTITY] == who
        ):
            entry = Entry(candidate)
            entry._index = self
            entry._row = row
            entry.source = row[ROW_SOURCE]
            entry.session_id = row[ROW_SESSION_ID]
            entry.kind = row[ROW_KIND]
            entry.parent_id = row[ROW_PARENT_ID]
            entry.project = row[ROW_PROJECT]
            entry.cwd = row[ROW_CWD]
            entry.mtime = row[ROW_MTIME]
            entry.started = row[ROW_STARTED]
            entry.messages = row[ROW_MESSAGES]
            entry.first_user = row[ROW_FIRST_USER]
            entry.name(row[ROW_TITLE])
            return entry
        entry = Entry(candidate)
        entry._index = self
        self.reparse(entry, stat)
        return entry

    def entries(self, candidates: Iterable[Candidate]) -> Iterator[Entry]:
        self.load_all()
        candidates = list(candidates)
        progress = self.progress
        progress.total += len(candidates)
        for candidate in candidates:
            progress.position += 1
            try:
                yield self.entry(candidate)
            except OSError:
                continue

    def reparse(self, entry: Entry, stat: os.stat_result | None = None) -> None:
        candidate = entry.candidate
        if stat is None:
            stat = os.stat(candidate.location)
        entry.adopt(parse_candidate(candidate, resume=self.earlier_parse(candidate)))
        self.parsed[candidate.location] = (stat.st_size, stat.st_mtime_ns, identity(candidate), entry)
        self.unsaved.add(candidate.location)
        self.progress.parsed_one()
        if len(self.unsaved) >= WRITE_EVERY or self.progress.clock() - self.written >= WRITE_SECONDS:
            # Also when the write fails (another process is writing): try again after the same pause.
            self.written = self.progress.clock()
            self.write()

    def earlier_parse(self, candidate: Candidate) -> Session | None:
        """The stored parse of a file that has changed, when the parser can go on from it."""
        if candidate.source not in RESUMABLE_SOURCES:
            return None
        row = self.row(candidate.location)
        if row is None or row[ROW_IDENTITY] != identity(candidate):
            return None
        found = self.query(
            "SELECT c.state, r.record FROM checkpoints c JOIN records r ON r.id = c.id WHERE c.id = ?",
            (row[ROW_ID],),
        )
        if not found:
            return None
        try:
            return resumable(candidate.path, unpack(found[0][1]), marshal.loads(found[0][0]))
        except (KeyError, TypeError, ValueError, EOFError, zlib.error):
            return None

    def row(self, location: str) -> tuple | None:
        if location in self.rows or self.complete:
            return self.rows.get(location)
        found = self.query(f"SELECT {ROW_COLUMNS} FROM sessions WHERE path = ?", (location,))
        self.rows[location] = found[0] if found else None
        return self.rows[location]

    def load_all(self) -> None:
        if self.complete:
            return
        self.rows = {row[ROW_PATH]: row for row in self.query(f"SELECT {ROW_COLUMNS} FROM sessions")}
        self.complete = True

    def stored_parent(self, candidate: Candidate) -> str | None:
        """Parent id last recorded for a file, however old; None when unknown."""
        known = self.parsed.get(candidate.location)
        if known is not None:
            return known[3].parent_id
        self.load_all()
        row = self.rows.get(candidate.location)
        return None if row is None else row[ROW_PARENT_ID]

    def load_session(self, entry: Entry) -> None:
        row = entry._row
        found = self.query(
            "SELECT r.record FROM sessions s JOIN records r ON r.id = s.id "
            "WHERE s.id = ? AND s.size = ? AND s.mtime_ns = ?",
            (row[ROW_ID], row[ROW_SIZE], row[ROW_MTIME_NS]),
        )
        if found:
            try:
                entry._session = session_from_record(entry.candidate.path, unpack(found[0][0]))
                return
            except (KeyError, TypeError, ValueError, EOFError, zlib.error):
                pass
        # Replaced or removed by another process since this run read the row.
        self.reparse(entry)

    def load_text(self, entry: Entry) -> SearchText | None:
        return self.texts([entry]).get(entry._row[ROW_ID])

    def texts(self, batch: list[Entry]) -> dict[int, SearchText]:
        wanted = {entry._row[ROW_ID]: entry._row for entry in batch}
        found: dict[int, SearchText] = {}
        if not wanted:
            return found
        marks = ",".join("?" * len(wanted))
        for row_id, size, mtime_ns, layout, low in self.query(
            "SELECT s.id, s.size, s.mtime_ns, t.layout, t.low FROM sessions s JOIN texts t ON t.id = s.id "
            f"WHERE s.id IN ({marks})",
            tuple(wanted),
        ):
            row = wanted[row_id]
            if size == row[ROW_SIZE] and mtime_ns == row[ROW_MTIME_NS]:
                try:
                    found[row_id] = SearchText.stored(layout, low)
                except (TypeError, ValueError, EOFError):
                    pass
        return found

    def with_text(self, entries: Iterable[Entry]) -> Iterator[tuple[Entry, SearchText]]:
        batch: list[Entry] = []
        for entry in entries:
            batch.append(entry)
            if len(batch) >= TEXT_BATCH:
                yield from self.batch_with_text(batch)
                batch = []
        yield from self.batch_with_text(batch)

    def batch_with_text(self, batch: list[Entry]) -> Iterator[tuple[Entry, SearchText]]:
        stored = self.texts([entry for entry in batch if entry._row is not None])
        for entry in batch:
            text = stored.pop(entry._row[ROW_ID], None) if entry._row is not None else None
            if text is None:
                if entry._row is not None:
                    try:
                        self.reparse(entry)
                    except OSError:
                        continue
                text = entry.search_text()
            yield entry, text

    def titles(self, location: str) -> dict[str, str]:
        """Session id -> title from a titles file: stored ones while the file is
        unchanged, read again (and stored when saving) otherwise."""
        known = self.title_sets.get(location)
        if known is not None:
            return known
        stamp = file_stamp(location)
        stored = self.query("SELECT size, mtime_ns FROM title_files WHERE path = ?", (location,))
        if stored and stored[0] == stamp:
            known = dict(self.query("SELECT session_id, title FROM titles WHERE path = ?", (location,)))
        else:
            known = {}
            if stamp is not None:
                from .titles import read_codex_titles

                known = read_codex_titles(location)
            if stamp is not None or stored:
                self.titles_read.add(location)
        self.title_sets[location] = known
        self.title_stamps[location] = stamp
        return known

    def query(self, sql: str, parameters: tuple = ()) -> list[tuple]:
        """Rows of a read; nothing when the index cannot be read (callers then parse)."""
        db = self.open()
        if db is None or self.damaged:
            return []
        try:
            return db.execute(sql, parameters).fetchall()
        except sqlite3.OperationalError:
            return []
        except sqlite3.DatabaseError:
            self.damaged = True
            return []

    # --- opening -------------------------------------------------------------

    def open(self) -> sqlite3.Connection | None:
        if self.db is not None or self.unusable:
            return self.db
        try:
            try:
                self.db = self.connect_patiently()
            except sqlite3.OperationalError:
                raise
            except sqlite3.DatabaseError:
                # Not an index (damaged, or something else under this name).
                self.remove_files()
                self.db = self.connect()
        except (sqlite3.Error, OSError) as error:
            self.unusable = str(error) or type(error).__name__
        return self.db

    def connect_patiently(self) -> sqlite3.Connection:
        """connect(), tried again while another process holds the file.

        SQLite's own busy timeout does not cover every case: switching a new
        file to WAL, or a writer that would deadlock, fails at once with
        "database is locked" when several agents start at the same moment.
        """
        deadline = time.monotonic() + BUSY_SECONDS
        pause = 0.02
        while True:
            try:
                return self.connect()
            except sqlite3.OperationalError as error:
                if not is_lock_error(error) or time.monotonic() >= deadline:
                    raise
            time.sleep(pause)
            pause = min(pause * 2, 0.5)

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Created here, owner-only, so SQLite never creates it with the umask's
        # mode; its -wal and -shm companions inherit the mode of this file.
        try:
            os.close(os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600))
        except FileExistsError:
            pass
        db = sqlite3.connect(str(self.path), timeout=BUSY_SECONDS, isolation_level=None)
        try:
            # WAL: searches keep reading while another process writes.
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            # Reading the texts through a memory map halves the time it takes.
            db.execute(f"PRAGMA mmap_size={MAPPED_BYTES}")
            # Text of a deleted or replaced session is overwritten, not left in free pages.
            db.execute("PRAGMA secure_delete=ON")
            if self.stored_format(db) != FORMAT:
                self.create_tables(db)
        except BaseException:
            db.close()
            raise
        return db

    @staticmethod
    def stored_format(db: sqlite3.Connection) -> str | None:
        try:
            found = db.execute("SELECT value FROM meta WHERE key = 'format'").fetchone()
        except sqlite3.OperationalError as error:
            if "no such table" in str(error):
                return None
            raise
        return found[0] if found else None

    def create_tables(self, db: sqlite3.Connection) -> None:
        db.execute("BEGIN IMMEDIATE")
        try:
            # Another process may have rebuilt it while this one waited.
            if self.stored_format(db) != FORMAT:
                # Ids go on from where they were: a search that is running
                # while the index is rebuilt must never find another session
                # under an id it read before.
                try:
                    last = db.execute("SELECT seq FROM sqlite_sequence WHERE name = 'sessions'").fetchone()
                except sqlite3.OperationalError:
                    last = None
                for table in TABLES:
                    db.execute(f"DROP TABLE IF EXISTS {table}")
                for statement in SCHEMA.split(";"):
                    if statement.strip():
                        db.execute(statement)
                db.execute("INSERT INTO meta (key, value) VALUES ('format', ?)", (FORMAT,))
                if last:
                    db.execute("INSERT INTO sqlite_sequence (name, seq) VALUES ('sessions', ?)", (last[0],))
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise

    def remove_files(self) -> None:
        for suffix in COMPANIONS:
            try:
                os.unlink(f"{self.path}{suffix}")
            except FileNotFoundError:
                pass

    # --- writing -------------------------------------------------------------

    def save(self, *, interrupted: bool = False) -> None:
        """Write what this run parsed and drop rows of files that are gone."""
        if self.damaged:
            # Start over; everything parsed in this run is written again.
            if self.db is not None:
                self.db.close()
                self.db = None
            try:
                self.remove_files()
            except OSError as error:
                self.unusable = str(error)
            self.damaged = False
            self.complete = False
            self.rows = {}
            self.unsaved = set(self.parsed)
            self.titles_read = {location for location, stamp in self.title_stamps.items() if stamp is not None}
        # Drop rows only for files that are gone: a run that looked at one
        # source (or none) must not evict the others.
        gone = (
            [
                location
                for location, row in self.rows.items()
                if row is not None and location not in self.present and not os.path.exists(location)
            ]
            if self.complete
            else []
        )
        if self.unsaved or gone or self.titles_read:
            self.open()
            error = self.unusable or self.write(gone)
            deadline = time.monotonic() + BUSY_SECONDS
            pause = 0.02
            while error and not self.unusable and is_lock_text(error) and time.monotonic() < deadline:
                # Same reason as connect_patiently(): try again instead of losing the parse.
                time.sleep(pause)
                pause = min(pause * 2, 0.5)
                error = self.write(gone)
            if error:
                print(f"find-session: cache not saved ({error})", file=sys.stderr)
        self.progress.done(interrupted=interrupted)
        if self.db is not None:
            self.db.close()
            self.db = None

    def write(self, gone: list[str] | tuple = ()) -> str:
        """Replace the rows of sessions parsed in this run; '' or why it failed."""
        db = self.open()
        if db is None:
            return self.unusable
        if self.damaged:
            return ""  # save() rebuilds
        try:
            db.execute("BEGIN IMMEDIATE")
            try:
                for location in gone:
                    self.delete(db, location)
                self.write_titles(db)
                for location in sorted(self.unsaved):
                    size, mtime_ns, who, entry = self.parsed[location]
                    session = entry.session()
                    text = entry.search_text()
                    self.delete(db, location)
                    row_id = db.execute(
                        f"INSERT INTO sessions ({ROW_COLUMNS}) "
                        "VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            location,
                            size,
                            mtime_ns,
                            who,
                            entry.source,
                            entry.session_id,
                            entry.kind,
                            entry.parent_id,
                            entry.project,
                            entry.cwd,
                            entry.mtime,
                            entry.started,
                            entry.messages,
                            entry.first_user,
                            "" if entry.source in TITLED_ELSEWHERE else entry.title,
                        ),
                    ).lastrowid
                    db.execute(
                        "INSERT INTO texts (id, layout, low) VALUES (?, ?, ?)", (row_id, text.layout(), text.low)
                    )
                    db.execute(
                        "INSERT INTO records (id, record) VALUES (?, ?)",
                        (row_id, pack(session_record(session))),
                    )
                    if session.checkpoint is not None:
                        db.execute(
                            "INSERT INTO checkpoints (id, state) VALUES (?, ?)",
                            (row_id, marshal.dumps(session.checkpoint)),
                        )
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        except sqlite3.OperationalError as error:
            # Busy for too long, read-only, disk full: parsed again next time.
            return str(error)
        except (sqlite3.Error, ValueError, UnicodeError) as error:
            self.damaged = True
            return str(error)
        for location in self.unsaved:
            # The text is in the index now; keep only what is cheap to keep.
            self.parsed[location][3]._text = None
        self.unsaved.clear()
        self.written = self.progress.clock()
        self.titles_read.clear()
        if gone:
            for location in gone:
                self.rows.pop(location, None)
            try:
                # So that the removed text does not linger in the write-ahead log.
                db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
            except sqlite3.Error:
                pass
        return ""

    def write_titles(self, db: sqlite3.Connection) -> None:
        """Replace the titles of files read in this run; forget files that are gone."""
        replaced = {location: self.title_stamps[location] for location in self.titles_read}
        for (location,) in db.execute("SELECT path FROM title_files").fetchall():
            if location not in self.title_sets and not os.path.exists(location):
                replaced[location] = None
        for location, stamp in replaced.items():
            db.execute("DELETE FROM titles WHERE path = ?", (location,))
            db.execute("DELETE FROM title_files WHERE path = ?", (location,))
            if stamp is None:
                continue
            db.execute("INSERT INTO title_files (path, size, mtime_ns) VALUES (?, ?, ?)", (location, *stamp))
            db.executemany(
                "INSERT INTO titles (path, session_id, title) VALUES (?, ?, ?)",
                [(location, session_id, title) for session_id, title in self.title_sets[location].items()],
            )

    @staticmethod
    def delete(db: sqlite3.Connection, location: str) -> None:
        for (row_id,) in db.execute("SELECT id FROM sessions WHERE path = ?", (location,)).fetchall():
            db.execute("DELETE FROM texts WHERE id = ?", (row_id,))
            db.execute("DELETE FROM records WHERE id = ?", (row_id,))
            db.execute("DELETE FROM checkpoints WHERE id = ?", (row_id,))
            db.execute("DELETE FROM sessions WHERE id = ?", (row_id,))
