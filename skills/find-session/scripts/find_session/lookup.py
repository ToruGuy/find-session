"""Resolve a selector to a session; find the subagents of a session."""

from __future__ import annotations

from pathlib import Path

from . import cache
from .cache import entry_for, latest_copies, load_candidate
from .discovery import discover
from .model import MAX_AMBIGUOUS_LOAD, SOURCE_KEYS, Candidate, Session
from .output import session_selector


def split_selector(selector: str) -> tuple[str | None, str]:
    prefix, sep, rest = selector.partition(":")
    if sep and prefix.lower() in SOURCE_KEYS and rest:
        return SOURCE_KEYS[prefix.lower()], rest
    return None, selector


def resolve_selector(home: Path, selector: str) -> list[Candidate]:
    """Candidates for `[source:]id`; exact ids win over substring matches, and
    copies of one conversation count as one (the one written last)."""
    source, needle = split_selector(selector)
    matches = [
        candidate
        for candidate in discover(home, include_children=True)
        if (source is None or candidate.source == source)
        and (needle in candidate.session_id or needle in candidate.name)
    ]
    if len(matches) <= 1:
        return matches
    if len(matches) <= MAX_AMBIGUOUS_LOAD:
        matches = latest_copies(matches)
    exact = [candidate for candidate in matches if candidate.session_id == needle]
    # Only Codex filenames differ from the stored id (rollout-<time>-<id>),
    # so only those need a cheap load to compare the real id.
    codex = [c for c in matches if c.source == "Codex" and c not in exact]
    if len(codex) <= MAX_AMBIGUOUS_LOAD:
        for candidate in codex:
            try:
                loaded = entry_for(candidate, first_only=True)
            except OSError:
                continue
            if loaded.session_id == needle:
                exact.append(candidate)
    return exact or matches


def find_session_by_id(home: Path, session_id: str) -> Session | None:
    matches = resolve_selector(home, session_id)
    if not matches:
        return None
    try:
        return load_candidate(matches[0])
    except OSError:
        return None


def children_of(home: Path, session: Session) -> list[str]:
    """Selectors of subagent sessions started from this one."""
    if session.kind != "main":
        return []
    found: list[str] = []
    for candidate in discover(home, include_children=True):
        if candidate.source != session.source:
            continue
        if session.source != "Codex":
            # omp and Claude Code: the place of the file says whose subagent it is.
            if candidate.kind == "child" and candidate.parent_id == session.session_id:
                found.append(candidate.selector)
            continue
        if session.session_id not in codex_meta_text(candidate):
            continue
        child = entry_for(candidate, first_only=True)
        if child.kind == "child" and child.parent_id == session.session_id:
            found.append(session_selector(child))
    return sorted(found)


def codex_meta_text(candidate: Candidate) -> str:
    """The raw session_meta line (cheap pre-check before parsing a file)."""
    if cache.CACHE is not None:
        parent = cache.CACHE.stored_parent(candidate)
        if parent is not None:
            return parent
    from .jsonl import MAX_LINE_BYTES

    try:
        with open(candidate.location, "rb") as handle:
            return handle.readline(MAX_LINE_BYTES).decode("utf-8", "replace")
    except OSError:
        return ""
