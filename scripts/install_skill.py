#!/usr/bin/env python3
"""Link the find-session skill of this clone into Claude Code, Codex and omp.

Only the tools found on this machine are linked (~/.claude, ~/.codex, ~/.omp);
--harness names them yourself. Repeatable: an existing link to this skill is
left alone, and so is a find-session installed another way (npx, skills.sh,
another clone, a Claude Code plugin). A different skill with the same name is never overwritten
unless --replace is given, and then it is moved to
<skills-root-parent>/skill-backups/find-session-<timestamp> first, outside the
discovery root, so the old copy is never loaded next to the new one. No
settings are touched. Afterwards it shows how many sessions each tool has and
fills the search index, so the first search is fast (--no-index skips that).

The rules are the same as in `npx find-session install` (npm/find-session.js):
tests/install_cases.json is run against both.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SKILL_DIR = REPO / "skills" / "find-session"
# Fixed, not taken from the checkout directory name: a clone in ~/src/fs
# must still install as "find-session".
SKILL_NAME = "find-session"
HARNESSES = ("claude", "codex", "omp")
LABELS = {"claude": "Claude Code", "codex": "Codex", "omp": "omp"}
MIN_PYTHON = (3, 9)
# The exit code the helper and the npx wrapper use for "no usable Python".
EXIT_NO_PYTHON = 3


def skill_roots(home: Path, omp_agent_dir: Path | None) -> dict[str, Path]:
    # omp loads ~/.claude/skills only when the foreign provider is opted in, so
    # it gets its own native link (~/.omp/agent/skills, per config-usage.md).
    return {
        "claude": home / ".claude/skills",
        "codex": home / ".codex/skills",
        "omp": (omp_agent_dir or home / ".omp/agent") / "skills",
    }


def other_roots(home: Path) -> dict[str, list[Path]]:
    """Other places a tool reads skills from, where another installer may have put this one."""
    # skills.sh (`npx skills add`) installs for Codex into ~/.agents/skills.
    return {"claude": plugin_skill_roots(home), "codex": [home / ".agents/skills"]}


def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def plugin_skill_roots(home: Path) -> list[Path]:
    """The skills folders of find-session installed as a Claude Code plugin.

    Claude Code lists its plugins in ~/.claude/plugins/installed_plugins.json.
    Only a plugin that every session loads counts: installed for the user (not
    for one project) and not switched off in ~/.claude/settings.json.
    """
    plugins = read_json(home / ".claude/plugins/installed_plugins.json").get("plugins")
    if not isinstance(plugins, dict):
        return []
    enabled = read_json(home / ".claude/settings.json").get("enabledPlugins")
    enabled = enabled if isinstance(enabled, dict) else {}
    roots: list[Path] = []
    for name, installs in plugins.items():
        if name.split("@")[0] != SKILL_NAME or enabled.get(name) is False:
            continue
        for entry in installs if isinstance(installs, list) else []:
            if isinstance(entry, dict) and isinstance(entry.get("installPath"), str):
                if entry.get("scope", "user") == "user":
                    roots.append(Path(entry["installPath"]) / "skills")
    return roots


def detected(home: Path, omp_agent_dir: Path | None) -> list[str]:
    """The tools that exist here: each by its own directory."""
    present = {
        "claude": (home / ".claude").is_dir(),
        "codex": (home / ".codex").is_dir(),
        "omp": (omp_agent_dir or home / ".omp/agent").is_dir() or (home / ".omp").is_dir(),
    }
    return [name for name in HARNESSES if present[name]]


def display(path: Path, home: Path) -> str:
    """A path as it is printed: with ~ for the home directory."""
    text, base = str(path), str(home)
    return "~" + text[len(base) :] if text == base or text.startswith(base + os.sep) else text


def installed_version(skill: Path) -> str | None:
    """The version of a find-session skill folder, or None when `skill` is not one."""
    try:
        head = (skill / "SKILL.md").read_text(errors="replace")[:400]
        if f"name: {SKILL_NAME}" not in head.splitlines() or not (skill / "scripts" / "sessions.py").is_file():
            return None
        return (skill / "VERSION").read_text().strip() or "unknown version"
    except OSError:
        return None


def backup_path(root: Path) -> Path:
    """A fresh backup location beside (not inside) the skills discovery root."""
    backups = root.parent / "skill-backups"
    base = f"{SKILL_NAME}-{datetime.now():%Y%m%d-%H%M%S-%f}"
    candidate = backups / base
    counter = 2
    while candidate.exists() or candidate.is_symlink():
        candidate = backups / f"{base}-{counter}"
        counter += 1
    return candidate


def kept_message(where: str, version: str) -> str:
    return f"kept, already installed from {where} ({version}); --replace links this one instead"


def install(
    root: Path, *, replace: bool, dry_run: bool, home: Path | None = None, others: tuple[Path, ...] | list[Path] = ()
) -> tuple[bool, str]:
    home = home or Path.home()
    target = root / SKILL_NAME
    skill = display(SKILL_DIR, home)
    if target.is_symlink() and target.resolve() == SKILL_DIR:
        return True, "already linked"
    if target.is_symlink() and target.resolve() == REPO:
        # Up to 0.2.1 the skill was the repository root; point the link at its new place.
        if dry_run:
            return True, f"would relink -> {skill}"
        target.unlink()
        target.symlink_to(SKILL_DIR, target_is_directory=True)
        return True, f"relinked -> {skill}"
    if target.exists() or target.is_symlink():
        version = installed_version(target)
        if version and not replace:
            return True, kept_message(display(target.resolve(), home), version)
        if not replace:
            return False, f"conflict: {display(target, home)} is a different skill, kept (use --replace)"
        backup = backup_path(root)
        if dry_run:
            return True, f"would move {display(target, home)} to {display(backup, home)} and link"
        backup.parent.mkdir(parents=True, exist_ok=True)
        # rename moves a symlink itself, never the directory it points to.
        target.rename(backup)
        target.symlink_to(SKILL_DIR, target_is_directory=True)
        return True, f"linked, previous one moved to {display(backup, home)}"
    if not replace:
        for other in others:
            version = installed_version(other / SKILL_NAME)
            if version and (other / SKILL_NAME).resolve() != SKILL_DIR:
                return True, kept_message(display(other / SKILL_NAME, home), version)
    if dry_run:
        return True, f"would link {display(target, home)} -> {skill}"
    root.mkdir(parents=True, exist_ok=True)
    target.symlink_to(SKILL_DIR, target_is_directory=True)
    return True, f"linked {display(target, home)}"


def run_helper(*arguments: str) -> int:
    helper = SKILL_DIR / "scripts" / "sessions.py"
    return subprocess.run([sys.executable, str(helper), *arguments]).returncode


def show_and_index(*, build_index: bool) -> int:
    """What the skill sees, then the index: last, so that an interrupted build leaves a finished install."""
    print("\nWhat the skill can see:", flush=True)
    code = run_helper("sources")
    if code == 0 and build_index:
        print(
            "\nIndexing your history so that the first search is fast (one-time; --no-index skips it; "
            "if it is interrupted, run this again: it continues):",
            flush=True,
        )
        code = run_helper("build-index")
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--home", type=Path, help="home directory to install into (default ~); only links, no index")
    parser.add_argument(
        "--omp-agent-dir",
        type=Path,
        help="omp agent directory (default <home>/.omp/agent, or $PI_CODING_AGENT_DIR without --home)",
    )
    parser.add_argument(
        "--harness",
        choices=HARNESSES,
        action="append",
        help="link this tool, found or not (repeatable; default: every tool found)",
    )
    parser.add_argument("--replace", action="store_true", help="move a different existing skill to a backup first")
    parser.add_argument("--dry-run", action="store_true", help="show what would change")
    parser.add_argument("--no-index", action="store_true", help="do not build the search index now")
    args = parser.parse_args(argv)

    if sys.version_info < MIN_PYTHON:
        print(
            f"find-session: Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer is needed, but {sys.executable} is "
            f"Python {sys.version_info[0]}.{sys.version_info[1]}. Nothing was installed.",
            file=sys.stderr,
        )
        return EXIT_NO_PYTHON

    omp_agent_dir = args.omp_agent_dir
    # An explicit --home (temporary homes in tests, other users) must never
    # fall through to this process's real omp agent dir.
    if omp_agent_dir is None and args.home is None and os.environ.get("PI_CODING_AGENT_DIR"):
        omp_agent_dir = Path(os.environ["PI_CODING_AGENT_DIR"])
    home = (args.home or Path.home()).expanduser()
    omp_agent_dir = omp_agent_dir.expanduser() if omp_agent_dir else None
    roots = skill_roots(home, omp_agent_dir)
    found = detected(home, omp_agent_dir)
    chosen = args.harness or found
    if not chosen:
        print(
            "find-session: no Claude Code, Codex or omp directory found (~/.claude, ~/.codex, ~/.omp).\n"
            "Install one of them first, or pick one explicitly: --harness claude",
            file=sys.stderr,
        )
        return 1
    others = other_roots(home)
    prefix = "dry-run: " if args.dry_run else ""
    ok = True
    for harness in HARNESSES:
        if harness not in chosen:
            if args.harness:
                print(f"{harness}: skipped, not named with --harness")
            else:
                print(f"{harness}: skipped, {LABELS[harness]} not found (add --harness {harness} to install anyway)")
            continue
        success, message = install(
            roots[harness], replace=args.replace, dry_run=args.dry_run, home=home, others=others.get(harness, ())
        )
        ok = ok and success
        print(f"{prefix}{harness}: {message}")
    if args.dry_run or not ok:
        return 0 if ok else 1
    if args.home is not None:
        # Another home's history would be indexed into this user's cache: only the links are made.
        return 0
    code = show_and_index(build_index=not args.no_index)
    if code == 0:
        print(
            '\nDone. Start a new agent session and ask it to "find the session where ...".\n'
            f"Update later: git -C {display(REPO, home)} pull"
        )
    return code


if __name__ == "__main__":
    try:
        code = main()
        sys.stdout.flush()
    except BrokenPipeError:
        # The reader went away (`… | head`): the links are made, there is nobody left to tell.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        code = 0
    raise SystemExit(code)
