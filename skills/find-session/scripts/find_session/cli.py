"""Command-line interface."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import cache
from .cache import cache_path
from .config import default_home, load_config, version
from .model import MAX_INSPECT_COMMAND, MAX_INSPECT_TEXT, MAX_INSPECT_TOTAL

# Each command's module is imported when that command runs: agents call this
# many times per task, and a search should not pay for loading `inspect`.

# The commands, in the order help lists them. The npx wrapper knows the same
# names (a test compares the two lists).
COMMANDS = ("search", "inspect", "recent", "index", "sources", "build-index")

# Older name of `recent`, still accepted and no longer listed.
LEGACY_COMMANDS = {"brief-input": "recent"}

# Options of the program itself that take a value (they come before the command).
ROOT_VALUE_OPTIONS = ("--home", "--book")

EXIT_INTERRUPTED = 130


def parse_since(value: str):
    from .timefilter import parse_since

    return parse_since(value)


def parse_until(value: str):
    from .timefilter import parse_until

    return parse_until(value)


def parse_range(value: str):
    from .transcript import parse_range

    return parse_range(value)


def program_name() -> str:
    """The name this was started under, so usage and error lines can be typed as they are.

    The npx wrapper says so itself; otherwise it is the entry script
    (`sessions.py`), the installed command (`find-session`) or the module.
    """
    named = os.environ.get("FIND_SESSION_PROG")
    if named:
        return named
    started = sys.argv[0] if sys.argv else ""
    name = os.path.basename(started)
    if name == "__main__.py" and os.path.basename(os.path.dirname(started)) == "find_session":
        return "python3 -m find_session"
    if name == "sessions.py":
        return name
    return "find-session"


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog=program_name(),
        description="Search past Claude Code, Codex and omp conversations, and read one back.",
        epilog="Words are matched without regard to case or diacritics; a session with any of them is listed and "
        "the more of them it has close together, the higher it ranks. Run `%(prog)s COMMAND --help` for a "
        "command's options.",
    )
    root.add_argument("--version", action="version", version=f"find-session {version()}")
    root.add_argument(
        "--home",
        type=Path,
        default=None,
        help="directory holding .claude, .codex and .omp (default: $FIND_SESSION_HOME, config 'home', then ~)",
    )
    root.add_argument(
        "--no-cache",
        action="store_true",
        help="parse every session file now instead of using the index ($FIND_SESSION_CACHE=off does the same)",
    )
    # Older name of --home, kept for existing callers.
    root.add_argument("--book", dest="home", type=Path, help=argparse.SUPPRESS)
    commands = root.add_subparsers(dest="command", required=True, metavar="COMMAND", title="commands")

    search = commands.add_parser(
        "search",
        help="rank sessions for a query, separately per tool",
        description="Rank sessions for a query, separately for Claude Code, Codex and omp. A session is listed "
        "when it has at least one of the words; one that has more of them, close together, ranks higher. "
        "Case and diacritics are ignored. In what was said a word is found anywhere, also inside a longer "
        "word; in a command only where a word starts; a word of up to three characters only as a whole word. "
        "The project path counts too; titles do not.",
    )
    search.add_argument(
        "query",
        nargs="+",
        metavar="WORDS",
        help='words to look for, quoted together or not: `search invoice export` is `search "invoice export"` '
        "(letters, digits, _ and - make a word)",
    )
    search.add_argument(
        "--limit",
        type=int,
        default=5,
        help="maximum results per source (default 5)",
    )
    search.add_argument(
        "--source",
        type=str.lower,
        choices=("all", "claude", "codex", "omp"),
        default="all",
        help="only this tool's sessions (default: all, each ranked separately)",
    )
    search.add_argument(
        "--children",
        action="store_true",
        help="add a separate ranking of subagent sessions and Codex review threads",
    )
    search.add_argument("--json", action="store_true", help="machine-readable output")
    search.add_argument(
        "--full",
        action="store_true",
        help="longer, human-oriented output: full ids, resume and inspect command per result",
    )
    search.add_argument(
        "--include-current",
        action="store_true",
        help="rank the session this search is run from like any other (by default it only gets one line)",
    )
    search.add_argument(
        "--since",
        type=parse_since,
        metavar="WHEN",
        help="only sessions active since WHEN: today, yesterday, 3d, 12h, 2w, YYYY-MM-DD[ HH:MM]",
    )
    search.add_argument(
        "--until",
        type=parse_until,
        metavar="WHEN",
        help="only sessions started before WHEN (a bare date includes that day)",
    )
    search.add_argument(
        "--project",
        metavar="TEXT",
        help="only sessions whose working directory contains TEXT ('.' = this directory and below)",
    )

    inspect = commands.add_parser(
        "inspect",
        help="print one conversation, or a part of it",
        description="Print one conversation as numbered messages (what was said, never tool output), "
        "followed by the command that resumes it. Exit code 1: no such session; 2: the id fits several.",
    )
    inspect.add_argument(
        "session_id",
        metavar="SELECTOR",
        help="as printed by search: claude:<id>, codex:<id> or omp:<id> (a unique part of the id is enough)",
    )
    inspect.add_argument("--json", action="store_true", help="machine-readable output")
    inspect.add_argument("--tail", type=int, metavar="N", help="only the last N messages")
    inspect.add_argument(
        "--range",
        dest="span",
        type=parse_range,
        metavar="A:B",
        help="only messages A..B (numbers as printed; A: or :B open-ended)",
    )
    inspect.add_argument("--grep", metavar="WORDS", help="only messages containing any of these words")
    inspect.add_argument(
        "--tools",
        action="store_true",
        help="also show the commands run after each message (never their output); best with --range or --grep",
    )
    inspect.add_argument(
        "--message-chars",
        type=int,
        default=MAX_INSPECT_TEXT,
        metavar="N",
        help=f"cut each message after N characters (default {MAX_INSPECT_TEXT}; 0 = never) and each command "
        f"shown by --tools after {MAX_INSPECT_COMMAND} (0 = all that is stored)",
    )
    inspect.add_argument(
        "--budget",
        type=int,
        default=MAX_INSPECT_TOTAL,
        metavar="N",
        help=f"total characters to print (default {MAX_INSPECT_TOTAL}; 0 = no limit); "
        "over it, the start and the end of the conversation are kept and the call for the next part is printed",
    )

    recent = commands.add_parser(
        "recent",
        help="what happened lately in all tools: start, last request and last answer per session",
        description="Sessions active in the last hours, newest first, each with its title, first and last "
        "request and the last answer. For questions like 'what did we do yesterday'.",
    )
    recent.add_argument("--hours", type=int, default=48, metavar="N", help="how far back to look (default 48)")
    recent.add_argument("--limit", type=int, default=20, metavar="N", help="maximum sessions (default 20)")
    recent.add_argument(
        "--include",
        action="append",
        metavar="GLOB",
        help="append files matching GLOB (relative to home); repeatable",
    )

    index = commands.add_parser(
        "index",
        help="list sessions, one line each, newest first (the newest 50 by default)",
        description="A table of contents of the history, newest first, one line per session: selector, date, "
        "project, number of messages and title (or first message). Prints the newest 50 unless --limit says "
        "otherwise; written to a file with --out, all of them.",
    )
    index.add_argument("--limit", type=int, default=None, metavar="N", help="maximum sessions (default 50; 0 = all)")
    index.add_argument(
        "--since",
        type=parse_since,
        metavar="WHEN",
        help="only sessions active since WHEN: today, yesterday, 3d, 12h, 2w, YYYY-MM-DD[ HH:MM]",
    )
    index.add_argument("--out", type=Path, default=None, help="write to this file instead (all sessions)")

    sources = commands.add_parser(
        "sources",
        help="show where history is read from and how many sessions each tool has",
    )
    sources.add_argument("--json", action="store_true", help="machine-readable output")

    commands.add_parser(
        "build-index",
        help="read all history into the search index now (a first search does this by itself)",
        description="Parse every session file that is new or changed and store it in the index, so that "
        "the next search is fast. Progress goes to stderr. Safe to interrupt: the next run continues.",
    )
    root.set_defaults(parsers={"search": search, "inspect": inspect, "recent": recent, "index": index})
    return root


# How a command is written in a one-line hint: the part before its options.
COMMAND_FORMS = {"search": 'search "WORDS"', "inspect": "inspect SELECTOR", "recent": "recent", "index": "index"}

# Options whose hint shows more than the option: what it is used with.
OPTION_FORMS = {"--tools": "--range A:B --tools"}


def option_form(action: argparse.Action, option: str) -> str:
    """An option as it is typed, with a placeholder for its value."""
    if option in OPTION_FORMS:
        return OPTION_FORMS[option]
    if action.nargs == 0:
        return option
    return f"{option} {action.metavar or (action.dest or 'VALUE').upper()}"


def misplaced(root: argparse.ArgumentParser, args: argparse.Namespace, extra: list[str]) -> str:
    """One line about arguments the command does not take: where the option belongs, when it is one of ours.

    argparse answers these with its usage text and `unrecognized arguments`,
    which sends an agent to the help instead of to the call it meant.
    """
    prog, command = root.prog, args.command
    for word in extra:
        option = word.partition("=")[0]
        if not option.startswith("--"):
            continue
        owners = [
            (name, action)
            for name, parser in args.parsers.items()
            for action in parser._actions
            if option in action.option_strings and name != command
        ]
        if owners:
            name, action = owners[0]
            names = " and ".join(", ".join(owner for owner, _ in owners).rsplit(", ", 1))
            return (
                f"{option} is an option of {names}, not of {command}: "
                f"{prog} {COMMAND_FORMS[name]} {option_form(action, option)}"
            )
        for action in root._actions:
            if option in action.option_strings:
                return f"{option} goes before the command: {prog} {option_form(action, option)} {command} …"
    return f"unrecognized arguments: {' '.join(extra)} (options of {command}: {prog} {command} --help)"


def with_current_names(argv: list[str]) -> list[str]:
    """`argv` with an older command name replaced by the one it has now."""
    position = 0
    while position < len(argv):
        word = argv[position]
        if word in ROOT_VALUE_OPTIONS:
            position += 2
            continue
        if word.startswith("-"):
            position += 1
            continue
        if word in LEGACY_COMMANDS:
            argv = [*argv[:position], LEGACY_COMMANDS[word], *argv[position + 1 :]]
        break
    return argv


def main(argv: list[str] | None = None) -> int:
    root = parser()
    args, extra = root.parse_known_args(with_current_names(list(sys.argv[1:] if argv is None else argv)))
    if args.command == "search" and not any(word.startswith("-") for word in extra):
        # Words typed after an option: `search invoice --since 3d export`.
        args.query += extra
        extra = []
    if extra:
        print(f"{root.prog}: error: {misplaced(root, args, extra)}", file=sys.stderr)
        return 2
    config = load_config()
    home = (args.home or default_home(config)).expanduser()
    location = None if args.no_cache else cache_path(home)
    cache.CACHE = None
    if location:
        from .index import SessionIndex

        cache.CACHE = SessionIndex(location)
    try:
        code = run_command(args, home, config)
        if cache.CACHE is not None:
            cache.CACHE.save()
        sys.stdout.flush()
        return code
    except KeyboardInterrupt:
        # What was parsed so far is kept: the next run continues from there.
        if cache.CACHE is not None:
            cache.CACHE.save(interrupted=True)
        print("find-session: interrupted; run it again to go on from here.", file=sys.stderr)
        return EXIT_INTERRUPTED
    except BrokenPipeError:
        # The reader went away (`… | head`): not an error, and nothing more to say.
        if cache.CACHE is not None:
            cache.CACHE.save()
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except (OSError, ValueError):
            pass
        return 0
    finally:
        cache.CACHE = None


def run_command(args: argparse.Namespace, home: Path, config: dict) -> int:
    if args.command == "index":
        from .recent import build_index

        return build_index(home, args.out, limit=args.limit, since=args.since)
    if args.command == "build-index":
        from .recent import build_search_index

        return build_search_index(home)
    if args.command == "recent":
        from .recent import build_recent, include_globs

        return build_recent(home, args.hours, args.limit, include=include_globs(config, args.include))
    if args.command == "search":
        from .search import search_sessions

        return search_sessions(
            home,
            " ".join(args.query),
            args.limit,
            args.source,
            include_children=args.children,
            as_json=args.json,
            config=config,
            since=args.since,
            until=args.until,
            project=args.project,
            full=args.full,
            include_current=args.include_current,
        )
    if args.command == "inspect":
        from .transcript import inspect_session

        return inspect_session(
            home,
            args.session_id,
            as_json=args.json,
            tail=args.tail,
            span=args.span,
            grep=args.grep,
            message_chars=max(0, args.message_chars),
            budget=max(0, args.budget),
            tools=args.tools,
        )
    if args.command == "sources":
        from .recent import describe_sources

        return describe_sources(home, as_json=args.json)
    return 2
