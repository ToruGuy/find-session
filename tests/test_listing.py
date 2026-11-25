"""`index`, `recent` and `sources`: bounded output and true counts."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import claude_turns, codex_answer, codex_meta, codex_user, fake, run_raw, write_jsonl

from find_session import recent


def many_sessions(home: Path, count: int) -> None:
    for number in range(count):
        day, hour = 1 + number // 24, number % 24
        write_jsonl(
            home / f".claude/projects/-Users-alex-work/{number:04d}aaaa-0000-4000-8000-000000000000.jsonl",
            claude_turns(
                f"Topic number {number} " + "detail " * 60, "Ok.", start=f"2026-08-{day:02d}T{hour:02d}:00:00Z"
            ),
        )


class IndexCommandTests(unittest.TestCase):
    def test_index_prints_the_newest_sessions_and_says_how_to_get_more(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            many_sessions(home, 130)
            code, output = run_raw("--home", tmp, "index")
            lines = output.splitlines()
            self.assertEqual(code, 0)
            self.assertEqual(
                lines[0],
                "index: the newest 50 of 130 sessions (in all: Claude 130, Codex 0, omp 0, agy 0); "
                "--limit N lists more (0 = all), --since WHEN a period",
            )
            self.assertEqual(len(lines), 1 + recent.INDEX_LIMIT)
            self.assertTrue(lines[1].startswith("claude:0129aaaa 2026-08-06 "), lines[1])
            self.assertTrue(lines[-1].startswith("claude:0080aaaa "), lines[-1])
            # One line per session, cut like a search row: the default stays a few kilobytes.
            self.assertLess(max(map(len, lines[1:])), 170)
            self.assertLess(len(output), 9000)
            # A row starts with a selector that inspect takes.
            code, _ = run_raw("--home", tmp, "inspect", lines[1].split()[0])
            self.assertEqual(code, 0)

    def test_limit_since_and_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            many_sessions(home, 130)
            _, output = run_raw("--home", str(home), "index", "--limit", "3")
            self.assertEqual(len(output.splitlines()), 4)
            self.assertIn("the newest 3 of 130 sessions", output)
            _, output = run_raw("--home", str(home), "index", "--limit", "0")
            self.assertEqual(len(output.splitlines()), 131)
            self.assertEqual(
                output.splitlines()[0], "index: 130 sessions, newest first (in all: Claude 130, Codex 0, omp 0, agy 0)"
            )
            _, output = run_raw("--home", str(home), "index", "--since", "2026-08-06 05:00", "--limit", "0")
            self.assertIn("sessions since 2026-08-06 05:00, newest first", output.splitlines()[0])
            self.assertLess(len(output.splitlines()), 10)
            # A file gets the whole list: it is there to be searched with other tools.
            out = Path(tmp) / "all.txt"
            _, output = run_raw("--home", str(home), "index", "--out", str(out))
            self.assertEqual(len(out.read_text().splitlines()), 131)
            self.assertLess(len(output), 200)
            _, output = run_raw("--home", str(home), "index", "--out", str(out), "--limit", "5")
            self.assertEqual(len(out.read_text().splitlines()), 6)

    def test_sessions_without_messages_are_not_listed(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            write_jsonl(home / ".claude/projects/-Users-alex-work/empty.jsonl", [{"type": "note"}])
            write_jsonl(home / ".claude/projects/-Users-alex-work/full.jsonl", claude_turns("Hello", "Hi."))
            _, output = run_raw("--home", tmp, "index")
            self.assertEqual(
                output.splitlines()[0], "index: 1 session, newest first (in all: Claude 1, Codex 0, omp 0, agy 0)"
            )
            self.assertNotIn("claude:empty", output)


class RecentCommandTests(unittest.TestCase):
    def test_recent_is_bounded_and_says_when_it_left_sessions_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            many_sessions(home, 45)
            long_answer = "Answer " + "sentence " * 400
            write_jsonl(
                home / ".claude/projects/-Users-alex-work/long.jsonl",
                claude_turns(
                    "Start " + "word " * 300,
                    "ok",
                    "Second request " + "word " * 300,
                    long_answer,
                    start="2026-08-02T23:00:00Z",
                ),
            )
            code, output = run_raw("--home", tmp, "recent", "--hours", "1000000")
            self.assertEqual(code, 0)
            self.assertIn(
                "(Claude Code + Codex + omp + agy): the newest 20 of 46 sessions (--limit N for more)", output
            )
            self.assertEqual(output.count("\n### "), 20)
            # Each text is cut at a word, with an ellipsis: nothing is printed at full length.
            for line in output.splitlines():
                self.assertLessEqual(len(line), 420, line[:80])
            self.assertIn("- last request: Second request word", output)
            self.assertLess(len(output), 20 * 1300)
            _, output = run_raw("--home", tmp, "recent", "--hours", "1000000", "--limit", "100")
            self.assertEqual(output.count("\n### "), 46)
            self.assertNotIn("the newest", output)


class SourcesCommandTests(unittest.TestCase):
    def make_home(self, tmp: str) -> Path:
        home = Path(tmp)
        write_jsonl(home / ".claude/projects/-Users-alex-work/one.jsonl", claude_turns("Hello", "Hi."))
        write_jsonl(
            home / ".claude/projects/-Users-alex-work/one/subagents/agent-a1b2c3d4e5f607182.jsonl",
            claude_turns("Task", "Report."),
        )
        for name, extra in (
            ("main-1", {}),
            ("main-2", {}),
            ("child-1", {"source": {"subagent": {"thread_spawn": {"parent_thread_id": "main-1"}}}}),
            ("child-2", {"thread_source": "subagent"}),
            ("child-3", {"source": {"subagent": {"thread_spawn": {"parent_thread_id": "main-2"}}}}),
            ("review-1", {"thread_source": "guardian_review"}),
        ):
            write_jsonl(
                home / f".codex/sessions/2026/09/22/rollout-{name}.jsonl",
                [codex_meta(name, **extra), codex_user("Something"), codex_answer("Done.")],
            )
        return home

    def test_sources_counts_subagents_and_review_threads_apart(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.make_home(tmp)
            code, output = run_raw("--home", tmp, "sources")
            self.assertEqual(code, 0)
            lines = output.splitlines()
            self.assertIn("Claude: 1 session, 1 subagent", lines)
            self.assertIn("Codex: 2 sessions, 3 subagents, 1 review thread", lines)
            self.assertIn("omp: 0 sessions, 0 subagents", lines)
            _, output = run_raw("--home", tmp, "sources", "--json")
            data = json.loads(output)
            self.assertEqual((data["Codex"]["main"], data["Codex"]["children"], data["Codex"]["reviews"]), (2, 3, 1))
            self.assertEqual((data["Claude"]["main"], data["Claude"]["children"]), (1, 1))

    def test_sources_agrees_with_the_index_and_does_not_build_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            self.make_home(str(home))
            cache = Path(tmp) / "cache"
            with mock.patch.dict(os.environ, {"FIND_SESSION_CACHE": str(cache)}):
                _, before = run_raw("--home", str(home), "sources")
                # Shown by the installers before anything is indexed: it must not start the long first build.
                self.assertFalse(cache.exists() and any(cache.iterdir()))
                _, listing = run_raw("--home", str(home), "index")
                _, after = run_raw("--home", str(home), "sources")
            self.assertEqual(before, after)
            self.assertIn("(in all: Claude 1, Codex 2, omp 0, agy 0)", listing)

    def test_config_line_says_when_there_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.make_home(tmp)
            missing = Path(tmp) / "nowhere" / "config.json"
            present = Path(tmp) / "config.json"
            present.write_text("{}")
            with mock.patch.dict(os.environ, {"FIND_SESSION_CONFIG": str(missing)}):
                _, output = run_raw("--home", tmp, "sources")
            self.assertEqual(output.splitlines()[-1], f"Config: none ({missing} is read when it exists)")
            with mock.patch.dict(os.environ, {"FIND_SESSION_CONFIG": str(present)}):
                _, output = run_raw("--home", tmp, "sources")
            self.assertEqual(output.splitlines()[-1], f"Config: {present}")


class ToldSecretEndToEndTests(unittest.TestCase):
    def test_a_secret_told_in_a_sentence_reaches_no_output_and_no_index(self):
        password = fake("Stag", "ing", "9x", "$")
        token = fake("Xk9", "mP2", "qLw")
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            rows = claude_turns(
                f"CANARYTOPIC the password for staging is {password}",
                f"Noted. CANARYTOPIC the secret is {token}, and hasło do serwera to {password}",
            )
            rows.append(
                {
                    "timestamp": "2026-09-22T10:02:00Z",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "toolu_1",
                                "name": "Bash",
                                "input": {
                                    "command": f"docker login -u me -p {password} registry.example.com # CANARYTOPIC"
                                },
                            }
                        ],
                    },
                }
            )
            write_jsonl(home / ".claude/projects/-Users-alex-work/canary.jsonl", rows)
            cache = Path(tmp) / "cache"
            outputs = []
            with mock.patch.dict(os.environ, {"FIND_SESSION_CACHE": str(cache)}):
                for argv in (
                    ("search", "CANARYTOPIC password staging"),
                    ("search", "CANARYTOPIC", "--full"),
                    ("search", "CANARYTOPIC docker", "--json"),
                    ("inspect", "claude:canary", "--tools"),
                    ("inspect", "claude:canary", "--json"),
                    ("recent", "--hours", "1000000"),
                    ("index",),
                ):
                    outputs.append(run_raw("--home", str(home), *argv)[1])
            outputs.append(run_raw("--home", str(home), "--no-cache", "search", "CANARYTOPIC")[1])
            joined = "\n".join(outputs)
            self.assertIn("the password for staging is [REDACTED]", joined)
            self.assertIn("docker login -u me -p [REDACTED] registry.example.com", joined)
            stored = b"".join(path.read_bytes() for path in sorted(cache.iterdir()))
            for value in (password, token):
                self.assertNotIn(value, joined)
                self.assertNotIn(value.encode(), stored)
                self.assertNotIn(value.lower().encode(), stored)


if __name__ == "__main__":
    unittest.main()
