"""Config file, hints, resume label and history home overrides."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import run_full, write_jsonl

from find_session import (
    cli,
    text,
)
from find_session.output import ENTRY_SCRIPT


class HintAndConfigTests(unittest.TestCase):
    run_cli = staticmethod(run_full)

    def make_home(self, tmp: str) -> Path:
        home = Path(tmp) / "history"
        write_jsonl(
            home / ".claude/projects/-Users-alex-my-app/hint-1.jsonl",
            [
                {"cwd": "/Users/alex/my app", "message": {"role": "user", "content": "HINTTOPIC first"}},
                {"message": {"role": "user", "content": "HINTTOPIC last"}},
            ],
        )
        return home

    def write_config(self, tmp: str, data: object) -> Path:
        path = Path(tmp) / "config.json"
        path.write_text(json.dumps(data) if not isinstance(data, str) else data)
        return path

    def test_default_output_has_generic_hints_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = self.make_home(tmp)
            code, output = self.run_cli("--home", str(home), "search", "HINTTOPIC")
            self.assertEqual(code, 0)
            self.assertIn("Session: hint-1", output)
            self.assertIn("Start: HINTTOPIC first", output)
            self.assertIn("Last request: HINTTOPIC last", output)
            self.assertIn("Resume: cd '/Users/alex/my app' && claude --resume hint-1", output)
            script = str(ENTRY_SCRIPT)
            self.assertIn(f"Inspect: python3 {script} inspect claude:hint-1", output)
            self.assertEqual(output.count("\nResume"), 1)

    def test_npx_wrapper_names_its_own_command_in_hints(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = self.make_home(tmp)
            with mock.patch.dict(os.environ, {"FIND_SESSION_COMMAND": "npx find-session"}):
                code, output = self.run_cli("--home", str(home), "search", "HINTTOPIC")
            self.assertEqual(code, 0)
            self.assertIn("Inspect: npx find-session inspect claude:hint-1", output)

    def test_configured_hints_and_resume_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = self.make_home(tmp)
            config = self.write_config(
                tmp,
                {
                    "home": str(home),
                    "resume_label": "Resume on laptop",
                    "hints": [
                        {"label": "Inspect on server", "command": "ssh box find-session inspect {selector}"},
                        {"label": "Open", "command": "{resume} --fork {unknown}"},
                        {"label": "broken"},
                    ],
                },
            )
            with mock.patch.dict(os.environ, {"FIND_SESSION_CONFIG": str(config)}):
                code, output = self.run_cli("search", "HINTTOPIC")
                self.assertEqual(code, 0)
                self.assertIn("Resume on laptop: cd '/Users/alex/my app' && claude --resume hint-1", output)
                self.assertIn("Inspect on server: ssh box find-session inspect claude:hint-1", output)
                self.assertIn("Open: cd '/Users/alex/my app' && claude --resume hint-1 --fork {unknown}", output)
                self.assertNotIn("broken", output)
                code, output = self.run_cli("search", "HINTTOPIC", "--json")
                record = json.loads(output)["results"]["Claude"][0]
                self.assertEqual(record["hints"][0]["label"], "Inspect on server")
                self.assertEqual(record["cwd"], "/Users/alex/my app")
                self.assertEqual(record["last"], "HINTTOPIC last")

    def test_home_from_env_and_book_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = self.make_home(tmp)
            with mock.patch.dict(os.environ, {"FIND_SESSION_HOME": str(home)}):
                code, output = self.run_cli("search", "HINTTOPIC")
            self.assertIn("Session: hint-1", output)
            code, output = self.run_cli("--book", str(home), "inspect", "hint-1")
            self.assertEqual(code, 0)
            self.assertIn("USER: HINTTOPIC first", output)
            self.assertIn("Resume: cd '/Users/alex/my app' && claude --resume hint-1", output)

    def test_broken_config_is_ignored_with_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = self.make_home(tmp)
            config = self.write_config(tmp, "{not json")
            with mock.patch.dict(os.environ, {"FIND_SESSION_CONFIG": str(config)}):
                code, output = self.run_cli("--home", str(home), "search", "HINTTOPIC")
            self.assertEqual(code, 0)
            self.assertIn("ignoring config", output)
            self.assertIn("Session: hint-1", output)

    def test_recent_include_from_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = self.make_home(tmp)
            (home / "notes").mkdir()
            (home / "notes/state.md").write_text("STATE_FROM_CONFIG")
            config = self.write_config(tmp, {"recent_include": ["notes/*.md"]})
            with mock.patch.dict(os.environ, {"FIND_SESSION_CONFIG": str(config)}):
                code, output = self.run_cli("--home", str(home), "recent", "--hours", "1")
            self.assertEqual(code, 0)
            self.assertIn("STATE_FROM_CONFIG", output)
            self.assertIn("HINTTOPIC first", output)

    def test_index_to_stdout(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = self.make_home(tmp)
            code, output = self.run_cli("--home", str(home), "index")
            self.assertEqual(code, 0)
            self.assertRegex(output, r"\nclaude:hint-1 [\d-]+ [\d:]+ ~/my app · 2 msgs · HINTTOPIC first\n")

    def test_short_project_hides_any_home_directory(self):
        self.assertEqual(text.short_project("/home/sam/work/site/"), "~/work/site")
        self.assertEqual(text.short_project("/Users/alex/code"), "~/code")
        self.assertEqual(text.short_project("/root/srv"), "~/srv")
        self.assertEqual(text.short_project("/srv/app"), "/srv/app")
        self.assertEqual(text.short_project(""), "unknown")

    def test_non_ascii_query_terms(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            write_jsonl(
                home / ".claude/projects/-Users-alex-x/accents.jsonl",
                [{"message": {"role": "user", "content": "Zażółć gęślą jaźń"}}],
            )
            code, output = self.run_cli("--home", tmp, "search", "gęślą")
            self.assertIn("Session: accents", output)

    def test_version_flag(self):
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured), self.assertRaises(SystemExit):
            cli.main(["--version"])
        self.assertRegex(captured.getvalue(), r"find-session \d+\.\d+\.\d+")


if __name__ == "__main__":
    unittest.main()
