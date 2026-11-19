"""Injected context (AGENTS.md, environment blocks) is never taken for user text."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from helpers import AGENTS_SCAFFOLD, AGENTS_SCAFFOLD_BARE, ENV_CONTEXT, codex_user, run_full, write_jsonl

from find_session.parsers import claude, codex


class InjectedContextTests(unittest.TestCase):
    run_cli = staticmethod(run_full)

    def write_codex(self, home: Path, name: str, *rows: dict) -> Path:
        path = home / f".codex/sessions/2026/09/22/rollout-{name}.jsonl"
        write_jsonl(
            path,
            [{"type": "session_meta", "payload": {"id": name, "cwd": "/Users/alex/x"}}, *rows],
        )
        return path

    def test_codex_injected_scaffold_is_not_a_user_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = self.write_codex(
                home,
                "scaffold",
                codex_user(AGENTS_SCAFFOLD, ENV_CONTEXT),
                codex_user("<recommended_plugins>\nSYNTH_PLUGIN\n</recommended_plugins>", AGENTS_SCAFFOLD, ENV_CONTEXT),
                codex_user(AGENTS_SCAFFOLD_BARE, ENV_CONTEXT),
                codex_user("A real question about the report"),
                codex_user(AGENTS_SCAFFOLD_BARE, "Second request"),
            )
            parsed = codex.parse_codex(path)
            self.assertEqual(parsed.user_messages, ["A real question about the report", "Second request"])
            first = codex.parse_codex(path, stop_after_first_user=True)
            self.assertEqual(first.user_messages, ["A real question about the report"])
            code, output = self.run_cli("--home", tmp, "search", "SYNTH_AGENTS_RULE")
            self.assertIn("=== Codex — top 0 of 0 ===", output)

    def test_codex_user_prose_sharing_a_message_with_injection_is_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = self.write_codex(
                home,
                "shared",
                codex_user(AGENTS_SCAFFOLD, ENV_CONTEXT, "Fix the sales chart"),
                codex_user(ENV_CONTEXT + "\n\nThen add a legend"),
                codex_user(
                    "<context_refresh>\nSYNTH_REFRESH\n</context_refresh>\n"
                    "<user_message>\nCheck yesterday's result\n</user_message>"
                ),
            )
            parsed = codex.parse_codex(path)
            self.assertEqual(
                parsed.user_messages,
                ["Fix the sales chart", "Then add a legend", "Check yesterday's result"],
            )
            joined = " ".join(parsed.user_messages)
            for leaked in ("SYNTH_", "AGENTS.md instructions", "environment_context"):
                self.assertNotIn(leaked, joined)

    def test_codex_discussion_of_agents_md_and_xml_is_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = self.write_codex(
                home,
                "discussion",
                codex_user("Should AGENTS.md instructions be shorter?"),
                codex_user("See # AGENTS.md instructions for repo, no tags"),
                codex_user("Compare <INSTRUCTIONS> with </INSTRUCTIONS> in the template"),
            )
            parsed = codex.parse_codex(path)
            self.assertEqual(len(parsed.user_messages), 3)

    def test_claude_project_prefers_session_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            directory = home / ".claude/projects/-Users-alex--local-share-demo-tool"
            write_jsonl(
                directory / "with-cwd.jsonl",
                [
                    {"type": "attachment", "cwd": "/Users/alex/.local/share/demo-tool"},
                    {
                        "type": "user",
                        "cwd": "/Users/alex/.local/share/demo-tool",
                        "message": {"role": "user", "content": "Topic CWDTOPIC"},
                    },
                ],
            )
            write_jsonl(
                directory / "without-cwd.jsonl",
                [{"message": {"role": "user", "content": "Old record"}}],
            )
            parsed = claude.parse_claude(directory / "with-cwd.jsonl")
            self.assertEqual(parsed.project, "~/.local/share/demo-tool")
            first = claude.parse_claude(directory / "with-cwd.jsonl", stop_after_first_user=True)
            self.assertEqual(first.project, "~/.local/share/demo-tool")
            legacy = claude.parse_claude(directory / "without-cwd.jsonl")
            self.assertEqual(legacy.project, claude.claude_project(directory.name))
            code, output = self.run_cli("--home", tmp, "search", "demo-tool")
            self.assertIn("Session: with-cwd", output)
            self.assertIn("~/.local/share/demo-tool", output)


if __name__ == "__main__":
    unittest.main()
