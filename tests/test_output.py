"""The compact default output."""

from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import claude_turns, codex_answer, codex_meta, codex_user, make_omp_home, run_raw, write_jsonl

from find_session import ranking


class CompactOutputTests(unittest.TestCase):
    def make_home(self, tmp: str) -> Path:
        home = Path(tmp)
        for index in range(3):
            path = home / f".claude/projects/-Users-alex-shop/0b7c2f1e-0000-0000-0000-00000000000{index}.jsonl"
            write_jsonl(
                path,
                [
                    {
                        "timestamp": "2026-09-22T10:00:00Z",
                        "cwd": "/Users/alex/projects/shop",
                        "message": {"role": "user", "content": f"Invoice export, part {index}"},
                    },
                    {
                        "timestamp": "2026-09-22T10:00:05Z",
                        "message": {"role": "assistant", "content": [{"type": "text", "text": "Done."}]},
                    },
                ],
            )
        write_jsonl(
            home / ".codex/sessions/2026/09/22/rollout-child.jsonl",
            [
                codex_meta("child-1", source={"subagent": {"thread_spawn": {"parent_thread_id": "parent-1"}}}),
                codex_user("Find the invoice export bug"),
                codex_answer("Invoice export fixed in the subagent"),
            ],
        )
        return home

    def test_default_output_is_compact(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.make_home(tmp)
            _, compact = run_raw("--home", tmp, "search", "invoice export")
            _, full = run_raw("--home", tmp, "search", "invoice export", "--full")
            self.assertLess(len(compact), len(full) * 0.7)
            lines = compact.splitlines()
            self.assertEqual(
                lines[0], 'search "invoice export": Claude 3, Codex 0, omp 0; 1 more in subagents (--children)'
            )
            # The helper path is printed once, not per result; no per-result resume or inspect lines.
            self.assertEqual(compact.count("sessions.py"), 1)
            self.assertNotIn("Resume:", compact)
            self.assertNotIn("Inspect:", compact)
            # Sections without results get no header.
            self.assertNotIn("\nCodex", compact)
            self.assertNotIn("\nomp", compact)
            # Ids share their first 8 characters, so each is shortened only as far as stays unique.
            for index in range(3):
                self.assertIn(f"claude:0b7c2f1e-0000-0000-0000-00000000000{index} ", compact)
            # Both words are in the first message, so no hit line is needed.
            self.assertNotIn(" hit ", compact)
            # The full format keeps resume and inspect per result.
            self.assertEqual(full.count("Resume: cd /Users/alex/projects/shop && claude --resume"), 3)

    def test_short_ids_resolve_with_inspect(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            write_jsonl(
                home / ".claude/projects/-Users-alex-x/9f1e2d3c-1111-2222-3333-444455556666.jsonl",
                claude_turns("Unique SHORTTOPIC", "ok"),
            )
            _, output = run_raw("--home", tmp, "search", "SHORTTOPIC")
            self.assertIn("claude:9f1e2d3c 2026-", output)
            code, transcript = run_raw("--home", tmp, "inspect", "claude:9f1e2d3c")
            self.assertEqual(code, 0)
            self.assertIn("Unique SHORTTOPIC", transcript)
            self.assertIn(
                "Resume: cd /Users/alex/work && claude --resume 9f1e2d3c-1111-2222-3333-444455556666", transcript
            )

    def test_subagents_and_config_hints_are_compact_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.make_home(tmp)
            config = Path(tmp) / "config.json"
            config.write_text(
                json.dumps(
                    {"hints": [{"label": "Inspect on server", "command": "ssh box find-session inspect {selector}"}]}
                )
            )
            with mock.patch.dict(os.environ, {"FIND_SESSION_CONFIG": str(config)}):
                _, output = run_raw("--home", tmp, "search", "invoice export", "--children")
            self.assertEqual(output.count("Inspect on server: ssh box find-session inspect {selector}"), 1)
            self.assertIn("\nsubagents\ncodex:child-1 ", output)
            self.assertIn("· subagent of parent-1", output)
            self.assertIn(" task: Find the invoice export bug", output)

    def test_omp_subagent_selector_from_compact_output_works_with_inspect(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_omp_home(tmp)
            _, output = run_raw("--home", tmp, "search", "CHILDTOPIC", "--children")
            selectors = re.findall(r"^(omp:\S+) ", output, re.M)
            self.assertTrue(any("/" in selector for selector in selectors), output)
            for selector in selectors:
                code, transcript = run_raw("--home", tmp, "inspect", selector)
                self.assertEqual(code, 0, selector)

    def test_inspect_prints_configured_resume_label_and_hints(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.make_home(tmp)
            config = Path(tmp) / "config.json"
            config.write_text(
                json.dumps(
                    {
                        "resume_label": "Resume on laptop",
                        "hints": [{"label": "Inspect on server", "command": "ssh box find-session inspect {selector}"}],
                    }
                )
            )
            with mock.patch.dict(os.environ, {"FIND_SESSION_CONFIG": str(config)}):
                _, output = run_raw("--home", tmp, "inspect", "claude:0b7c2f1e-0000-0000-0000-000000000001")
            self.assertIn("Resume on laptop: cd /Users/alex/projects/shop && claude --resume", output)
            self.assertIn(
                "Inspect on server: ssh box find-session inspect claude:0b7c2f1e-0000-0000-0000-000000000001", output
            )

    def test_no_matches_is_one_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.make_home(tmp)
            _, output = run_raw("--home", tmp, "search", "NOTHINGLIKETHIS")
            self.assertEqual(output, 'search "NOTHINGLIKETHIS": no matches (Claude 0, Codex 0, omp 0)\n')

    def test_no_history_at_all_points_at_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, output = run_raw("--home", tmp, "search", "anything")
            first, second = output.splitlines()
            self.assertEqual(first, 'search "anything": no matches (Claude 0, Codex 0, omp 0)')
            self.assertRegex(second, r"^no session files found: `python3 \S+sessions\.py sources` shows where")


class MatchCountTests(unittest.TestCase):
    """Any query word lists a session; the header says how many have all of them."""

    def make_home(self, tmp: str) -> Path:
        home = Path(tmp)
        texts = {
            "both-1": "The invoice export is broken",
            "both-2": "Rewrite the export of every invoice",
            "one-1": "Blog post about the staging deploy for the team",
            "one-2": "An invoice for the hosting came in",
        }
        for name, text in texts.items():
            write_jsonl(home / f".claude/projects/-Users-alex-shop/{name}.jsonl", claude_turns(text, "Done."))
        write_jsonl(
            home / ".codex/sessions/2026/09/22/rollout-codex-one.jsonl",
            [codex_meta("codex-one"), codex_user("Export the report"), codex_answer("Exported.")],
        )
        return home

    def test_header_counts_sessions_with_all_words_apart_from_partial_ones(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.make_home(tmp)
            _, output = run_raw("--home", tmp, "search", "invoice export")
            self.assertEqual(
                output.splitlines()[0], 'search "invoice export": Claude 2 (+1 partial), Codex 0 (+1 partial), omp 0'
            )
            # Every one of them is still listed, the complete ones first.
            self.assertIn("\nClaude\nclaude:both-", output)
            self.assertEqual(output.count("\nclaude:"), 3)
            _, full = run_raw("--home", tmp, "search", "invoice export", "--full")
            self.assertIn("(Claude 2 (+1 partial), Codex 0 (+1 partial), omp 0)", full.splitlines()[0])
            _, limited = run_raw("--home", tmp, "search", "invoice export", "--limit", "1")
            self.assertIn("\nClaude (top 1 of 3)\n", limited)

    def test_one_word_or_all_complete_adds_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.make_home(tmp)
            _, output = run_raw("--home", tmp, "search", "invoice")
            self.assertEqual(output.splitlines()[0], 'search "invoice": Claude 3, Codex 0, omp 0')
            _, output = run_raw("--home", tmp, "search", "staging deploy")
            self.assertEqual(output.splitlines()[0], 'search "staging deploy": Claude 1, Codex 0, omp 0')

    def test_a_word_no_session_has_is_named(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.make_home(tmp)
            _, output = run_raw("--home", tmp, "search", "invoice nonexistentword")
            self.assertEqual(
                output.splitlines()[0],
                'search "invoice nonexistentword": Claude 0 (+3 partial), Codex 0, omp 0; no session has: nonexistentword',
            )
            _, full = run_raw("--home", tmp, "search", "invoice nonexistentword", "--full")
            self.assertIn("\nNo session has: nonexistentword\n", full)
            # When nothing matches, every word is missing and the line says only that.
            _, output = run_raw("--home", tmp, "search", "nonexistentword otherabsentword")
            self.assertEqual(
                output, 'search "nonexistentword otherabsentword": no matches (Claude 0, Codex 0, omp 0)\n'
            )

    def test_json_keeps_counts_and_adds_the_complete_ones(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.make_home(tmp)
            for leading in ((), ("--no-cache",)):
                _, output = run_raw("--home", tmp, *leading, "search", "invoice export nonexistentword", "--json")
                data = json.loads(output)
                self.assertEqual(data["counts"], {"Claude": 3, "Codex": 1, "omp": 0})
                self.assertEqual(data["counts_all_words"], {"Claude": 0, "Codex": 0, "omp": 0})
                self.assertEqual(data["words_not_found"], ["nonexistentword"])
                _, output = run_raw("--home", tmp, *leading, "search", "invoice export", "--json")
                data = json.loads(output)
                self.assertEqual(data["counts_all_words"], {"Claude": 2, "Codex": 0, "omp": 0})
                self.assertEqual(data["words_not_found"], [])
                modified = data["results"]["Claude"][0]["modified"]
                self.assertRegex(modified, r"^\d{4}-\d\d-\d\dT\d\d:\d\d[+-]\d\d:\d\d$")


class WholeWordTests(unittest.TestCase):
    """Nothing that is printed stops in the middle of a word."""

    LONG = (
        "Great, add a test for the invoice export and then look at the rounding of the totals, because the "
        "customer reported that three of the monthly statements were off by one cent after the migration "
        "to the new ledger, and nobody could say when exactly that started to happen in production."
    )

    def test_snippet_starts_and_ends_between_words(self):
        for width in (100, 160):
            for phrase in ("invoice export", "monthly statements", "new ledger", "production"):
                hit = self.LONG.find(phrase)
                text = ranking.window_of_words(self.LONG, hit, hit + len(phrase), width)
                body = text.strip("…")
                self.assertIn(phrase, body)
                self.assertLessEqual(len(body), width)
                # Words are dropped, not the budget (a match at the very end has less after it).
                if phrase != "production":
                    self.assertGreater(len(body), width * 3 // 4)
                words = self.LONG.split()
                self.assertEqual(body.split(), [word for word in body.split() if word in words], text)
                position = self.LONG.find(body)
                self.assertGreaterEqual(position, 0)
                self.assertEqual(text.startswith("…"), position > 0)
                self.assertEqual(text.endswith("…"), position + len(body) < len(self.LONG))
                if position:
                    self.assertEqual(self.LONG[position - 1], " ")

    def test_a_short_message_is_shown_whole(self):
        self.assertEqual(ranking.window_of_words("Staging is live.", 0, 7, 100), "Staging is live.")

    def test_match_inside_one_long_token_stays_in_the_window(self):
        path = "/srv/" + "/".join(f"directory{number:02d}" for number in range(40)) + "/invoice-export.csv"
        text = f"Open {path} now"
        hit = text.find("invoice-export")
        shown = ranking.window_of_words(text, hit, hit + len("invoice-export"), 100)
        self.assertIn("invoice-export", shown)
        self.assertLessEqual(len(shown.strip("…")), 100)

    def test_hit_line_and_first_line_of_a_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            write_jsonl(
                home / ".claude/projects/-Users-alex-shop/words.jsonl",
                claude_turns("Please look at the ledger problem that " + "we discussed at length " * 8, self.LONG),
            )
            _, output = run_raw("--home", tmp, "search", "invoice export")
            first = next(line for line in output.splitlines() if line.startswith(" first: "))
            hit = next(line for line in output.splitlines() if line.startswith(" hit "))
            self.assertEqual(
                hit,
                " hit 1:2 agent: Great, add a test for the invoice export and then look at the rounding of the totals, because…",
            )
            self.assertTrue(
                first.endswith(" length…") or first.endswith(" at…") or first.endswith(" discussed…"), first
            )
            self.assertLessEqual(len(first), len(" first: ") + 100)

    def test_a_hit_that_is_the_line_above_is_not_printed_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            # Short, one of two words, in the first message: the first line says it all.
            write_jsonl(home / ".claude/projects/-x/short.jsonl", claude_turns("Export the blog posts", "Done."))
            # Long: where to read on is still worth a line, the text is not repeated.
            write_jsonl(
                home / ".claude/projects/-x/long.jsonl",
                claude_turns("Export the photo archive", *[f"Step {number}" for number in range(12)]),
            )
            # The words come later: the hit line shows them.
            write_jsonl(
                home / ".claude/projects/-x/later.jsonl",
                claude_turns("Hello there", "The invoice export is done.", "Thanks"),
            )
            _, output = run_raw("--home", tmp, "search", "invoice export")
            rows = output.split("\nClaude\n")[1].split("\nclaude:")
            by_name = {row.split()[0].replace("claude:", ""): row.splitlines()[1:] for row in rows}
            self.assertEqual(by_name["short"], [" first: Export the blog posts"])
            self.assertEqual(by_name["long"], [" first: Export the photo archive", " hit 1:2"])
            self.assertEqual(by_name["later"], [" first: Hello there", " hit 1:3 agent: The invoice export is done."])

    def test_inspect_says_how_far_the_numbers_go_and_where_ids_come_from(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            write_jsonl(home / ".claude/projects/-Users-alex-shop/short.jsonl", claude_turns("One", "Two", "Three"))
            code, output = run_raw("--home", tmp, "inspect", "claude:short", "--range", "80:90")
            self.assertEqual(code, 0)
            self.assertIn("(no messages match this selection; the session has messages 1:3)", output)
            code, output = run_raw("--home", tmp, "inspect", "claude:nope")
            self.assertEqual(code, 1)
            self.assertEqual(output, 'Session not found: claude:nope (ids are printed by: search "WORDS")\n')


if __name__ == "__main__":
    unittest.main()
