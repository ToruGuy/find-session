"""Small text helpers shared by parsers and output."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path


def squash(text: str) -> str:
    return " ".join(text.split())


def clean_user_text(text: str) -> str:
    text = text.strip()
    if not text:
        return ""
    lowered = text.lower()
    injected_prefixes = (
        "<environment_context>",
        "<task-notification>",
        "<system-reminder>",
        "<command-name>",
        "base directory for this skill:",
    )
    if lowered.startswith("<") or lowered.startswith(injected_prefixes):
        return ""
    if "tool_result" in lowered[:240]:
        return ""
    return squash(text)


def clean_assistant_text(text: str) -> str:
    text = text.strip()
    return squash(text) if text else ""


def content_text(content: object, allowed_types: set[str]) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") not in allowed_types:
            continue
        text = block.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


# Any user's home directory (macOS, Linux, root) is shown as ~/ so that
# sessions synced from another machine read the same as local ones.
HOME_PREFIX = re.compile(r"\A(?:/Users/[^/]+|/home/[^/]+|/root)/")


def short_project(cwd: str) -> str:
    if not cwd:
        return "unknown"
    local_home = str(Path.home()).rstrip("/") + "/"
    if local_home != "/" and cwd.startswith(local_home):
        return "~/" + cwd[len(local_home) :].rstrip("/")
    match = HOME_PREFIX.match(cwd)
    if match:
        return "~/" + cwd[match.end() :].rstrip("/")
    return cwd.rstrip("/") or "/"


# Compact output (the default) is written for agents: every line has to earn
# its tokens. Repeated parts (helper path, resume command, config hints) are
# printed once, ids are shortened to a unique prefix, and the hit line names
# the stretch of messages to read with inspect --range.
MAX_COMPACT_TEXT = 100

# The quote on a hit line. It is placed to hold the query words, so it is cut
# at its full width more often than a title is: a little narrower, it costs no
# more characters per search than it did (measured on logged queries).
MAX_HIT_TEXT = 96


def compact_text(text: str, limit: int = MAX_COMPACT_TEXT) -> str:
    """`text` in at most `limit` characters, ending after a whole word when it is cut."""
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    if not text[limit - 1].isspace():
        space = cut.rfind(" ")
        # One very long token (a path, a URL) is cut where the limit falls.
        if space >= limit // 2:
            cut = cut[:space]
    return cut.rstrip() + "…"


def home_relative(path: str) -> str:
    home = str(Path.home())
    if path == home or path.startswith(home + "/"):
        return "~" + path[len(home) :]
    return path


def local_time(stamp: str) -> datetime | None:
    """A record timestamp (ISO 8601, usually UTC) in the local timezone."""
    if not stamp:
        return None
    try:
        moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment.astimezone() if moment.tzinfo else moment
