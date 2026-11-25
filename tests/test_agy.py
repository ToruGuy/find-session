"""Antigravity CLI (agy): transcripts, the summaries database, subagents and selectors."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import (
    AGY_ROOT,
    agy_main_steps,
    agy_request,
    agy_step,
    agy_summaries,
    run_full,
    run_raw,
    write_agy,
)

from find_session import discovery, tools
from find_session import index as index_module
from find_session.parsers import agy

MAIN = "0a9b1c2d-0000-4000-8000-00000000a001"
CHILD = "0a9b1c2d-0000-4000-8000-00000000a002"
OTHER = "0a9b1c2d-0000-4000-8000-00000000a003"
CWD = "/Users/alex/projects/demo"


class AgyCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "history"
        self.cache_dir = Path(tmp.name) / "cache"
        self.main = write_agy(self.home, MAIN, agy_main_steps())
        self.db = agy_summaries(self.home, (MAIN, "", CWD, ""))

    def with_index(self):
        patcher = mock.patch.dict(os.environ, {"FIND_SESSION_CACHE": str(self.cache_dir)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def search_json(self, query: str = "AGYTOPIC", *extra: str) -> dict:
        code, output = run_raw("--home", str(self.home), "search", query, "--json", *extra)
        self.assertEqual(code, 0, output)
        return json.loads(output)

    def set_title(self, conversation_id: str, title: str) -> None:
        db = sqlite3.connect(self.db)
        with db:
            db.execute(
                "UPDATE conversation_summaries SET title = ? WHERE conversation_id = ?", (title, conversation_id)
            )
        db.close()


class AgyParseTests(AgyCase):
    def test_requests_answers_and_commands_only(self):
        session = agy.parse_agy(self.main)
        self.assertEqual(session.source, "agy")
        self.assertEqual(session.session_id, MAIN)
        self.assertEqual((session.cwd, session.project, session.kind), (CWD, "~/projects/demo", "main"))
        self.assertEqual(session.user_messages, ["Plan the AGYTOPIC rollout"])
        self.assertEqual(session.assistant_messages, ["Rollout planned."])
        commands = [text for _, text in session.tools]
        self.assertEqual(commands[:2], ["read_file /Users/alex/projects/demo/AGYREADFILE.md", "$ AGYCMD --check"])
        self.assertEqual(commands[2], "write_to_file /tmp/AGYWRITTEN.txt")
        self.assertEqual(session.last_stamp, "2026-09-22T10:00:04Z")
        code, output = run_full("--home", str(self.home), "inspect", f"agy:{MAIN}", "--tools")
        self.assertEqual(code, 0, output)
        self.assertIn("$ AGYCMD --check", output)
        self.assertIn(f"Resume: cd {CWD} && agy --conversation {MAIN}", output)
        for leaked in ("SYNTH_", "Created At", "Showing lines", "USER_REQUEST"):
            self.assertNotIn(leaked, output)

    def test_shortened_transcript_is_read_when_the_full_one_is_missing(self):
        self.main.unlink()
        # transcript.jsonl: values JSON-encoded, and a cut list of calls kept as its text.
        calls = [{"name": "run_command", "args": {"CommandLine": '"AGYSHORTCMD --x"', "WaitMsBeforeAsync": "5000"}}]
        write_agy(
            self.home,
            MAIN,
            [
                agy_request(0, "Plan the AGYTOPIC rollout"),
                agy_step(1, "PLANNER_RESPONSE", tool_calls=repr(calls)),
                agy_step(2, "PLANNER_RESPONSE", tool_calls="[{'name': 'run_command', 'args': {'CommandLine': '\"cut"),
            ],
            name="transcript.jsonl",
        )
        (candidate,) = discovery.agy_files(self.home / AGY_ROOT)
        self.assertTrue(candidate.location.endswith("/transcript.jsonl"))
        session = agy.parse_agy(candidate.path)
        self.assertEqual([text for _, text in session.tools], ["$ AGYSHORTCMD --x"])
        # A request cut short before its closing tag is kept.
        self.assertEqual(agy.agy_user_text("<USER_REQUEST>\nPlan the AGYTOPIC roll"), "Plan the AGYTOPIC roll")

    def test_conversations_without_a_transcript_or_a_row_and_stray_files(self):
        (self.home / AGY_ROOT / "brain" / OTHER).mkdir(parents=True)
        (self.home / AGY_ROOT / "brain" / "notes.txt").write_text("SYNTH")
        write_agy(self.home, CHILD, [agy_request(0, "No row for AGYTOPIC")])
        found = {c.session_id for c in discovery.discover(self.home)}
        self.assertEqual(found, {MAIN, CHILD})
        # Not in the database: no working directory, nothing else lost.
        session = agy.parse_agy(self.home / AGY_ROOT / "brain" / CHILD / ".system_generated/logs/transcript_full.jsonl")
        self.assertEqual(
            (session.cwd, session.project, session.user_messages), ("", "unknown", ["No row for AGYTOPIC"])
        )
        # No database at all.
        self.db.unlink()
        self.assertEqual(agy.parse_agy(self.main).cwd, "")
        self.assertEqual(len(self.search_json()["results"]["agy"]), 2)

    def test_root_override_and_sources(self):
        moved = self.home.parent / "elsewhere"
        (self.home / AGY_ROOT).rename(moved)
        with mock.patch.dict(os.environ, {"AGY_SESSIONS_ROOT": str(moved)}):
            self.assertEqual([c.session_id for c in discovery.discover(self.home)], [MAIN])
            _, output = run_raw("--home", str(self.home), "sources")
        self.assertIn("agy: 1 session, 0 subagents\n", output)

    def test_history_lookups_are_not_indexed(self):
        self.assertTrue(tools.HISTORY_LOOKUP.search("ls ~/.gemini/antigravity-cli/brain"))
        self.assertFalse(tools.HISTORY_LOOKUP.search("ls ~/.gemini/settings.json"))


class AgySearchTests(AgyCase):
    def test_own_ranking_selector_resume_and_title(self):
        self.set_title(MAIN, "AGY rollout plan")
        data = self.search_json()
        self.assertEqual(data["counts"]["agy"], 1)
        (record,) = data["results"]["agy"]
        self.assertEqual(
            (record["session_id"], record["title"], record["project"]), (MAIN, "AGY rollout plan", "~/projects/demo")
        )
        _, compact = run_raw("--home", str(self.home), "search", "AGYCMD", "--source", "agy")
        self.assertIn("agy 1", compact.splitlines()[0])
        self.assertRegex(compact, r"\nagy:0a9b1c2d [\d-]+ [\d:]+ ~/projects/demo · 2 msgs\n")
        self.assertIn(" title: AGY rollout plan\n", compact)
        for word in ("SYNTH_REASONING", "SYNTH_TOOL_OUTPUT", "SYNTH_REMINDER", "SYNTH_METADATA", "SYNTH_BODY"):
            self.assertIn("no matches", run_raw("--home", str(self.home), "search", word)[1], word)

    def test_a_selector_matches_the_id_not_the_shared_file_name(self):
        code, output = run_raw("--home", str(self.home), "inspect", "transcript_full")
        self.assertEqual(code, 1, output)
        code, output = run_raw("--home", str(self.home), "inspect", MAIN[-6:])
        self.assertEqual(code, 0, output)

    def test_subagent_has_its_own_ranking_and_is_listed_under_its_parent(self):
        write_agy(
            self.home,
            CHILD,
            [
                agy_request(0, "Check the AGYTOPIC numbers", source="MODEL"),
                agy_step(1, "PLANNER_RESPONSE", content="Numbers checked."),
            ],
        )
        agy_summaries(self.home, (CHILD, "", CWD, MAIN))
        for leading in ((), ("--no-cache",)):
            if not leading:
                self.with_index()
            code, output = run_raw("--home", str(self.home), *leading, "search", "AGYTOPIC", "--json")
            data = json.loads(output)
            self.assertEqual([r["session_id"] for r in data["results"]["agy"]], [MAIN], leading)
            self.assertEqual(data["hidden_subagent_matches"], 1, leading)
            data = self.search_json("AGYTOPIC", "--children")
            self.assertEqual([r["session_id"] for r in data["results"]["subagents"]], [CHILD], leading)
            _, child = run_full("--home", str(self.home), *leading, "inspect", f"agy:{CHILD}")
            self.assertIn("DELEGATED TASK", child)
            self.assertNotIn("Resume:", child)
            _, parent = run_full("--home", str(self.home), *leading, "inspect", f"agy:{MAIN}")
            self.assertIn(f"agy:{CHILD}", parent)
        _, output = run_raw("--home", str(self.home), "sources")
        self.assertIn("agy: 1 session, 1 subagent\n", output)

    def test_every_command_prints_the_same_with_and_without_the_index(self):
        self.set_title(MAIN, "AGY rollout plan")
        commands = [
            ["search", "AGYTOPIC"],
            ["search", "AGYTOPIC", "--full"],
            ["inspect", f"agy:{MAIN}", "--tools"],
            ["index"],
            ["recent", "--hours", "1000000"],
        ]

        def outputs(*leading: str) -> list[str]:
            return [
                re.sub(r"(built|collected) [\d-]+ [\d:]+", "NOW", run_raw("--home", str(self.home), *leading, *c)[1])
                for c in commands
            ]

        expected = outputs("--no-cache")
        self.assertIn("AGY rollout plan", expected[0])
        self.with_index()
        self.assertEqual(outputs(), expected, "index built")
        with mock.patch.object(index_module, "parse_candidate", side_effect=AssertionError("parsed")):
            self.assertEqual(outputs(), expected, "index used")


class AgyTitleTests(AgyCase):
    def titles(self) -> dict:
        return {r["session_id"]: r["title"] for r in self.search_json()["results"]["agy"]}

    def test_title_change_is_noticed_without_parsing_the_transcript(self):
        self.with_index()
        self.assertEqual(self.titles(), {MAIN: None})
        with mock.patch.object(index_module, "parse_candidate", side_effect=AssertionError("parsed")):
            self.set_title(MAIN, "First name")
            self.assertEqual(self.titles(), {MAIN: "First name"})
            # agy has the database open: the new title is still in the -wal file.
            writer = sqlite3.connect(self.db)
            self.addCleanup(writer.close)
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("PRAGMA wal_autocheckpoint=0")
            with writer:
                writer.execute("UPDATE conversation_summaries SET title = 'Second name'")
            self.assertTrue(Path(f"{self.db}-wal").stat().st_size)
            self.assertEqual(self.titles(), {MAIN: "Second name"})
            # Unchanged: the stored titles are used and the database is not opened.
            with mock.patch("find_session.titles.read_agy_titles", side_effect=AssertionError("read again")):
                self.assertEqual(self.titles(), {MAIN: "Second name"})

    def test_reading_creates_nothing_next_to_the_database(self):
        before = sorted(os.listdir(self.db.parent))
        self.set_title(MAIN, "Named")
        self.assertEqual(self.titles(), {MAIN: "Named"})
        self.assertEqual(sorted(os.listdir(self.db.parent)), before)


if __name__ == "__main__":
    unittest.main()
