"""--since / --until / --project filters."""

from __future__ import annotations

import argparse
import os
import re
from datetime import datetime

from .model import Session
from .text import local_time


def now() -> datetime:
    """Current local time (a function so tests can fix the clock)."""
    return datetime.now().astimezone()


RELATIVE_TIME = re.compile(r"\A(\d+)\s*([hdw])\Z")


def parse_when(value: str, *, end: bool) -> datetime:
    """Local, timezone-aware moment for --since (end=False) or --until (end=True).

    Accepts today, yesterday, Nh / Nd / Nw (ago), YYYY-MM-DD and
    YYYY-MM-DD HH:MM. A bare date as --until includes that whole day.
    """
    from datetime import timedelta

    text = value.strip().lower()
    current = now()
    midnight = current.replace(hour=0, minute=0, second=0, microsecond=0)
    day = timedelta(days=1)
    if text in {"today", "yesterday"}:
        start = midnight - (day if text == "yesterday" else timedelta(0))
        return start + day if end else start
    relative = RELATIVE_TIME.match(text)
    if relative:
        amount, unit = int(relative.group(1)), relative.group(2)
        hours = {"h": 1, "d": 24, "w": 168}[unit] * amount
        return current - timedelta(hours=hours)
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid time {value!r}; use today, yesterday, 3d, 12h, 2w, YYYY-MM-DD or 'YYYY-MM-DD HH:MM'"
        ) from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=current.tzinfo)
    if end and len(value.strip()) == 10:
        parsed += day
    return parsed


def parse_since(value: str) -> datetime:
    return parse_when(value, end=False)


def parse_until(value: str) -> datetime:
    return parse_when(value, end=True)


def activity_window(session: Session) -> tuple[datetime, datetime]:
    """When the conversation started and when its file was last written."""
    last = datetime.fromtimestamp(session.mtime).astimezone()
    first = local_time(session.started) or last
    return min(first, last), last


def in_window(session: Session, since: datetime | None, until: datetime | None) -> bool:
    first, last = activity_window(session)
    return (since is None or last >= since) and (until is None or first < until)


def in_project(session: Session, project: str | None) -> bool:
    """--project: a substring of the working directory, or "." for this directory."""
    if not project:
        return True
    if project in {".", "./"}:
        here = os.getcwd().rstrip("/")
        return session.cwd == here or session.cwd.startswith(here + "/")
    from .searchtext import fold

    needle = fold(project)
    return needle in fold(session.cwd) or needle in fold(session.project)
