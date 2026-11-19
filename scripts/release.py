#!/usr/bin/env python3
"""Stamp a release: propose the next version, write it everywhere, move the changelog.

  python3 scripts/release.py                  propose the next version from [Unreleased]
  python3 scripts/release.py --apply 0.3.0    stamp it (version files + CHANGELOG section)
  python3 scripts/release.py --notes 0.3.0    print that version's changelog section
  python3 scripts/release.py --check v0.3.0   verify a tag against the files (used by CI)

The rule (SemVer): pre-1.0, any Added/Changed/Removed/Deprecated entry or a
**BREAKING** one bumps MINOR, only Fixed/Security bumps PATCH; from 1.0 on,
BREAKING bumps MAJOR. It never commits, tags or pushes.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
# Every file that carries the version. A test keeps them equal.
PLAIN_FILES = ("VERSION", "skills/find-session/VERSION")
JSON_FILES = (".claude-plugin/plugin.json", "package.json")
MINOR_GROUPS = {"added", "changed", "removed", "deprecated"}
PATCH_GROUPS = {"fixed", "security"}
UNRELEASED = re.compile(r"^## \[Unreleased\][^\n]*\n", re.MULTILINE)
NEXT_SECTION = re.compile(r"^## \[", re.MULTILINE)
SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
# The first "version" key of a manifest; only that line changes.
JSON_VERSION = re.compile(r'^(\s*"version"\s*:\s*")([^"]*)(")', re.MULTILINE)


class ReleaseError(Exception):
    pass


def parse_version(text: str) -> tuple[int, int, int]:
    match = SEMVER.match(text.strip())
    if not match:
        raise ReleaseError(f"not a version (X.Y.Z): {text.strip()!r}")
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def current_version(root: Path) -> str:
    return (root / "VERSION").read_text().strip()


def unreleased(changelog: str) -> tuple[int, int, str]:
    """Start and end offsets of the [Unreleased] body, and the body itself."""
    match = UNRELEASED.search(changelog)
    if not match:
        raise ReleaseError("CHANGELOG.md has no '## [Unreleased]' section")
    following = NEXT_SECTION.search(changelog, match.end())
    end = following.start() if following else len(changelog)
    return match.end(), end, changelog[match.end() : end]


def bump(body: str, current: str) -> tuple[str, str]:
    """The next version and a one-line reason, from the [Unreleased] body."""
    groups: dict[str, int] = {}
    group = None
    breaking = False
    for line in body.splitlines():
        heading = re.match(r"^###\s+(\w+)", line)
        if heading:
            group = heading.group(1).lower()
            continue
        if re.match(r"^\s*[-*]\s+\S", line):
            groups[group or "other"] = groups.get(group or "other", 0) + 1
            breaking = breaking or "BREAKING" in line
    if not groups:
        raise ReleaseError("[Unreleased] is empty: nothing to release")
    unknown = sorted(set(groups) - MINOR_GROUPS - PATCH_GROUPS)
    if unknown:
        raise ReleaseError(f"[Unreleased] has entries outside the Keep a Changelog groups: {', '.join(unknown)}")
    major, minor, patch = parse_version(current)
    found = ", ".join(f"{count} {name.capitalize()}" for name, count in groups.items())
    if breaking and major >= 1:
        return f"{major + 1}.0.0", f"{found}, BREAKING -> MAJOR"
    if breaking or set(groups) & MINOR_GROUPS:
        label = "BREAKING before 1.0 -> MINOR" if breaking and not set(groups) & MINOR_GROUPS else "MINOR"
        return f"{major}.{minor + 1}.0", f"{found} -> {label}"
    return f"{major}.{minor}.{patch + 1}", f"{found} -> PATCH"


def section(changelog: str, version: str) -> str:
    """The body of '## [version] - date' (without its heading)."""
    match = re.search(rf"^## \[{re.escape(version)}\][^\n]*\n", changelog, re.MULTILINE)
    if not match:
        raise ReleaseError(f"CHANGELOG.md has no '## [{version}]' section")
    following = NEXT_SECTION.search(changelog, match.end())
    return changelog[match.end() : following.start() if following else len(changelog)].strip() + "\n"


def versions(root: Path) -> dict[str, str]:
    found = {name: (root / name).read_text().strip() for name in PLAIN_FILES}
    for name in JSON_FILES:
        found[name] = json.loads((root / name).read_text())["version"]
    return found


def apply(root: Path, version: str, today: date) -> list[str]:
    current = current_version(root)
    if parse_version(version) <= parse_version(current):
        raise ReleaseError(f"{version} is not newer than the current {current}")
    changelog_path = root / "CHANGELOG.md"
    changelog = changelog_path.read_text()
    if re.search(rf"^## \[{re.escape(version)}\]", changelog, re.MULTILINE):
        raise ReleaseError(f"CHANGELOG.md already has a [{version}] section")
    start, end, body = unreleased(changelog)
    bump(body, current)  # refuses an empty [Unreleased]

    # Check every file before writing any, so a failure leaves nothing half-stamped.
    manifests = {}
    for name in JSON_FILES:
        text = (root / name).read_text()
        if not JSON_VERSION.search(text):
            raise ReleaseError(f'{name} has no "version" field')
        manifests[name] = JSON_VERSION.sub(lambda m: m.group(1) + version + m.group(3), text, count=1)

    for name in PLAIN_FILES:
        (root / name).write_text(version + "\n")
    for name, text in manifests.items():
        (root / name).write_text(text)
    released = f"\n## [{version}] - {today.isoformat()}\n\n{body.strip()}\n\n"
    changelog_path.write_text(changelog[:start] + released + changelog[end:])
    return [*PLAIN_FILES, *JSON_FILES, "CHANGELOG.md"]


def check(root: Path, tag: str) -> str:
    """The version a tag stands for, when every file and the changelog agree with it."""
    if not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        raise ReleaseError(f"tag must look like vX.Y.Z, got {tag!r}")
    version = tag[1:]
    mismatched = {name: value for name, value in versions(root).items() if value != version}
    if mismatched:
        listed = ", ".join(f"{name}={value}" for name, value in mismatched.items())
        raise ReleaseError(f"tag {tag} does not match the version files: {listed}")
    section((root / "CHANGELOG.md").read_text(), version)
    return version


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--apply", metavar="X.Y.Z", help="stamp this version into all files and the changelog")
    action.add_argument("--notes", metavar="X.Y.Z", help="print the changelog section of this version")
    action.add_argument("--check", metavar="vX.Y.Z", help="fail unless the files and changelog match this tag")
    parser.add_argument("--root", type=Path, default=REPO, help=argparse.SUPPRESS)
    parser.add_argument("--date", type=date.fromisoformat, default=None, help="release date (default today)")
    args = parser.parse_args(argv)
    root = args.root
    try:
        if args.notes:
            sys.stdout.write(section((root / "CHANGELOG.md").read_text(), args.notes))
            return 0
        if args.check:
            print(check(root, args.check))
            return 0
        if args.apply:
            parse_version(args.apply)
            changed = apply(root, args.apply, args.date or date.today())
            print(f"Stamped {args.apply} in: {', '.join(changed)}")
            print(
                f'Next: review the diff, commit on main ("release: {args.apply}"), then tag v{args.apply} and push the tag.'
            )
            return 0
        current = current_version(root)
        _, _, body = unreleased((root / "CHANGELOG.md").read_text())
        proposed, reason = bump(body, current)
        print(f"[Unreleased]: {reason}: {current} -> {proposed}")
        print(f"Apply with: python3 scripts/release.py --apply {proposed}")
        return 0
    except ReleaseError as error:
        print(f"release: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
