"""Tool-call commands are searchable; output and file bodies are not."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from helpers import claude_rows, codex_tool_rows, omp_header, omp_msg, run_raw, write_jsonl, write_omp

from find_session import tools
from find_session.parsers import claude, codex


class CommandIndexTests(unittest.TestCase):
    def make_home(self, tmp: str) -> Path:
        home = Path(tmp)
        write_jsonl(home / ".claude/projects/-Users-alex-projects-shop/cmd-1.jsonl", claude_rows())
        write_jsonl(home / ".codex/sessions/2026/09/22/rollout-codex-tools.jsonl", codex_tool_rows())
        return home

    def search_ids(self, home: Path, query: str) -> set[str]:
        _, output = run_raw("--home", str(home), "search", query, "--json", "--children")
        data = json.loads(output)
        return {row["session_id"] for rows in data["results"].values() for row in rows}

    def test_commands_and_arguments_are_searchable(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = self.make_home(tmp)
            self.assertEqual(self.search_ids(home, "DEPLOYCMD"), {"cmd-1"})
            self.assertEqual(self.search_ids(home, "WRITTENFILE"), {"cmd-1"})
            self.assertEqual(self.search_ids(home, "CODEXFUNCCMD"), {"codex-tools"})
            self.assertEqual(self.search_ids(home, "CODEXEXECCMD"), {"codex-tools"})
            self.assertEqual(self.search_ids(home, "PATCHEDFILE"), {"codex-tools"})

    def test_output_file_bodies_patches_and_history_lookups_are_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = self.make_home(tmp)
            for word in (
                "TOOLOUTPUTWORD",
                "FILEBODYWORD",
                "OLDBODY",
                "NEWBODY",
                "CODEXOUTPUTWORD",
                "PATCHBODYWORD",
                "LOOKUPTOPIC",
            ):
                self.assertEqual(self.search_ids(home, word), set(), word)

    def test_every_way_of_calling_this_tool_is_a_history_lookup(self):
        for command in (
            'python3 ~/.claude/skills/find-session/scripts/sessions.py search "pelican migration"',
            "python3 /opt/plugins/cache/find-session/0.3.0/skills/find-session/scripts/sessions.py inspect claude:ab12",
            'HELPER="/a b/find-session/scripts/sessions.py"; python3 "$HELPER" search "pelican migration"',
            'npx find-session search "walrus rollout"',
            "npx -y find-session@latest inspect claude:ab12 --range 3:9",
            'find-session search "otter backlog"',
            "~/.local/bin/find-session index | grep -i otter",
            "find-session --home ~/mirror --no-cache recent --hours 24",
            "uvx find-session search otter",
            'python3 -m find_session search "otter backlog"',
            "bash -lc 'find-session search otter'",
            "cat ~/.claude/projects/-Users-alex-shop/0b7c.jsonl | grep otter",
        ):
            self.assertTrue(tools.HISTORY_LOOKUP.search(command), command)

    def test_work_on_or_near_the_tool_is_not_a_lookup(self):
        for command in (
            "cd ~/code/find-session/ && pytest",
            "cd ~/code/find-session && grep -rn search src",
            "ls ~/.local/share/find-session/skills",
            "git clone https://github.com/ToruGuy/find-session",
            "npx find-session install",
            "find-session --version",
            "python3 -m find_session sources",
            "pnpm run deploy-blog --target staging",
        ):
            self.assertFalse(tools.HISTORY_LOOKUP.search(command), command)

    def test_a_session_that_only_searched_for_a_topic_does_not_rank_for_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            calls = {
                "PELICANTOPIC": 'python3 ~/.claude/skills/find-session/scripts/sessions.py search "PELICANTOPIC"',
                "WALRUSTOPIC": 'npx find-session search "WALRUSTOPIC"',
                "OTTERTOPIC": 'find-session search "OTTERTOPIC"',
                "HERONTOPIC": 'python3 -m find_session search "HERONTOPIC"',
                "KEPTTOPIC": "cd ~/code/find-session/ && pytest -k KEPTTOPIC",
            }
            rows = [{"cwd": "/Users/alex/x", "message": {"role": "user", "content": "Look for earlier work"}}]
            for number, command in enumerate(calls.values()):
                rows.append(
                    {
                        "message": {
                            "role": "assistant",
                            "content": [
                                {"type": "tool_use", "id": f"t{number}", "name": "Bash", "input": {"command": command}}
                            ],
                        }
                    }
                )
            rows.append({"message": {"role": "assistant", "content": [{"type": "text", "text": "Nothing found."}]}})
            write_jsonl(home / ".claude/projects/-Users-alex-x/lookups.jsonl", rows)
            for word in calls:
                self.assertEqual(self.search_ids(home, word), {"lookups"} if word == "KEPTTOPIC" else set(), word)

    def test_omp_tool_calls_are_indexed(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            write_omp(
                home / ".omp/agent/sessions/-Users-alex-x/2026-09-22T10-00-00-000Z_omp-cmd.jsonl",
                [
                    omp_header("omp-cmd"),
                    omp_msg("u1", None, "user", "Check the server"),
                    omp_msg(
                        "a1",
                        "u1",
                        "assistant",
                        [
                            {
                                "type": "toolCall",
                                "id": "t1",
                                "name": "bash",
                                "arguments": {"command": "OMPCMDWORD status"},
                            },
                            {"type": "text", "text": "Checked"},
                        ],
                    ),
                    omp_msg("r1", "a1", "toolResult", [{"type": "text", "text": "OMPOUTPUTWORD"}]),
                ],
            )
            self.assertEqual(self.search_ids(home, "OMPCMDWORD"), {"omp-cmd"})
            self.assertEqual(self.search_ids(home, "OMPOUTPUTWORD"), set())

    def test_commands_keep_message_numbers_and_show_only_with_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = self.make_home(tmp)
            session = claude.parse_claude(home / ".claude/projects/-Users-alex-projects-shop/cmd-1.jsonl")
            self.assertEqual([role for role, _ in session.turns], ["user", "assistant", "assistant"])
            # One Bash, Write and Edit each (the repeated Write is dropped, the lookup skipped).
            self.assertEqual([after for after, _ in session.tools], [2, 2, 2])
            _, plain = run_raw("--home", str(home), "inspect", "claude:cmd-1")
            self.assertNotIn("DEPLOYCMD", plain)
            _, with_tools = run_raw("--home", str(home), "inspect", "claude:cmd-1", "--tools")
            self.assertIn("$ DEPLOYCMD --env prod", with_tools)
            self.assertIn("Write /Users/alex/projects/shop/WRITTENFILE.md", with_tools)
            self.assertLess(with_tools.index("[2 · "), with_tools.index("$ DEPLOYCMD"))
            self.assertLess(with_tools.index("$ DEPLOYCMD"), with_tools.index("[3 · "))
            _, grep = run_raw("--home", str(home), "inspect", "claude:cmd-1", "--tools", "--grep", "WRITTENFILE")
            self.assertIn("[2 · ", grep)
            self.assertNotIn("[1 · ", grep)

    def test_codex_bookkeeping_calls_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = self.make_home(tmp)
            session = codex.parse_codex(home / ".codex/sessions/2026/09/22/rollout-codex-tools.jsonl")
            texts = [text for _, text in session.tools]
            self.assertEqual(len(texts), 3)
            self.assertFalse(any("timeout_ms" in text or "wait_agent" in text for text in texts))
            self.assertIn("apply_patch src/PATCHEDFILE.py", texts)


if __name__ == "__main__":
    unittest.main()
