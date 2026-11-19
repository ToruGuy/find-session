"""What ranks where: subagents, summaries, empty sessions, chunks."""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from helpers import claude_turns, codex_answer, codex_meta, codex_user, run_full, run_raw, write_jsonl

from find_session import (
    ranking,
)
from find_session.parsers import claude, codex


class RankingHygieneTests(unittest.TestCase):
    run_cli = staticmethod(run_full)

    def write_codex(self, home: Path, name: str, meta: dict, *rows: dict) -> None:
        write_jsonl(home / f".codex/sessions/2026/09/22/rollout-{name}.jsonl", [meta, *rows])

    def test_codex_subagents_and_review_threads_leave_main_ranking(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.write_codex(home, "parent-1", codex_meta("parent-1"), codex_user("Plan KINDTOPIC"))
            self.write_codex(
                home,
                "child-1",
                codex_meta(
                    "child-1",
                    source={"subagent": {"thread_spawn": {"parent_thread_id": "parent-1"}}},
                    thread_source="subagent",
                ),
                codex_user("Implement KINDTOPIC part"),
                codex_answer("Part done"),
            )
            self.write_codex(
                home,
                "review-1",
                codex_meta("review-1", source={"subagent": {"other": "guardian"}}, thread_source="guardian_review"),
                codex_user("The following is the Codex agent history KINDTOPIC"),
            )
            code, output = self.run_cli("--home", tmp, "search", "KINDTOPIC")
            self.assertIn("=== Codex — top 1 of 1 ===", output)
            self.assertIn("Session: parent-1", output)
            self.assertNotIn("child-1", output)
            self.assertNotIn("review-1", output)
            self.assertIn(
                "Also 2 subagent or review session(s) containing all the words, not shown: add --children.", output
            )

            code, output = self.run_cli("--home", tmp, "search", "KINDTOPIC", "--children")
            self.assertIn("=== subagents — top 2 of 2 ===", output)
            self.assertIn("Codex subagent of session parent-1 (no resume)", output)
            self.assertIn("Codex automatic review thread (no resume)", output)
            self.assertIn("Start: [delegated task] Implement KINDTOPIC part", output)

            code, output = self.run_cli("--home", tmp, "inspect", "child-1")
            self.assertEqual(code, 0)
            self.assertIn("DELEGATED TASK: Implement KINDTOPIC part", output)
            code, output = self.run_cli("--home", tmp, "inspect", "parent-1", "--json")
            self.assertEqual(json.loads(output)["children"], ["codex:child-1"])

    def test_copied_parent_session_meta_does_not_replace_own_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.write_codex(
                home,
                "fork-1",
                codex_meta("fork-1", source={"subagent": {"thread_spawn": {"parent_thread_id": "base-1"}}}),
                codex_meta("base-1", cwd="/Users/alex/elsewhere"),
                codex_user("Copied FORKTOPIC history"),
            )
            parsed = codex.parse_codex(home / ".codex/sessions/2026/09/22/rollout-fork-1.jsonl")
            self.assertEqual((parsed.session_id, parsed.kind, parsed.parent_id), ("fork-1", "child", "base-1"))
            self.assertEqual(parsed.project, "~/projects/demo")

    def test_index_and_recent_list_main_sessions_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.write_codex(home, "main-1", codex_meta("main-1"), codex_user("Main LISTTOPIC"))
            self.write_codex(
                home,
                "review-2",
                codex_meta("review-2", thread_source="guardian_review"),
                codex_user("Review LISTTOPIC"),
            )
            for command in (["index"], ["recent", "--hours", "1"]):
                code, output = self.run_cli("--home", tmp, *command)
                self.assertIn("main-1", output)
                self.assertNotIn("review-2", output)

    def test_empty_sessions_do_not_match_on_project_path_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.write_codex(home, "empty", codex_meta("empty-1", cwd="/Users/alex/pathtopic"))
            self.write_codex(home, "full", codex_meta("full-1", cwd="/Users/alex/pathtopic"), codex_user("Hello"))
            code, output = self.run_cli("--home", tmp, "search", "pathtopic")
            self.assertIn("Session: full-1", output)
            self.assertNotIn("empty-1", output)

    def test_prompt_wrapped_in_context_inside_user_message_is_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            wrapped = (
                "<assistant_context>\nSYNTH_PERSONA\n</assistant_context>\n\n"
                "<user_message>\n<context>\nSYNTH_STATE\n</context>\n\nWhat is on my list today?\n</user_message>"
            )
            self.write_codex(home, "voice", codex_meta("voice-1"), codex_user(wrapped))
            parsed = codex.parse_codex(home / ".codex/sessions/2026/09/22/rollout-voice.jsonl")
            self.assertEqual(parsed.user_messages, ["What is on my list today?"])

    def test_compaction_summary_is_not_a_user_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            write_jsonl(
                home / ".claude/projects/-Users-alex-x/compacted.jsonl",
                [
                    {"timestamp": "2026-09-22T10:00:00Z", "message": {"role": "user", "content": "Start SUMTOPIC"}},
                    {
                        "timestamp": "2026-09-22T11:00:00Z",
                        "isCompactSummary": True,
                        "message": {"role": "user", "content": "Summary: the user wanted SUMTOPIC and more."},
                    },
                    {
                        "timestamp": "2026-09-22T11:01:00Z",
                        "message": {
                            "role": "user",
                            "content": "This session is being continued from a previous conversation that ran out of context. SUMTOPIC",
                        },
                    },
                    {"timestamp": "2026-09-22T11:02:00Z", "message": {"role": "user", "content": "Real follow-up"}},
                ],
            )
            parsed = claude.parse_claude(home / ".claude/projects/-Users-alex-x/compacted.jsonl")
            self.assertEqual(parsed.user_messages, ["Start SUMTOPIC", "Real follow-up"])
            self.assertEqual(len(parsed.summaries), 2)
            code, output = self.run_cli("--home", tmp, "search", "SUMTOPIC")
            self.assertIn("Last request: Real follow-up", output)
            # Still searchable: summaries retell the conversation.
            code, output = self.run_cli("--home", tmp, "search", "wanted")
            self.assertIn("Session: compacted", output)
            self.assertNotIn("Last request: This session", output)


class ChunkRankingTests(unittest.TestCase):
    def write(self, home: Path, name: str, texts: list[str]) -> Path:
        path = home / f".claude/projects/-Users-alex-work/{name}.jsonl"
        write_jsonl(path, claude_turns(*texts))
        return path

    def test_words_together_beat_words_far_apart(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            scattered = [f"filler {i}" for i in range(60)]
            scattered[3] = "we talked about ALPHAWORD"
            scattered[55] = "and much later about BETAWORD"
            self.write(home, "scattered", scattered)
            together = [f"other {i}" for i in range(12)]
            together[5] = "ALPHAWORD and BETAWORD in one answer"
            self.write(home, "together", together)
            _, output = run_raw("--home", str(home), "search", "ALPHAWORD BETAWORD", "--json")
            ranked = [row["session_id"] for row in json.loads(output)["results"]["Claude"]]
            self.assertEqual(ranked, ["together", "scattered"])

    def test_hit_range_points_inside_a_long_session_and_inspect_reads_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            texts = [f"routine message {i}" for i in range(200)]
            texts[0] = "Long master conversation"
            texts[140] = "Where is the GAMMAWORD export?"
            texts[141] = "The GAMMAWORD export lives in DELTAWORD module."
            # A compaction-style retelling earlier must not decide the range.
            texts[60] = "This session is being continued from a previous conversation. GAMMAWORD DELTAWORD"
            self.write(home, "long-1", texts)
            _, output = run_raw("--home", str(home), "search", "GAMMAWORD DELTAWORD")
            match = re.search(r"^ hit (\d+):(\d+) ", output, re.M)
            self.assertIsNotNone(match, output)
            first, last = int(match.group(1)), int(match.group(2))
            self.assertLessEqual(first, 142)
            self.assertGreaterEqual(last, 142)
            self.assertLessEqual(last - first, ranking.CHUNK_MESSAGES + 1)
            _, transcript = run_raw("--home", str(home), "inspect", "claude:long-1", "--range", f"{first}:{last}")
            self.assertIn("[142 · ", transcript)
            self.assertIn("lives in DELTAWORD module", transcript)
            self.assertNotIn("routine message 5\n", transcript)
            _, data = run_raw("--home", str(home), "search", "GAMMAWORD DELTAWORD", "--json")
            record = json.loads(data)["results"]["Claude"][0]
            self.assertEqual(record["chunk"], {"first": first, "last": last})
            self.assertEqual(record["match"]["n"], 142)
            self.assertEqual(record["messages"], 200)


if __name__ == "__main__":
    unittest.main()
