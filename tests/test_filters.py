"""--since / --until / --project and the match line."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import datetime_from_iso, run_full, write_jsonl

from find_session import (
    cli,
    timefilter,
)


class FilterAndMatchTests(unittest.TestCase):
    run_cli = staticmethod(run_full)

    def make_home(self, tmp: str) -> Path:
        home = Path(tmp)
        for name, cwd, started in (
            ("old-1", "/Users/alex/clients/acme-shop", "2026-09-01T09:00:00Z"),
            ("new-1", "/Users/alex/clients/acme-shop", "2026-09-20T09:00:00Z"),
            ("other-1", "/Users/alex/garden", "2026-09-21T09:00:00Z"),
        ):
            path = home / f".claude/projects/-Users-alex-{name}/{name}.jsonl"
            write_jsonl(
                path,
                [
                    {
                        "timestamp": started,
                        "cwd": cwd,
                        "message": {"role": "user", "content": f"Start {name} FILTERTOPIC"},
                    },
                    {
                        "timestamp": started,
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "We decided the FILTERTOPIC budget is final."}],
                        },
                    },
                ],
            )
            moment = datetime_from_iso(started).timestamp() + 3600
            os.utime(path, (moment, moment))
        return home

    def fixed_now(self):
        return mock.patch.object(timefilter, "now", return_value=datetime_from_iso("2026-09-22T12:00:00Z").astimezone())

    def test_since_until_and_relative_times(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.make_home(tmp)
            with self.fixed_now():
                _, output = self.run_cli("--home", tmp, "search", "FILTERTOPIC", "--since", "2026-09-15", "--json")
                ids = {r["session_id"] for r in json.loads(output)["results"]["Claude"]}
                self.assertEqual(ids, {"new-1", "other-1"})
                _, output = self.run_cli("--home", tmp, "search", "FILTERTOPIC", "--until", "2026-09-20", "--json")
                ids = {r["session_id"] for r in json.loads(output)["results"]["Claude"]}
                self.assertEqual(ids, {"old-1", "new-1"})
                _, output = self.run_cli("--home", tmp, "search", "FILTERTOPIC", "--since", "3d", "--json")
                ids = {r["session_id"] for r in json.loads(output)["results"]["Claude"]}
                self.assertEqual(ids, {"new-1", "other-1"})
                _, output = self.run_cli("--home", tmp, "search", "FILTERTOPIC", "--since", "yesterday")
                self.assertIn("Session: other-1", output)
                self.assertNotIn("Session: new-1", output)
                self.assertIn("[since 2026-09-21 00:00]", output)
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                cli.main(["--home", tmp, "search", "x", "--since", "last tuesday"])

    def test_project_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.make_home(tmp)
            _, output = self.run_cli("--home", tmp, "search", "FILTERTOPIC", "--project", "ACME", "--json")
            ids = {r["session_id"] for r in json.loads(output)["results"]["Claude"]}
            self.assertEqual(ids, {"old-1", "new-1"})
            with mock.patch.object(timefilter.os, "getcwd", return_value="/Users/alex/garden"):
                _, output = self.run_cli("--home", tmp, "search", "FILTERTOPIC", "--project", ".", "--json")
            ids = {r["session_id"] for r in json.loads(output)["results"]["Claude"]}
            self.assertEqual(ids, {"other-1"})

    def test_match_line_shows_where_the_words_are(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.make_home(tmp)
            _, output = self.run_cli("--home", tmp, "search", "budget final", "--project", "garden")
            self.assertRegex(
                output, r"Match: \[#2 · \d\d-\d\d \d\d:\d\d AGENT\] We decided the FILTERTOPIC budget is final\."
            )
            _, output = self.run_cli("--home", tmp, "search", "Start other-1", "--project", "garden")
            self.assertNotIn("Match:", output)


if __name__ == "__main__":
    unittest.main()
