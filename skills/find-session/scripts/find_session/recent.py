"""The recent, index and sources commands."""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime
from pathlib import Path

from .cache import Entry, entries, latest_copies, note_present
from .config import config_path
from .discovery import agy_root, discover, omp_roots, roots
from .model import MAX_ASSISTANT_BRIEF, MAX_USER_BRIEF, SOURCES, Candidate, Session
from .text import compact_text

# `index` prints this many sessions unless told otherwise: a whole history is
# hundreds of kilobytes, which no agent should get by default.
INDEX_LIMIT = 50


def build_index(home: Path, out: Path | None, *, limit: int | None = None, since: datetime | None = None) -> int:
    """One line per main session, newest first: a table of contents.

    On stdout the newest INDEX_LIMIT sessions; written to a file, all of them
    (it is there to be searched with other tools). `limit` overrides both, 0
    meaning all.
    """
    from .output import short_ids
    from .timefilter import in_window

    rows: list[Entry] = []
    counts = {source: 0 for source in SOURCES}
    known: dict[str, list[str]] = {}
    candidates = discover(home)
    note_present(candidates)
    candidates = latest_copies(candidates)
    # Parsed in full even without the index: a title may be anywhere in the file.
    for session in entries(candidates):
        known.setdefault(session.source, []).append(session.session_id)
        if session.kind != "main" or not session.messages:
            continue
        counts[session.candidate.source] += 1
        if since is None or in_window(session, since, None):
            rows.append(session)
    rows.sort(key=lambda item: item.mtime, reverse=True)
    to_file = out is not None and str(out) != "-"
    if limit is None:
        limit = 0 if to_file else INDEX_LIMIT
    shown = rows[:limit] if limit > 0 else rows
    per_source = ", ".join(f"{source} {counts[source]}" for source in SOURCES)
    period = f" since {since:%Y-%m-%d %H:%M}" if since else ""
    cut = len(shown) < len(rows)
    more = "; --limit N lists more (0 = all), --since WHEN a period" if cut else ""
    listed = f"the newest {len(shown)} of {len(rows)} sessions" if cut else counted(len(rows), "session")
    summary = f"{listed}{period}{'' if cut else ', newest first'} (in all: {per_source})"
    short = short_ids(known, [(session.source, session.session_id) for session in shown])
    lines = [f"index: {summary}{more}"]
    for session in shown:
        selector = f"{session.source.lower()}:{short.get((session.source, session.session_id), session.session_id)}"
        lines.append(
            f"{selector} {datetime.fromtimestamp(session.mtime):%Y-%m-%d %H:%M} {session.project} · "
            f"{session.messages} msgs · {compact_text(session.title or session.first_user)}"
        )
    text = "\n".join(lines) + "\n"
    if not to_file:
        sys.stdout.write(text)
        return 0
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text)
    print(f"{out.name}: {summary} -> {out}")
    return 0


def recent_sessions(home: Path, hours: int, limit: int) -> tuple[list[Session], int]:
    """The newest main sessions active in the last `hours`, and how many there are in all."""
    cutoff = time.time() - hours * 3600
    candidates = []
    for candidate in discover(home):
        try:
            # A file is never older than its last record, so an old file needs no look inside.
            if (candidate.stat or os.stat(candidate.location)).st_mtime >= cutoff:
                candidates.append(candidate)
        except OSError:
            continue
    note_present(candidates)
    candidates = latest_copies(candidates)
    # The session's own time decides, not the file's: a copied history is all "new" files.
    found = [
        entry for entry in entries(candidates) if entry.kind == "main" and entry.mtime >= cutoff and entry.first_user
    ]
    found.sort(key=lambda entry: entry.mtime, reverse=True)
    sessions: list[Session] = []
    for entry in found:
        if len(sessions) >= limit:
            break
        try:
            sessions.append(entry.session())
        except OSError:
            continue
    return sessions, len(found)


def emit_file(label: str, path: Path, max_chars: int = 7000) -> None:
    try:
        text = path.read_text(errors="replace").strip()
    except OSError:
        return
    if text:
        print(f"\n### {label}\n\n{text[:max_chars]}")


def include_globs(config: dict, extra: list[str] | None) -> list[str]:
    configured = config.get("recent_include")
    globs = [g for g in configured if isinstance(g, str)] if isinstance(configured, list) else []
    return [*globs, *(extra or [])]


def build_recent(
    home: Path,
    hours: int,
    limit: int,
    *,
    include: list[str] | None = None,
) -> int:
    """Recent activity across all harnesses, e.g. as input for a daily brief.

    `include` globs (relative to `home`) append extra context files, such as
    project state notes; nothing outside the session history is read otherwise.
    """
    print(f"# Recent activity, collected {datetime.now():%Y-%m-%d %H:%M}")
    sessions, total = recent_sessions(home, hours, limit)
    more = f": the newest {len(sessions)} of {total} sessions (--limit N for more)" if total > len(sessions) else ""
    print(f"\n## Last {hours}h (Claude Code + Codex + omp + agy){more}")
    if not sessions:
        print("\n(no sessions in this window)")
    for session in sessions:
        if not session.user_messages:
            continue
        print(
            f"\n### {datetime.fromtimestamp(session.mtime):%Y-%m-%d %H:%M} · "
            f"{session.source} · {session.project} · {session.session_id}"
        )
        if session.title:
            print(f"- title: {session.title}")
        print(f"- start: {compact_text(session.user_messages[0], MAX_USER_BRIEF)}")
        if len(session.user_messages) > 1:
            print(f"- last request: {compact_text(session.user_messages[-1], MAX_USER_BRIEF)}")
        if session.assistant_messages:
            print(f"- last agent result: {compact_text(session.assistant_messages[-1], MAX_ASSISTANT_BRIEF)}")
        print(f"- user messages: {len(session.user_messages)}")

    seen: set[Path] = set()
    files: list[Path] = []
    for pattern in include or []:
        try:
            matched = sorted(home.glob(pattern))
        except (ValueError, NotImplementedError):  # absolute or invalid pattern
            matched = []
        for path in matched:
            if path.is_file() and path not in seen:
                seen.add(path)
                files.append(path)
    if files:
        print("\n\n## Included files")
        for path in files:
            try:
                label = str(path.relative_to(home))
            except ValueError:
                label = str(path)
            emit_file(label, path)
    return 0


def codex_kind_of(candidate: Candidate) -> str:
    """main, child or review for a Codex file, from the record at its start.

    Codex keeps subagents and review threads next to user conversations and
    tells them apart only inside the file. `sources` runs before any index
    exists (the installers show it first), so it looks itself: one record per file.
    """
    from .jsonl import iter_json_lines
    from .parsers.codex import codex_kind

    try:
        for position, obj in enumerate(iter_json_lines(candidate.path)):
            if obj.get("type") == "session_meta":
                payload = obj.get("payload")
                return codex_kind(payload)[0] if isinstance(payload, dict) else "main"
            if position >= 4:
                break
    except OSError:
        pass
    return "main"


def agy_kind_of(candidate: Candidate) -> str:
    """main or child for an agy conversation: agy names a subagent's parent in its summaries database."""
    from .titles import agy_summaries

    summary = agy_summaries(candidate.titles).get(candidate.session_id)
    return "child" if summary and summary[2] else "main"


def counted(number: int, noun: str) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


def describe_sources(home: Path, *, as_json: bool = False) -> int:
    """Diagnostic: where each source is read from and how many session files it has."""
    claude_root, codex_root = roots(home)
    kinds = {source: {"main": 0, "child": 0, "review": 0} for source in SOURCES}
    for candidate in discover(home, include_children=True):
        if candidate.source == "Codex":
            kind = codex_kind_of(candidate)
        elif candidate.source == "agy":
            kind = agy_kind_of(candidate)
        else:
            kind = candidate.kind
        kinds[candidate.source][kind] += 1
    report: dict[str, dict] = {}
    for source, source_roots in (
        ("Claude", [claude_root]),
        ("Codex", [codex_root]),
        ("omp", omp_roots(home)),
        ("agy", [agy_root(home)]),
    ):
        report[source] = {
            "roots": [{"path": str(root), "exists": root.is_dir()} for root in source_roots],
            "main": kinds[source]["main"],
            "children": kinds[source]["child"],
            "reviews": kinds[source]["review"],
        }
    if as_json:
        import json

        print(json.dumps(report, indent=2))
        return 0
    from .text import home_relative

    for source, info in report.items():
        reviews = f", {counted(info['reviews'], 'review thread')}" if info["reviews"] else ""
        print(f"{source}: {counted(info['main'], 'session')}, {counted(info['children'], 'subagent')}{reviews}")
        for root in info["roots"]:
            state = "ok" if root["exists"] else "missing"
            print(f"  {home_relative(root['path'])} ({state})")
    config = home_relative(str(config_path()))
    print(f"Config: {config}" if config_path().is_file() else f"Config: none ({config} is read when it exists)")
    return 0


def build_search_index(home: Path) -> int:
    """Parse whatever is new or changed, so that the next search finds the index ready."""
    from . import cache
    from .text import home_relative

    if cache.CACHE is None:
        print("The index is turned off (--no-cache or FIND_SESSION_CACHE=off): nothing to build.")
        return 0
    candidates = discover(home, include_children=True)
    note_present(candidates)
    started = time.monotonic()
    counts = {source: 0 for source in SOURCES}
    for entry in entries(candidates):
        counts[entry.candidate.source] += 1
    parsed = len(cache.CACHE.parsed)
    summary = ", ".join(f"{source} {counts[source]}" for source in SOURCES)
    work = f"{parsed} parsed in {time.monotonic() - started:.0f} s" if parsed else "all up to date"
    print(f"Index: {sum(counts.values())} session files ({summary}), {work}")
    print(f"  {home_relative(str(cache.CACHE.path))}")
    return 0
