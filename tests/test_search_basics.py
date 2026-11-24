"""Parsing of Claude Code and Codex sessions, index, recent and per-source search."""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from helpers import write_jsonl

from find_session import (
    recent,
    search,
)
from find_session.parsers import claude, codex


class ParsingAndSearchTests(unittest.TestCase):
    def test_recent_reads_no_other_files_unless_included(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "notes/projects").mkdir(parents=True)
            (home / "notes/board.md").write_text("UNWANTED_NOTES_BOARD")
            (home / "notes/projects/state.md").write_text("WANTED_PROJECT_STATE")
            captured = io.StringIO()
            with contextlib.redirect_stdout(captured):
                result = recent.build_recent(home, hours=48, limit=30)
            self.assertEqual(result, 0)
            self.assertNotIn("UNWANTED_NOTES_", captured.getvalue())
            self.assertNotIn("WANTED_PROJECT_STATE", captured.getvalue())

            captured = io.StringIO()
            with contextlib.redirect_stdout(captured):
                recent.build_recent(home, 48, 30, include=["notes/projects/*.md", "/etc/*"])
            output = captured.getvalue()
            self.assertIn("### notes/projects/state.md", output)
            self.assertIn("WANTED_PROJECT_STATE", output)
            self.assertNotIn("UNWANTED_NOTES_", output)

    def test_codex_filters_injected_context_and_keeps_final_answers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout-test.jsonl"
            write_jsonl(
                path,
                [
                    {
                        "type": "session_meta",
                        "payload": {"id": "codex-1", "cwd": "/Users/alex/projects/demo"},
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "<environment_context>x</environment_context>"}],
                        },
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "Merge the Codex sessions"}],
                        },
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "phase": "commentary",
                            "content": [{"type": "output_text", "text": "Analysing"}],
                        },
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "phase": "final_answer",
                            "content": [{"type": "output_text", "text": "Bridge ready"}],
                        },
                    },
                ],
            )
            parsed = codex.parse_codex(path)
            self.assertEqual(parsed.session_id, "codex-1")
            self.assertEqual(parsed.project, "~/projects/demo")
            self.assertEqual(parsed.user_messages, ["Merge the Codex sessions"])
            self.assertEqual(parsed.assistant_messages, ["Bridge ready"])

    def test_claude_filters_skill_injection(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "-Users-alex-projects-demo" / "claude-1.jsonl"
            write_jsonl(
                path,
                [
                    {
                        "message": {
                            "role": "user",
                            "content": "Base directory for this skill: /tmp/skill",
                        }
                    },
                    {
                        "message": {
                            "role": "user",
                            "content": "<command-message>find-session</command-message>",
                        }
                    },
                    {"message": {"role": "user", "content": "Find the previous session"}},
                    {
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "Found it"}],
                        }
                    },
                ],
            )
            parsed = claude.parse_claude(path)
            self.assertEqual(parsed.project, "~/projects/demo")
            self.assertEqual(parsed.user_messages, ["Find the previous session"])
            self.assertEqual(parsed.assistant_messages, ["Found it"])

    def test_unified_index_and_brief_include_both_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            write_jsonl(
                home / ".claude/projects/-Users-alex-projects-demo/claude-1.jsonl",
                [{"message": {"role": "user", "content": "Claude topic"}}],
            )
            write_jsonl(
                home / ".codex/sessions/2026/08/13/rollout-codex-1.jsonl",
                [
                    {
                        "type": "session_meta",
                        "payload": {"id": "codex-1", "cwd": "/Users/alex/projects/demo"},
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "Codex topic"}],
                        },
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "phase": "final_answer",
                            "content": [{"type": "output_text", "text": "Codex result"}],
                        },
                    },
                ],
            )
            out = home / "index.md"
            with contextlib.redirect_stdout(io.StringIO()):
                recent.build_index(home, out)
            index = out.read_text()
            self.assertIn("Claude 1, Codex 1", index)
            self.assertIn("\nclaude:claude-1 ", index)
            self.assertIn("\ncodex:codex-1 ", index)

            captured = io.StringIO()
            with contextlib.redirect_stdout(captured):
                recent.build_recent(home, 48, 10)
            brief = captured.getvalue()
            self.assertIn("Claude Code + Codex", brief)
            self.assertIn("Codex topic", brief)
            self.assertIn("Codex result", brief)

    def test_search_returns_each_source_independently_and_uses_project_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            for number in range(3):
                write_jsonl(
                    home / f".claude/projects/-Users-alex-projects-demo/claude-{number}.jsonl",
                    [{"message": {"role": "user", "content": "ACME report dashboard"}}],
                )
            write_jsonl(
                home / ".codex/sessions/2026/08/13/rollout-codex-acme.jsonl",
                [
                    {
                        "type": "session_meta",
                        "payload": {
                            "id": "codex-acme",
                            "cwd": "/Users/alex/clients/acme-shop",
                        },
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "Check the panel"}],
                        },
                    },
                ],
            )

            captured = io.StringIO()
            with contextlib.redirect_stdout(captured):
                search.search_sessions(home, "ACME", limit=1, full=True)
            output = captured.getvalue()
            self.assertIn("=== Claude — top 1 of 3 ===", output)
            self.assertIn("=== Codex — top 1 of 1 ===", output)
            self.assertIn("Session: codex-acme", output)
            self.assertIn("always check Claude, Codex, omp and agy", output)

    def test_search_source_filter_is_diagnostic_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            write_jsonl(
                home / ".claude/projects/-Users-alex-projects-demo/claude-only.jsonl",
                [{"message": {"role": "user", "content": "Garden"}}],
            )
            write_jsonl(
                home / ".codex/sessions/2026/08/13/rollout-codex-only.jsonl",
                [
                    {
                        "type": "session_meta",
                        "payload": {"id": "codex-only", "cwd": "/home/sam/garden"},
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "Offer"}],
                        },
                    },
                ],
            )
            captured = io.StringIO()
            with contextlib.redirect_stdout(captured):
                search.search_sessions(home, "Garden", limit=5, source_filter="codex", full=True)
            output = captured.getvalue()
            self.assertNotIn("=== Claude", output)
            self.assertIn("=== Codex", output)
            self.assertIn("Session: codex-only", output)


if __name__ == "__main__":
    unittest.main()
