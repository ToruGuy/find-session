"""Search results as compact text, full text or JSON records."""

from __future__ import annotations

import os
import shlex
from bisect import bisect_left
from datetime import datetime
from pathlib import Path

from .model import MAX_INDEX_TEXT, Session
from .ranking import CHUNK_MESSAGES, Hit, match_snippet
from .searchtext import find_word, fold
from .text import MAX_HIT_TEXT, compact_text, home_relative, local_time
from .timefilter import activity_window

if False:  # typing only
    from typing import Iterable

# The script agents are told to call; printed in hints so they are copy-pasteable.
# In a clone the short scripts/sessions.py at the repository root; in a copied skill
# folder the sessions.py next to the package.
_SKILL_ENTRY = Path(__file__).resolve().parents[1] / "sessions.py"
_CLONE_ENTRY = Path(__file__).resolve().parents[4] / "scripts" / "sessions.py"
ENTRY_SCRIPT = (
    _CLONE_ENTRY if (_CLONE_ENTRY.parent.parent / "skills").is_dir() and _CLONE_ENTRY.is_file() else _SKILL_ENTRY
)
SHORT_ID = 8


def short_ids(
    known: dict[str, list[str]], wanted: Iterable[tuple[str, str]] | None = None
) -> dict[tuple[str, str], str]:
    """Shortest prefix (at least SHORT_ID chars) unique among a source's ids.

    `wanted` limits the work to the (source, id) pairs that will be printed;
    the prefixes are the same either way, because they depend on all known ids.
    """
    result: dict[tuple[str, str], str] = {}
    ordered = {source: sorted(set(ids)) for source, ids in known.items()}
    if wanted is None:
        wanted = [(source, session_id) for source, ids in ordered.items() for session_id in ids]
    for source, session_id in wanted:
        ids = ordered.get(source, [])
        index = bisect_left(ids, session_id)
        if index == len(ids) or ids[index] != session_id:
            continue
        if "/" in session_id:
            # omp subagent: parent id plus task name, kept whole.
            result[(source, session_id)] = session_id
            continue
        length = SHORT_ID
        for other in (ids[index - 1] if index else "", ids[index + 1] if index + 1 < len(ids) else ""):
            common = len(os.path.commonprefix([session_id, other]))
            length = max(length, common + 1)
        result[(source, session_id)] = session_id[:length]
    return result


def print_compact_results(
    query: str,
    filter_text: str,
    sections: list[str],
    shown: dict[str, list[tuple[Hit, float, Session]]],
    totals: dict[str, int],
    limit: int,
    hidden_children: int,
    config: dict,
    *,
    terms: list[str],
    short: dict[tuple[str, str], str],
    rarity: list[float] | None = None,
    current: str = "",
    counts: str = "",
    missing: Iterable[str] = (),
    no_history: bool = False,
) -> None:
    counts = counts or ", ".join(f"{name} {totals[name]}" for name in sections)
    hidden = f"; {hidden_children} more in subagents (--children)" if hidden_children else ""
    if not any(totals[name] for name in sections):
        print(f'search "{query}"{filter_text}: no matches ({counts}){hidden}')
        if no_history:
            print(f"no session files found: `{helper_command(short=True)} sources` shows where history is read from")
        if current:
            print(current)
        return
    absent = f"; no session has: {' '.join(missing)}" if missing else ""
    print(f'search "{query}"{filter_text}: {counts}{hidden}{absent}')
    print(f"inspect: {helper_command(short=True)} inspect SELECTOR [--range A:B]  (also prints the resume command)")
    for hint in config.get("hints") or []:
        if isinstance(hint, dict) and isinstance(hint.get("label"), str) and isinstance(hint.get("command"), str):
            print(f"{hint['label']}: {hint['command']}")
    for name in sections:
        if not totals[name]:
            continue
        count = min(limit, totals[name])
        more = f" (top {count} of {totals[name]})" if totals[name] > count else ""
        print(f"\n{name}{more}")
        for hit, _, session in shown[name]:
            print_compact_match(session, hit, terms=terms, short=short, rarity=rarity)
    if current:
        print(f"\n{current}")


def current_session_line(source: str, session_id: str, hit: Hit) -> str:
    """The calling session, left out of the ranking: its selector and where the words are."""
    where = f" hit {hit.first}:{hit.last}" if hit.last > hit.first else f" hit {hit.first}" if hit.first else ""
    return f"this session also matches: {source.lower()}:{session_id}{where}"


def print_compact_match(
    session: Session,
    hit: Hit,
    *,
    terms: list[str],
    short: dict[tuple[str, str], str],
    rarity: list[float] | None = None,
) -> None:
    sid = short.get((session.source, session.session_id), session.session_id)
    modified = datetime.fromtimestamp(session.mtime)
    total = len(session.turns)
    kind = ""
    if session.kind == "review":
        kind = " · review thread"
    elif session.kind != "main":
        kind = f" · subagent of {session.parent_id[:SHORT_ID] or '?'}"
    print(f"{session.source.lower()}:{sid} {modified:%Y-%m-%d %H:%M} {session.project} · {total} msgs{kind}")
    # One line says which conversation this is: its title when it has one
    # (shorter, and rarely shared with another session), else how it began.
    if session.title:
        visible = session.title
        print(f" title: {compact_text(visible)}")
    elif session.user_messages:
        visible = session.user_messages[0]
        print(f" first: {compact_text(visible)}")
    else:
        # A subagent's first prompt was written by its parent agent.
        visible = session.delegated_messages[0] if session.delegated_messages else ""
        print(f" task: {compact_text(visible)}")
    shown = compact_text(visible)
    long_session = total > CHUNK_MESSAGES
    folded = fold(visible)
    if long_session or not all(find_word(folded, term) >= 0 for term in terms):
        snippet = match_snippet(
            session, terms, " ".join(terms), width=MAX_HIT_TEXT, span=(hit.first, hit.last), rarity=rarity
        )
        if snippet:
            where = f"{hit.first}:{hit.last}" if hit.last > hit.first else f"{hit.first}"
            repeated = (
                not session.title
                and snippet["role"] != "tool"
                and session.turns[snippet["n"] - 1][1] == visible
                and snippet["text"].strip("…") in shown
            )
            if not repeated:
                print(f" hit {where} {ROLE_LABELS[snippet['role']].split()[0].lower()}: {snippet['text']}")
            elif long_session:
                # The line above is that very message: only where to read on is new.
                print(f" hit {where}")


def session_selector(session: Session) -> str:
    return f"{session.source.lower()}:{session.session_id}"


def resume_command(session: Session) -> str:
    """Ready-to-run resume command; empty for subagents and review threads.

    Claude Code (and omp) look sessions up per project directory, so the
    command changes into the recorded working directory first when known.
    """
    if session.kind != "main":
        return ""
    if session.source == "Codex":
        command = f"codex resume {session.session_id}"
    elif session.source == "omp":
        command = f"omp --resume {session.session_id}"
    else:
        command = f"claude --resume {session.session_id}"
    if session.cwd:
        return f"cd {shlex.quote(session.cwd)} && {command}"
    return command


def helper_command(*, short: bool = False) -> str:
    """How to call this tool again, as a copy-pasteable command.

    From a clone that is the entry script (with ~ for the home directory when
    `short`); installed as a package it is the `find-session` command. The npx
    wrapper sets FIND_SESSION_COMMAND when it runs the copy in npm's temporary cache.
    """
    if os.environ.get("FIND_SESSION_COMMAND"):
        return os.environ["FIND_SESSION_COMMAND"]
    if not ENTRY_SCRIPT.is_file():
        return "find-session"
    if short:
        helper = home_relative(str(ENTRY_SCRIPT))
        return f"python3 {helper if ' ' not in helper else shlex.quote(helper)}"
    return f"python3 {shlex.quote(str(ENTRY_SCRIPT))}"


def inspect_command(session: Session) -> str:
    return f"{helper_command()} inspect {session_selector(session)}"


class _Placeholders(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def configured_hints(session: Session, config: dict) -> list[dict]:
    """Extra per-machine hint lines from config (e.g. a command on another host)."""
    hints = config.get("hints")
    if not isinstance(hints, list):
        return []
    values = _Placeholders(
        selector=session_selector(session),
        id=session.session_id,
        source=session.source.lower(),
        cwd=shlex.quote(session.cwd) if session.cwd else "",
        resume=resume_command(session),
    )
    rendered: list[dict] = []
    for hint in hints:
        if not isinstance(hint, dict):
            continue
        label, template = hint.get("label"), hint.get("command")
        if not isinstance(label, str) or not isinstance(template, str):
            continue
        if "{resume}" in template and not values["resume"]:
            continue
        rendered.append({"label": label, "command": template.format_map(values)})
    return rendered


def resume_label(config: dict) -> str:
    label = config.get("resume_label")
    return label if isinstance(label, str) and label else "Resume"


def kind_note(session: Session) -> str:
    if session.kind == "review":
        return f"{session.source} automatic review thread"
    return f"{session.source} subagent of session {session.parent_id or 'unknown'}"


def first_prompt(session: Session) -> str:
    if session.user_messages:
        return session.user_messages[0][:MAX_INDEX_TEXT]
    if session.delegated_messages:
        return "[delegated task] " + session.delegated_messages[0][:MAX_INDEX_TEXT]
    return ""


def match_record(
    session: Session,
    hit: Hit,
    config: dict | None = None,
    *,
    terms: list[str] | None = None,
    query: str = "",
    rarity: list[float] | None = None,
) -> dict:
    started = local_time(session.started)
    record = {
        "source": session.source,
        "kind": session.kind,
        "session_id": session.session_id,
        "parent_id": session.parent_id or None,
        "selector": session_selector(session),
        "project": session.project,
        "cwd": session.cwd or None,
        "started": started.isoformat(timespec="minutes") if started else None,
        # With the UTC offset, like `started`.
        "modified": datetime.fromtimestamp(session.mtime).astimezone().isoformat(timespec="minutes"),
        "score": hit.score,
        "title": session.title or None,
        "first": first_prompt(session),
        "match": match_snippet(session, terms, query, span=(hit.first, hit.last), rarity=rarity) if terms else None,
        # The stretch of messages where the words occur together.
        "chunk": {"first": hit.first, "last": hit.last} if hit.first else None,
        "messages": len(session.turns),
        "last": session.user_messages[-1][:MAX_INDEX_TEXT] if session.user_messages else None,
        "resume": resume_command(session) or None,
        "inspect": inspect_command(session),
    }
    hints = configured_hints(session, config or {})
    if hints:
        record["hints"] = hints
    return record


def print_search_match(
    session: Session,
    hit: Hit,
    config: dict | None = None,
    *,
    terms: list[str] | None = None,
    query: str = "",
    rarity: list[float] | None = None,
) -> None:
    config = config or {}
    first = first_prompt(session)
    last = session.user_messages[-1][:MAX_INDEX_TEXT] if session.user_messages else ""
    started, modified = activity_window(session)
    since = f" | started {started:%Y-%m-%d}" if f"{started:%Y-%m-%d}" != f"{modified:%Y-%m-%d}" else ""
    print(f"\n{modified:%Y-%m-%d %H:%M} | {session.source} | {session.project} | score={hit.score}{since}")
    print(f"Session: {session.session_id}")
    if session.title:
        print(f"Title: {session.title}")
    print(f"Start: {first}")
    if last and last != first:
        print(f"Last request: {last}")
    # Only when the lines above do not already show why it matched.
    visible = fold(f"{session.title}\n{first}\n{last}")
    explained = bool(terms) and all(find_word(visible, term) >= 0 for term in terms)
    long_session = len(session.turns) > CHUNK_MESSAGES
    span = (hit.first, hit.last) if hit.first else None
    snippet = (
        match_snippet(session, terms, query, span=span, rarity=rarity)
        if terms and (long_session or not explained)
        else None
    )
    if snippet:
        moment = datetime.fromisoformat(snippet["time"]) if snippet["time"] else None
        when = f" · {moment:%m-%d %H:%M}" if moment else ""
        print(f"Match: [#{snippet['n']}{when} {ROLE_LABELS[snippet['role']]}] {snippet['text']}")
        if long_session and span:
            print(f"Read around it: --range {hit.first}:{hit.last} ({len(session.turns)} messages in total)")
    command = resume_command(session)
    if command:
        print(f"{resume_label(config)}: {command}")
    else:
        print(f"{kind_note(session)} (no resume)")
    print(f"Inspect: {inspect_command(session)}")
    for hint in configured_hints(session, config):
        print(f"{hint['label']}: {hint['command']}")


ROLE_LABELS = {
    "user": "USER",
    "assistant": "AGENT",
    "delegated": "DELEGATED TASK",
    "summary": "SUMMARY (compaction)",
    "tool": "COMMAND",
}
