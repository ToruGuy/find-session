"""The search command."""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime
from pathlib import Path

from .cache import Entry, entries, latest_copies, note_present, with_search_text
from .config import load_config
from .discovery import discover
from .model import SOURCE_KEYS, SOURCES, Session
from .output import current_session_line, match_record, print_compact_results, print_search_match, short_ids
from .ranking import Hit, has_every_word, query_terms, score_text
from .timefilter import in_project, in_window

if False:  # typing only
    from typing import Iterator

CHILDREN_SECTION = "subagents"

# What a harness exports to the commands its agent runs: the id of the session
# the command was run from. Claude Code: a subagent gets its parent's id, and
# the parent is the session that is listed. Codex: both names carry the thread
# id; the first one that is set is used. No such variable is documented for
# omp or agy, so none is used there.
CALLER_VARIABLES = (
    ("Claude", ("CLAUDE_CODE_SESSION_ID",)),
    ("Codex", ("CODEX_THREAD_ID", "CODEX_SESSION_ID")),
)


def calling_session() -> tuple[str, str] | None:
    """(source, session id) of the session this search is run from, when the environment says so.

    An agent names its topic before it searches, so its own session matches
    the query and would take a place in the ranking. Variables of two
    harnesses at once mean one was started from inside the other, and the
    outer one's id names a different session: no session is taken for the
    caller then, rather than hiding one that may be the session looked for.
    """
    found = []
    for source, names in CALLER_VARIABLES:
        for name in names:
            value = os.environ.get(name, "").strip()
            if value:
                found.append((source, value))
                break
    return found[0] if len(found) == 1 else None


def count_text(sections: list[str], totals: dict[str, int], complete: dict[str, int]) -> str:
    """`Claude 4 (+31 partial), Codex 1, omp 0, agy 0`: sessions with every query word,
    and how many more have only some of them (left out when there are none)."""
    parts = []
    for name in sections:
        partial = totals[name] - complete[name]
        parts.append(f"{name} {complete[name]} (+{partial} partial)" if partial else f"{name} {totals[name]}")
    return ", ".join(parts)


def search_sessions(
    home: Path,
    query: str,
    limit: int,
    source_filter: str = "all",
    *,
    include_children: bool = False,
    as_json: bool = False,
    config: dict | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    project: str | None = None,
    full: bool = False,
    include_current: bool = False,
) -> int:
    config = load_config() if config is None else config
    terms = query_terms(query)
    if not terms:
        print("Nothing to search for.", file=sys.stderr)
        return 2
    selected_sources = list(SOURCES) if source_filter == "all" else [SOURCE_KEYS[source_filter.lower()]]
    sections = list(selected_sources)
    # Subagents and automatic review threads get their own ranking so they
    # never crowd out, or pose as, user conversations.
    if include_children:
        sections.append(CHILDREN_SECTION)
    matches: dict[str, list[tuple[Hit, float, Entry]]] = {name: [] for name in sections}
    # Subagents are scored even when hidden: a word that only occurs in a
    # subagent's answer must not vanish silently.
    hidden_children = 0
    # The calling session is scored like any other but not listed: it gets one
    # line under the results instead.
    caller = None if include_current else calling_session()
    current: tuple[Hit, Entry] | None = None
    known_ids: dict[str, list[str]] = {}
    candidates = discover(home, include_children=True)
    note_present(candidates)
    # A conversation stored twice is one result: the copy `inspect` opens.
    candidates = latest_copies(candidates)

    def eligible() -> Iterator[Entry]:
        """Sessions that pass the filters, judged on a few facts alone."""
        for entry in entries(candidate for candidate in candidates if candidate.source in selected_sources):
            known_ids.setdefault(entry.source, []).append(entry.session_id)
            if not entry.messages:
                continue
            if (since or until) and not in_window(entry, since, until):
                continue
            if in_project(entry, project):
                yield entry

    # A session with any of the words is listed. How many have all of them is
    # counted apart, and so is which words no session has: the totals alone
    # would read as "sessions about the query".
    complete = {name: 0 for name in sections}
    seen = [False] * len(terms)
    # How many matching sessions have each word: the rarer one says more about a hit.
    common = [0] * len(terms)
    for entry, text in with_search_text(eligible()):
        # Codex marks subagents only inside the file, so kind comes from the parse.
        section = CHILDREN_SECTION if entry.kind != "main" else entry.source
        listed = section in matches
        # A subagent that will not be listed only counts when it has every
        # word: once each word has been seen somewhere, a session that lacks
        # one is passed over without counting anything.
        if not listed and all(seen) and not has_every_word(text, terms):
            continue
        hit = score_text(text, entry.mtime, terms, query)
        if not hit.score:
            continue
        if not all(seen):
            seen = [a or b for a, b in zip(seen, hit.found)]
        common = [a + b for a, b in zip(common, hit.found)]
        if caller == (entry.source, entry.session_id):
            if listed and (current is None or hit.score > current[0].score):
                current = (hit, entry)
            continue
        if listed:
            matches[section].append((hit, entry.mtime, entry))
            complete[section] += all(hit.found)
        elif all(hit.found):
            hidden_children += 1

    shown: dict[str, list[tuple[Hit, float, Session]]] = {}
    for name, section_matches in matches.items():
        section_matches.sort(key=lambda row: (row[0].score, row[1]), reverse=True)
        # Only the sessions that are printed are read in full.
        shown[name] = rows = []
        position = 0
        while len(rows) < limit and position < len(section_matches):
            hit, mtime, entry = section_matches[position]
            try:
                rows.append((hit, mtime, entry.session()))
            except OSError:
                # Removed since it was scored, and no longer in the index.
                del section_matches[position]
                continue
            position += 1
    totals = {name: len(matches[name]) for name in sections}
    rarity = [1 / (1 + count) for count in common]
    # Words that no matching session has (a typo, or the wrong language): only
    # worth saying when other words did match.
    # Named as they were typed, not as they are matched (without diacritics).
    typed = re.findall(r"[\w-]+", query.lower())
    named = typed if len(typed) == len(terms) else terms
    missing = [word for word, found in zip(named, seen) if not found] if any(seen) else []
    missing = list(dict.fromkeys(missing))

    if as_json:
        payload = {
            "query": query,
            "counts": totals,
            # Of those, the sessions that have every query word.
            "counts_all_words": complete,
            "words_not_found": missing,
            "hidden_subagent_matches": hidden_children,
            "results": {
                name: [
                    match_record(session, hit, config, terms=terms, query=query, rarity=rarity)
                    for hit, _, session in shown[name]
                ]
                for name in sections
            },
            # The calling session when it matched: in no ranking and in no count.
            "current_session": None,
        }
        if current:
            try:
                payload["current_session"] = match_record(
                    current[1].session(), current[0], config, terms=terms, query=query, rarity=rarity
                )
            except OSError:
                pass
        import json

        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    filters = []
    if since:
        filters.append(f"since {since:%Y-%m-%d %H:%M}")
    if until:
        filters.append(f"until {until:%Y-%m-%d %H:%M}")
    if project:
        filters.append(f"project {project}")
    filter_text = f" [{', '.join(filters)}]" if filters else ""
    if not full:
        printed = [(session.source, session.session_id) for rows in shown.values() for _, _, session in rows]
        if current:
            printed.append((current[1].source, current[1].session_id))
        short = short_ids(known_ids, printed)
        print_compact_results(
            query,
            filter_text,
            sections,
            shown,
            totals,
            limit,
            hidden_children,
            config,
            terms=terms,
            rarity=rarity,
            short=short,
            counts=count_text(sections, totals, complete),
            missing=missing,
            no_history=not any(known_ids.values()),
            current=current_session_line(
                current[1].source,
                short.get((current[1].source, current[1].session_id), current[1].session_id),
                current[0],
            )
            if current
            else "",
        )
        return 0

    print(f"Sessions matching: {query}{filter_text} ({count_text(sections, totals, complete)})")
    if missing:
        print(f"No session has: {' '.join(missing)}")
    if hidden_children:
        print(
            f"Also {hidden_children} subagent or review session(s) containing all the words, not shown: add --children."
        )
    if source_filter == "all":
        print("Results are ranked per source: always check Claude, Codex, omp and agy before drawing conclusions.")

    for name in sections:
        print(f"\n=== {name} — top {min(limit, totals[name])} of {totals[name]} ===")
        if not totals[name]:
            print(f"(no matches in {name})")
            continue
        for hit, _, session in shown[name]:
            print_search_match(session, hit, config, terms=terms, query=query, rarity=rarity)
    if current:
        line = current_session_line(current[1].source, current[1].session_id, current[0])
        print(f"\nNot listed: {line} (--include-current lists it)")
    return 0
