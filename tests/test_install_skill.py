#!/usr/bin/env python3
"""The installer of a clone (scripts/install_skill.py)."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import helpers  # noqa: F401  (clears the environment: no real cache, config or history)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import install_skill  # noqa: E402

SKILL_DIR = Path(__file__).resolve().parents[1] / "skills" / "find-session"
CASES = json.loads((Path(__file__).resolve().parent / "install_cases.json").read_text())["cases"]
LINKS = {
    "claude": ".claude/skills/find-session",
    "codex": ".codex/skills/find-session",
    "omp": ".omp/agent/skills/find-session",
}
ALL_TOOLS = (".claude", ".codex", ".omp")


def run(home: Path, *extra: str) -> tuple[int, str]:
    """Run the installer for `home`; with --home it only links (no summary, no index)."""
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
        code = install_skill.main(["--home", str(home), *extra])
    return code, captured.getvalue()


def make_home(tmp: str, *dirs: str) -> Path:
    home = Path(tmp).resolve() / "home"
    home.mkdir()
    for name in dirs:
        (home / name).mkdir(parents=True)
    return home


def other_install(path: Path) -> None:
    """A find-session skill folder as another installer leaves it."""
    (path / "scripts").mkdir(parents=True)
    (path / "SKILL.md").write_text("---\nname: find-session\ndescription: synthetic\n---\n")
    (path / "scripts" / "sessions.py").write_text("")
    (path / "VERSION").write_text("9.9.9\n")


def plugin_install(home: Path, plugin: dict) -> None:
    """find-session as Claude Code records a plugin install."""
    other_install(home / plugin["path"] / "skills" / "find-session")
    name = "find-session@find-session"
    entry = {"scope": plugin["scope"], "installPath": str(home / plugin["path"]), "version": "9.9.9"}
    listing = {"version": 2, "plugins": {name: [entry], "other@market": [{"scope": "user", "installPath": "/nowhere"}]}}
    (home / ".claude/plugins/installed_plugins.json").write_text(json.dumps(listing))
    if plugin.get("disabled"):
        (home / ".claude/settings.json").write_text(json.dumps({"enabledPlugins": {name: False}}))


class SharedCases(unittest.TestCase):
    """tests/install_cases.json: the same cases the npx installer is tested with."""

    def setUp(self):
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("PI_CODING_AGENT_DIR", None)

    def test_cases(self):
        for case in CASES:
            with self.subTest(case["name"]), tempfile.TemporaryDirectory() as tmp:
                home = make_home(tmp, *case["dirs"])
                for path in case.get("foreign", []):
                    (home / path).mkdir(parents=True)
                    (home / path / "SKILL.md").write_text("---\nname: something-else\n---\n")
                if case.get("other_install"):
                    other_install(home / case["other_install"])
                if case.get("plugin"):
                    plugin_install(home, case["plugin"])
                for link, target in case.get("links", {}).items():
                    (home / link).parent.mkdir(parents=True, exist_ok=True)
                    (home / link).symlink_to(home / target, target_is_directory=True)

                code, output = run(home, *case["args"])

                self.assertEqual(code, case["exit"], output)
                tools = tuple(f"{prefix}{name}: " for prefix in ("", "dry-run: ") for name in LINKS)
                lines = [line for line in output.splitlines() if line.startswith(tools)]
                skill = install_skill.display(SKILL_DIR, home)
                self.assertEqual(len(lines), len(case["out"]), output)
                for line, expected in zip(lines, case["out"]):
                    expected = expected.replace("{skill}", skill)
                    if expected.endswith("*"):
                        self.assertTrue(line.startswith(expected[:-1]), (line, expected))
                    else:
                        self.assertEqual(line, expected)
                if case.get("stderr"):
                    self.assertIn(case["stderr"], output)
                for name, link in LINKS.items():
                    ours = (home / link).is_symlink() and (home / link).resolve() == SKILL_DIR
                    self.assertEqual(ours, name in case["linked"], f"{name} link")
                for path in case.get("absent", []):
                    self.assertFalse((home / path).exists() or (home / path).is_symlink(), path)


class InstallSkillTests(unittest.TestCase):
    def setUp(self):
        # A real omp agent dir on the developer machine must never be touched.
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("PI_CODING_AGENT_DIR", None)

    def test_explicit_home_ignores_agent_dir_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            real_agent = Path(tmp) / "real-agent"
            with mock.patch.dict(os.environ, {"PI_CODING_AGENT_DIR": str(real_agent)}):
                code, _ = run(home, "--harness", "omp")
            self.assertEqual(code, 0)
            self.assertTrue((home / ".omp/agent/skills/find-session").is_symlink())
            self.assertFalse(real_agent.exists())

    def test_install_twice_links_all_three_harnesses(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = make_home(tmp, *ALL_TOOLS)
            (home / ".claude/skills/other").mkdir(parents=True)
            first, _ = run(home)
            second, output = run(home)
            self.assertEqual((first, second), (0, 0))
            for root in (".claude/skills", ".codex/skills", ".omp/agent/skills"):
                link = home / root / "find-session"
                self.assertTrue(link.is_symlink())
                self.assertEqual(link.resolve(), SKILL_DIR)
            self.assertEqual(output.count("already linked"), 3)
            self.assertTrue((home / ".claude/skills/other").is_dir())

    def test_conflict_is_preserved_unless_replace_keeps_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = make_home(tmp)
            existing = home / ".omp/agent/skills/find-session"
            existing.mkdir(parents=True)
            (existing / "SKILL.md").write_text("user version")
            code, output = run(home)
            self.assertEqual(code, 1)
            self.assertIn("conflict", output)
            self.assertFalse(existing.is_symlink())

            code, _ = run(home, "--replace")
            self.assertEqual(code, 0)
            self.assertTrue(existing.is_symlink())
            backups = list((home / ".omp/agent/skill-backups").glob("find-session-*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual((backups[0] / "SKILL.md").read_text(), "user version")

    def test_replace_leaves_only_active_skill_in_discovery_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = make_home(tmp, *ALL_TOOLS)
            old_skill = Path(tmp) / "old-checkout/find-session"
            old_skill.mkdir(parents=True)
            (old_skill / "SKILL.md").write_text("old linked version")
            claude_root = home / ".claude/skills"
            claude_root.mkdir(parents=True)
            (claude_root / "find-session").symlink_to(old_skill, target_is_directory=True)
            omp_root = home / ".omp/agent/skills"
            (omp_root / "find-session").mkdir(parents=True)
            (omp_root / "find-session/SKILL.md").write_text("old copied version")

            code, output = run(home, "--replace")
            self.assertEqual(code, 0)
            # Second replace run: already linked, no new backup.
            code, _ = run(home, "--replace")
            self.assertEqual(code, 0)

            for root in (claude_root, omp_root, home / ".codex/skills"):
                entries = sorted(path.name for path in root.iterdir())
                self.assertEqual(entries, ["find-session"])
                self.assertEqual((root / "find-session").resolve(), SKILL_DIR)
                skill_files = sorted(root.glob("*/SKILL.md"))
                self.assertEqual(len(skill_files), 1)

            claude_backups = list((home / ".claude/skill-backups").iterdir())
            self.assertEqual(len(claude_backups), 1)
            self.assertTrue(claude_backups[0].is_symlink())
            self.assertEqual(claude_backups[0].resolve(), old_skill.resolve())
            omp_backups = list((home / ".omp/agent/skill-backups").iterdir())
            self.assertEqual(len(omp_backups), 1)
            self.assertEqual((omp_backups[0] / "SKILL.md").read_text(), "old copied version")
            self.assertFalse((home / ".codex/skill-backups").exists())

    def test_backup_names_never_collide(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "skills"
            fixed = install_skill.datetime(2026, 9, 22, 12, 0, 0)
            with mock.patch.object(install_skill, "datetime") as clock:
                clock.now.return_value = fixed
                for version in ("first", "second"):
                    (root / "find-session").mkdir(parents=True)
                    (root / "find-session/SKILL.md").write_text(version)
                    ok, _ = install_skill.install(root, replace=True, dry_run=False, home=Path(tmp))
                    self.assertTrue(ok)
                    (root / "find-session").unlink()
            backups = sorted((Path(tmp) / "skill-backups").iterdir())
            self.assertEqual(len(backups), 2)
            self.assertEqual(sorted((path / "SKILL.md").read_text() for path in backups), ["first", "second"])

    def test_dry_run_replace_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = make_home(tmp)
            existing = home / ".omp/agent/skills/find-session"
            existing.mkdir(parents=True)
            code, output = run(home, "--replace", "--dry-run", "--harness", "omp")
            self.assertEqual(code, 0)
            self.assertIn("dry-run", output)
            self.assertFalse(existing.is_symlink())
            self.assertFalse((home / ".omp/agent/skill-backups").exists())

    def test_skill_name_does_not_depend_on_checkout_directory(self):
        self.assertEqual(install_skill.SKILL_NAME, "find-session")
        self.assertTrue((install_skill.SKILL_DIR / "SKILL.md").is_file())
        self.assertTrue((install_skill.SKILL_DIR / "scripts" / "sessions.py").is_file())
        # What the installers take for "a find-session skill" must hold for the real one.
        self.assertEqual(
            install_skill.installed_version(install_skill.SKILL_DIR),
            (install_skill.REPO / "VERSION").read_text().strip(),
        )

    def test_skill_folder_carries_the_same_version_as_the_repository(self):
        version = (install_skill.REPO / "VERSION").read_text()
        self.assertEqual((install_skill.SKILL_DIR / "VERSION").read_text(), version)
        plugin = json.loads((install_skill.REPO / ".claude-plugin" / "plugin.json").read_text())
        self.assertEqual(plugin["version"], version.strip())
        package = json.loads((install_skill.REPO / "package.json").read_text())
        self.assertEqual(package["version"], version.strip())

    def test_link_to_the_old_repository_root_layout_is_moved(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = make_home(tmp)
            root = home / ".claude/skills"
            root.mkdir(parents=True)
            (root / "find-session").symlink_to(install_skill.REPO, target_is_directory=True)
            code, output = run(home, "--harness", "claude")
            self.assertEqual(code, 0)
            self.assertIn("relinked", output)
            self.assertEqual((root / "find-session").resolve(), SKILL_DIR)

    def test_omp_agent_dir_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            agent = Path(tmp) / "agent"
            code, _ = run(home, "--omp-agent-dir", str(agent), "--harness", "omp")
            self.assertEqual(code, 0)
            self.assertTrue((agent / "skills/find-session").is_symlink())
            self.assertFalse((home / ".claude").exists())

    def test_an_omp_agent_dir_that_exists_counts_as_omp_being_there(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = make_home(tmp)
            agent = Path(tmp) / "agent"
            agent.mkdir()
            code, output = run(home, "--omp-agent-dir", str(agent))
            self.assertEqual(code, 0, output)
            self.assertTrue((agent / "skills/find-session").is_symlink())

    def test_old_python_is_refused_before_anything_is_linked(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = make_home(tmp, ".claude")
            with mock.patch.object(install_skill.sys, "version_info", (3, 8, 20, "final", 0)):
                code, output = run(home)
            self.assertEqual(code, install_skill.EXIT_NO_PYTHON)
            self.assertIn("Python 3.9 or newer is needed", output)
            self.assertFalse((home / ".claude/skills").exists())


class SummaryAndIndexTests(unittest.TestCase):
    """After linking: what the skill sees, then the index (in the user's own home only)."""

    def test_a_given_home_only_gets_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = make_home(tmp, ".claude")
            with mock.patch.object(install_skill, "run_helper", side_effect=AssertionError("helper was run")):
                code, output = run(home)
            self.assertEqual(code, 0)
            self.assertNotIn("What the skill can see", output)

    def test_the_real_install_shows_sources_then_builds_the_index_last(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = make_home(tmp, ".claude")
            calls = []

            def helper(*arguments: str) -> int:
                calls.append(arguments)
                return 0

            captured = io.StringIO()
            with mock.patch.object(install_skill.Path, "home", return_value=home):
                with mock.patch.object(install_skill, "run_helper", helper):
                    with contextlib.redirect_stdout(captured):
                        code = install_skill.main([])
                    self.assertEqual(code, 0)
                    self.assertEqual(calls, [("sources",), ("build-index",)])
                    output = captured.getvalue()
                    self.assertLess(output.index("claude: linked ~/.claude/skills/find-session"), output.index("What"))
                    self.assertIn("--no-index skips it", output)
                    self.assertTrue(output.rstrip().endswith("pull"), output)
                    calls.clear()
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(install_skill.main(["--no-index"]), 0)
                    self.assertEqual(calls, [("sources",)])

    def test_the_helper_really_builds_an_index_in_the_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "history"
            helpers.write_jsonl(
                home / ".claude/projects/-x/installed.jsonl", helpers.claude_turns("INSTALLTOPIC", "ok")
            )
            cache = Path(tmp) / "cache"
            env = {**os.environ, "FIND_SESSION_HOME": str(home), "FIND_SESSION_CACHE": str(cache)}
            really_run = subprocess.run
            with mock.patch.object(
                install_skill.subprocess,
                "run",
                lambda command, **options: really_run(command, env=env, capture_output=True, text=True),
            ):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(install_skill.show_and_index(build_index=True), 0)
            self.assertEqual(len(list(cache.glob("index-*.sqlite3"))), 1)


if __name__ == "__main__":
    unittest.main()
