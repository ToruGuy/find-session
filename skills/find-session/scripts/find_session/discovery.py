"""Where session files live and which ones exist."""

from __future__ import annotations

import os
from pathlib import Path

from .config import default_home
from .model import Candidate

if False:  # typing only
    from typing import Iterable


def roots(home: Path | None = None) -> tuple[Path, Path]:
    home = home or default_home()
    claude = Path(os.environ.get("CLAUDE_SESSIONS_ROOT", home / ".claude/projects"))
    codex = Path(os.environ.get("CODEX_SESSIONS_ROOT", home / ".codex/sessions"))
    return claude, codex


def omp_roots(home: Path | None = None) -> list[Path]:
    """Native omp session roots: default profile, named profiles, agent-dir override.

    OMP_SESSIONS_ROOT replaces discovery entirely (mirrors, XDG layouts, tests).
    """
    override = os.environ.get("OMP_SESSIONS_ROOT")
    if override:
        return [Path(override).expanduser()]
    home = home or default_home()
    base = home / (os.environ.get("PI_CONFIG_DIR") or ".omp")
    found: list[Path] = []
    agent_dir = os.environ.get("PI_CODING_AGENT_DIR")
    if agent_dir:
        found.append(Path(agent_dir).expanduser() / "sessions")
    found.append(base / "agent/sessions")
    profiles = base / "profiles"
    if profiles.is_dir():
        found.extend(sorted(profiles.glob("*/agent/sessions")))
    unique: list[Path] = []
    seen: set[Path] = set()
    for root in found:
        key = root.resolve() if root.exists() else root
        if key not in seen:
            seen.add(key)
            unique.append(root)
    return unique


# Codex names its threads in this file next to the sessions directory, one
# JSON row per rename: {"id", "thread_name", "updated_at"}.
CODEX_TITLES = "session_index.jsonl"


def discover(home: Path | None = None, *, include_children: bool = False) -> list[Candidate]:
    """All session files, each attributed to exactly one source."""
    claude_root, codex_root = roots(home)
    if os.sep == "/":
        found = claude_files(claude_root, include_children) + codex_files(codex_root)
    else:
        found = glob_files(claude_root, codex_root, include_children)
    titles = str(codex_root.parent / CODEX_TITLES)
    for candidate in found:
        if candidate.source == "Codex":
            candidate.titles = titles
    seen: set[str] = set()
    for root in omp_roots(home):
        if not root.is_dir():
            continue
        from .parsers.omp import omp_candidates

        for candidate in omp_candidates(root):
            if candidate.kind == "child" and not include_children:
                continue
            key = f"{candidate.kind}:{candidate.session_id}:{candidate.path.name}"
            if key not in seen:
                seen.add(key)
                found.append(candidate)
    return found


# Claude Code keeps what a subagent said and ran in a file of its own:
# <project>/<session id>/subagents/agent-<agent id>.jsonl (deeper for an agent
# started by an agent). The agent id is the subagent's session id here.
CLAUDE_SUBAGENTS = "subagents"
CLAUDE_AGENT_PREFIX = "agent-"


def glob_files(claude_root: Path, codex_root: Path, include_children: bool = False) -> list[Candidate]:
    """Claude and Codex session files, found with pathlib: the definition of
    which files count and in which order (ties in a ranking keep this order)."""
    found: list[Candidate] = []
    if claude_root.is_dir():
        for path in sorted(claude_root.glob("*/*.jsonl")):
            if CLAUDE_SUBAGENTS not in path.parts:
                found.append(Candidate("Claude", path, path.stem))
        if include_children and CLAUDE_SUBAGENTS not in claude_root.parts:
            children = []
            for path in sorted(claude_root.glob(f"*/*/{CLAUDE_SUBAGENTS}/**/*.jsonl")):
                relative = path.relative_to(claude_root).parts
                if relative[0] != CLAUDE_SUBAGENTS:
                    try:
                        stat = path.stat()
                    except OSError:
                        stat = None
                    children.append((path, relative[1], stat))
            found.extend(one_per_agent(children))
    if codex_root.is_dir():
        for path in sorted(codex_root.rglob("*.jsonl")):
            found.append(Candidate("Codex", path, path.stem.removeprefix("rollout-")))
    return found


# The same files in the same order as glob_files(), listed with os.scandir:
# several times faster on thousands of files, and each file's size and mtime
# come along, so the index does not have to ask for them again.


def one_per_agent(children: list[tuple[Path | str, str, os.stat_result | None]]) -> list[Candidate]:
    """Subagent files (path, parent session id, stat) in path order as candidates, one per agent id.

    A session that was forked or moved to another project directory takes
    copies of its subagent files along. A copy is the same conversation under
    the same agent id, so only one is listed, the largest (the one that went
    on, if any did), the first in path order among equals: a selector then
    never names two files.
    """
    kept: dict[str, tuple[int, Candidate]] = {}
    for path, parent, stat in children:
        name = _stem(str(path).rpartition("/")[2] if isinstance(path, str) else path.name)
        agent = name[len(CLAUDE_AGENT_PREFIX) :] if name.startswith(CLAUDE_AGENT_PREFIX) else name
        size = stat.st_size if stat is not None else -1
        if agent and (agent not in kept or size > kept[agent][0]):
            kept[agent] = (size, Candidate("Claude", path, agent, "child", parent, stat=stat))
    return [candidate for _, candidate in kept.values()]


def claude_files(root: Path, include_children: bool = False) -> list[Candidate]:
    """<root>/<project>/<session>.jsonl, like root.glob("*/*.jsonl"), then the subagent files."""
    if not root.is_dir() or CLAUDE_SUBAGENTS in root.parts:
        return []
    base = _base(root)
    found: list[Candidate] = []
    children: list[tuple[str, str, os.stat_result | None]] = []
    for project in sorted(_listing(str(root)), key=lambda entry: entry.name):
        try:
            if project.name == CLAUDE_SUBAGENTS or not project.is_dir():
                continue
        except OSError:
            continue
        prefix = f"{base}{project.name}/"
        listing = _listing(prefix[:-1])
        for name, stat in sorted((entry.name, _stat(entry)) for entry in listing if entry.name.endswith(".jsonl")):
            found.append(Candidate("Claude", prefix + name, _stem(name), stat=stat))
        if not include_children:
            continue
        # A session's folder (named by its id) exists only when it has subagents or other extras.
        for session in sorted(entry.name for entry in listing if not entry.name.endswith(".jsonl")):
            below = f"{prefix}{session}/{CLAUDE_SUBAGENTS}"
            for parts, _, stat in _files_below(below):
                children.append(("/".join((below, *parts)), session, stat))
    return found + one_per_agent(children)


def codex_files(root: Path) -> list[Candidate]:
    """Every *.jsonl below <root>, like root.rglob("*.jsonl")."""
    if not root.is_dir():
        return []
    base = _base(root)
    return [
        Candidate("Codex", base + "/".join(parts), _stem(name).removeprefix("rollout-"), stat=stat)
        for parts, name, stat in _files_below(str(root))
    ]


def _files_below(directory: str) -> list[tuple[tuple[str, ...], str, os.stat_result | None]]:
    """(path parts below `directory`, file name, stat) of every *.jsonl in it and
    its subdirectories, in path order; nothing when it does not exist."""
    rows: list[tuple[tuple[str, ...], str, os.stat_result | None]] = []
    pending: list[tuple[str, ...]] = [()]
    while pending:
        parts = pending.pop()
        for entry in _listing("/".join((directory, *parts))):
            try:
                # Like pathlib, directories reached through a symlink are not entered.
                if entry.is_dir(follow_symlinks=False):
                    pending.append((*parts, entry.name))
            except OSError:
                pass
            if entry.name.endswith(".jsonl"):
                rows.append(((*parts, entry.name), entry.name, _stat(entry)))
    rows.sort(key=lambda row: row[0])
    return rows


def _base(root: Path) -> str:
    """Text that, followed by a relative path, names a file below `root` the way pathlib does."""
    text = str(root)
    if text == ".":
        return ""
    return text if text.endswith("/") else text + "/"


def _listing(directory: str) -> list[os.DirEntry]:
    try:
        with os.scandir(directory) as entries:
            return list(entries)
    except OSError:
        return []


def _stat(entry: os.DirEntry) -> os.stat_result | None:
    try:
        return entry.stat()
    except OSError:
        return None


def _stem(name: str) -> str:
    """File name without its last suffix, like Path.stem."""
    dot = name.rfind(".")
    return name[:dot] if 0 < dot < len(name) - 1 else name


def session_paths(home: Path | None = None) -> Iterable[tuple[str, Path]]:
    for candidate in discover(home):
        yield candidate.source, candidate.path
