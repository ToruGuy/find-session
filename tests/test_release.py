#!/usr/bin/env python3

from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import release  # noqa: E402

CHANGELOG = """# Changelog

Intro line.

## [Unreleased]

{unreleased}
## [0.2.1] - 2026-11-13

### Fixed
- Older fix.
"""


def make_repo(tmp: str, unreleased: str, version: str = "0.2.1") -> Path:
    root = Path(tmp)
    (root / "skills/find-session").mkdir(parents=True)
    (root / ".claude-plugin").mkdir()
    for name in release.PLAIN_FILES:
        (root / name).write_text(version + "\n")
    # Same shape as the real manifests: the version is not the first key.
    (root / ".claude-plugin/plugin.json").write_text(
        json.dumps({"name": "find-session", "version": version, "keywords": ["a"]}, indent=2) + "\n"
    )
    (root / "package.json").write_text(
        json.dumps({"name": "find-session", "version": version, "engines": {"node": ">=18"}}, indent=2) + "\n"
    )
    (root / "CHANGELOG.md").write_text(CHANGELOG.format(unreleased=unreleased))
    return root


def run(*args: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = release.main(list(args))
    return code, out.getvalue(), err.getvalue()


def copy_of_the_repository(tmp: str) -> Path:
    """The real version files and changelog; right after a release, with one entry to release."""
    root = Path(tmp)
    for name in (*release.PLAIN_FILES, *release.JSON_FILES, "CHANGELOG.md"):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / name, root / name)
    changelog = root / "CHANGELOG.md"
    text = changelog.read_text()
    if "## [Unreleased]\n\n## [" in text:
        changelog.write_text(text.replace("## [Unreleased]\n", "## [Unreleased]\n\n### Fixed\n- A bug.\n", 1))
    return root


class ProposeTests(unittest.TestCase):
    def propose(self, unreleased: str, version: str = "0.2.1") -> tuple[int, str, str]:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp, unreleased, version)
            return run("--root", str(root))

    def test_added_or_changed_bumps_minor(self):
        for group in ("Added", "Changed", "Removed", "Deprecated"):
            code, out, _ = self.propose(f"### {group}\n- Something.\n\n### Fixed\n- A bug.\n")
            self.assertEqual(code, 0)
            self.assertIn("0.2.1 -> 0.3.0", out)

    def test_only_fixes_bump_patch(self):
        code, out, _ = self.propose("### Fixed\n- A bug.\n\n### Security\n- A hole.\n")
        self.assertEqual(code, 0)
        self.assertIn("0.2.1 -> 0.2.2", out)

    def test_breaking_bumps_minor_before_1_0_and_major_after(self):
        breaking = "### Fixed\n- **BREAKING:** the output changed.\n"
        self.assertIn("0.2.1 -> 0.3.0", self.propose(breaking)[1])
        self.assertIn("1.4.2 -> 2.0.0", self.propose(breaking, "1.4.2")[1])
        self.assertIn("1.4.2 -> 1.5.0", self.propose("### Added\n- New.\n", "1.4.2")[1])

    def test_empty_unreleased_is_refused(self):
        code, _, err = self.propose("### Added\n\n")
        self.assertEqual(code, 1)
        self.assertIn("empty", err)

    def test_unknown_group_is_refused(self):
        code, _, err = self.propose("### Misc\n- Something.\n")
        self.assertEqual(code, 1)
        self.assertIn("misc", err)

    def test_real_changelog_gives_a_proposal(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out, err = run("--root", str(copy_of_the_repository(tmp)))
        self.assertEqual(code, 0, err)
        self.assertIn("Apply with: python3 scripts/release.py --apply", out)


class ApplyTests(unittest.TestCase):
    def test_apply_stamps_every_file_and_moves_the_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp, "### Added\n- New thing.\n\n### Fixed\n- A bug.\n")
            plugin_before = (root / ".claude-plugin/plugin.json").read_text()
            code, out, err = run("--root", str(root), "--apply", "0.3.0", "--date", "2026-12-01")
            self.assertEqual(code, 0, err)
            self.assertEqual(set(release.versions(root).values()), {"0.3.0"})
            # Only the version line of a manifest changes.
            self.assertEqual(
                (root / ".claude-plugin/plugin.json").read_text(), plugin_before.replace('"0.2.1"', '"0.3.0"')
            )
            changelog = (root / "CHANGELOG.md").read_text()
            self.assertIn(
                "## [Unreleased]\n\n## [0.3.0] - 2026-12-01\n\n### Added\n- New thing.\n\n### Fixed\n- A bug.\n\n"
                "## [0.2.1] - 2026-11-13",
                changelog,
            )
            self.assertTrue(changelog.startswith("# Changelog\n\nIntro line.\n"))
            self.assertEqual(release.section(changelog, "0.3.0"), "### Added\n- New thing.\n\n### Fixed\n- A bug.\n")
            # After the stamp, the tag check passes and a second stamp has nothing to release.
            self.assertEqual(release.check(root, "v0.3.0"), "0.3.0")
            code, _, err = run("--root", str(root), "--apply", "0.4.0")
            self.assertEqual(code, 1)
            self.assertIn("empty", err)

    def test_apply_refuses_bad_versions_and_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp, "### Fixed\n- A bug.\n")
            before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
            for bad in ("0.2.1", "0.1.9", "1.0", "v0.3.0"):
                code, _, err = run("--root", str(root), "--apply", bad)
                self.assertEqual(code, 1, bad)
                self.assertTrue(err.startswith("release: "))
            self.assertEqual({path: path.read_bytes() for path in root.rglob("*") if path.is_file()}, before)

    def test_apply_refuses_an_existing_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp, "### Fixed\n- A bug.\n\n## [0.2.5] - 2026-11-20\n\n### Fixed\n- x\n")
            code, _, err = run("--root", str(root), "--apply", "0.2.5")
            self.assertEqual(code, 1)
            self.assertIn("already has", err)

    def test_works_on_a_copy_of_the_real_repository(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = copy_of_the_repository(tmp)
            code, out, _ = run("--root", str(root))
            self.assertEqual(code, 0)
            proposed = out.split(" -> ")[-1].split()[0]
            code, _, err = run("--root", str(root), "--apply", proposed)
            self.assertEqual(code, 0, err)
            self.assertEqual(set(release.versions(root).values()), {proposed})
            self.assertEqual(json.loads((root / "package.json").read_text())["version"], proposed)
            self.assertIn("## [Unreleased]\n\n## [", (root / "CHANGELOG.md").read_text())


class CheckTests(unittest.TestCase):
    def test_check_rejects_a_tag_that_does_not_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp, "### Fixed\n- A bug.\n")
            self.assertEqual(run("--root", str(root), "--check", "v0.2.1")[0], 0)
            code, _, err = run("--root", str(root), "--check", "v0.2.2")
            self.assertEqual(code, 1)
            self.assertIn("does not match", err)
            (root / "package.json").write_text('{"version": "0.2.0"}\n')
            code, _, err = run("--root", str(root), "--check", "v0.2.1")
            self.assertEqual(code, 1)
            self.assertIn("package.json=0.2.0", err)
            self.assertEqual(run("--root", str(root), "--check", "0.2.1")[0], 1)

    def test_check_needs_a_changelog_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp, "### Fixed\n- A bug.\n", version="0.2.2")
            code, _, err = run("--root", str(root), "--check", "v0.2.2")
            self.assertEqual(code, 1)
            self.assertIn("no '## [0.2.2]' section", err)

    def test_notes_print_one_section(self):
        code, out, _ = run("--notes", "0.1.0")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("### Added"))
        self.assertNotIn("## [", out)


class RepositoryVersionTests(unittest.TestCase):
    def test_all_version_copies_are_equal(self):
        found = release.versions(REPO)
        self.assertEqual(len(set(found.values())), 1, found)
        self.assertEqual(
            set(found), {"VERSION", "skills/find-session/VERSION", ".claude-plugin/plugin.json", "package.json"}
        )

    def test_release_date_defaults_to_today(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp, "### Fixed\n- A bug.\n")
            run("--root", str(root), "--apply", "0.2.2")
            self.assertIn(f"## [0.2.2] - {date.today().isoformat()}", (root / "CHANGELOG.md").read_text())


if __name__ == "__main__":
    unittest.main()
