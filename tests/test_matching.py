"""How a query word is matched: without diacritics, and a short word only as a whole word."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from helpers import claude_turns, run_raw, write_jsonl

from find_session.model import Session
from find_session.ranking import count_terms, match_snippet, query_terms, window_of_words
from find_session.searchtext import COMMAND_HEAD, SearchText, find_word, fold, later_part, unfolded_span


def session_of(*messages: str, project: str = "~/work", commands: tuple[str, ...] = ()) -> Session:
    session = Session("Claude", "s", project, Path("s.jsonl"), 0.0)
    for number, text in enumerate(messages):
        session.append("assistant" if number % 2 else "user", text, "")
    for command in commands:
        session.tools.append((len(session.turns), command))
    return session


def counts_of(session: Session, query: str):
    return count_terms(SearchText.of(session), query_terms(query), "")


class FoldingTests(unittest.TestCase):
    def test_fold_drops_case_and_diacritics(self):
        self.assertEqual(fold("Wdrożenie ŻÓŁTEJ gęśli"), "wdrozenie zoltej gesli")
        self.assertEqual(fold("Crème Brûlée, Ærø, Đorđe, naïve"), "creme brulee, æro, dorde, naive")
        # Written with combining marks (as macOS writes file names): the same result.
        self.assertEqual(fold("wdrożenie"), "wdrozenie")
        self.assertEqual(fold("plain ascii 123"), "plain ascii 123")
        # Nothing is taken from a script without diacritics to drop.
        self.assertEqual(fold("Привет 東京"), "привет 東京")

    def test_a_word_finds_both_spellings(self):
        session = session_of("Planujemy wdrożenie na produkcję", "Wdrozenie gotowe, lista zadań w pliku")
        for query in ("wdrożenie", "wdrozenie", "WDROŻENIE", "produkcje", "zadan", "zadań"):
            counts = counts_of(session, query)
            self.assertIsNotNone(counts, query)
            self.assertEqual(counts.found, (True,), query)
        self.assertEqual(counts_of(session, "wdrozenie").said, [[1, 1]])
        self.assertEqual(counts_of(session, "wdrożenie").said, [[1, 1]])

    def test_search_finds_the_other_spelling_and_prints_the_original_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = home / ".claude/projects/-Users-alex-work/fold-1.jsonl"
            write_jsonl(path, claude_turns("Zaczynamy", "Jutro wdrożenie na produkcję: ŻÓŁTA lista gotowa."))
            for query in ("wdrozenie zolta", "wdrożenie żółta"):
                code, output = run_raw("--home", tmp, "search", query)
                self.assertEqual(code, 0)
                self.assertIn("Claude 1, ", output.splitlines()[0], query)
                # The snippet is the text as it was written, not as it is matched.
                self.assertIn("Jutro wdrożenie na produkcję: ŻÓŁTA lista gotowa.", output, query)
                self.assertNotIn("wdrozenie na produkcje", output.split("\n", 1)[1], query)
            code, output = run_raw("--home", tmp, "inspect", "claude:fold-1", "--grep", "zolta")
            self.assertIn("ŻÓŁTA lista", output)

    def test_a_missing_word_is_named_as_it_was_typed(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_jsonl(Path(tmp) / ".claude/projects/-Users-alex-work/x.jsonl", claude_turns("Faktura gotowa", "ok"))
            _, output = run_raw("--home", tmp, "search", "faktura Żyrafa")
            self.assertIn("no session has: żyrafa", output.splitlines()[0])

    def test_snippet_positions_survive_text_whose_folded_length_differs(self):
        # "İ" lowers to two characters and a combining mark folds to none.
        text = "İİİİ " * 60 + "óó wdrożenie tutaj " + "x " * 200
        self.assertNotEqual(len(fold(text)), len(text))
        start = fold(text).index("wdrozenie")
        low, high = unfolded_span(text, start, start + len("wdrozenie"))
        self.assertEqual(text[low:high], "wdrożenie")
        session = session_of(text)
        snippet = match_snippet(session, ["wdrozenie"], "wdrozenie", width=60)
        self.assertIn("wdrożenie tutaj", snippet["text"])
        # The usual case costs nothing: same positions.
        self.assertEqual(unfolded_span("Wdrożenie", 2, 5), (2, 5))


class LongCommandWordsTests(unittest.TestCase):
    """A word far into a long command finds the session, and counts for little in the ranking."""

    LONG = (
        "cd /srv && " + " && ".join(f"step{n:02d} --flag" for n in range(40)) + " && curl deploy.example.com/latecall"
    )

    def test_the_word_is_counted_apart_from_the_start_of_the_command(self):
        self.assertGreater(self.LONG.index("latecall"), COMMAND_HEAD)
        session = session_of("Release it", "Releasing.", commands=(self.LONG,))
        counts = counts_of(session, "latecall step01 release")
        self.assertEqual(counts.found, (True, True, True))
        self.assertEqual((counts.late[0], counts.ran[0]), ([0, 1], [0, 0]))
        self.assertEqual((counts.late[1], counts.ran[1]), (None, [0, 1]))
        self.assertEqual((counts.late[2], counts.said[2]), (None, [1, 0]))
        # Only where a word starts, like in any command.
        self.assertIsNone(counts_of(session, "atecall").late[0])
        self.assertIsNone(count_terms(SearchText.of(session), ["latecall"], "", use_tools=False).late[0])

    def test_a_word_cut_by_the_limit_is_whole_in_the_later_part(self):
        for filler in ("x " * 197, "x" * 394, "x" * 330 + " " + "y" * 63):
            command = f"{filler}straddleword and more " + "z " * 100
            self.assertLess(command.index("straddleword"), COMMAND_HEAD)
            self.assertGreater(command.index("straddleword") + len("straddleword"), COMMAND_HEAD)
            self.assertIn("straddleword", later_part(command), filler[-5:])
            counts = counts_of(session_of("Run it", commands=(command,)), "straddleword")
            self.assertTrue(counts.found[0])
        self.assertEqual(later_part("short command"), "")

    def test_it_lists_the_session_below_one_that_says_the_word(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            rows = claude_turns("Release the shop", "Releasing.")
            rows.append(
                {
                    "timestamp": "2026-09-22T10:00:30Z",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": self.LONG}}],
                    },
                }
            )
            write_jsonl(home / ".claude/projects/-Users-alex-work/late-1.jsonl", rows)
            write_jsonl(
                home / ".claude/projects/-Users-alex-work/said-1.jsonl",
                claude_turns("What does latecall do?", "It notifies.", start="2026-09-01T10:00:00Z"),
            )
            _, output = run_raw("--home", tmp, "search", "latecall", "--json")
            payload = json.loads(output)
            self.assertEqual(payload["counts"]["Claude"], 2)
            said, late = payload["results"]["Claude"]
            self.assertEqual((said["session_id"], late["session_id"]), ("said-1", "late-1"))
            self.assertGreater(said["score"], late["score"] + 500)
            self.assertGreater(late["score"], 0)
            self.assertEqual(late["match"]["role"], "tool")
            self.assertIn("latecall", late["match"]["text"])
            # The header counts it as a session that has the word.
            _, output = run_raw("--home", tmp, "search", "latecall nothing-else")
            self.assertIn("Claude 0 (+2 partial)", output)


class HitLineTests(unittest.TestCase):
    """The line quoted for a hit shows why the session matched."""

    def test_among_messages_with_one_word_each_the_rarer_word_is_quoted(self):
        # Every message of the stretch has the common word; one has the rare one.
        messages = [f"Step {number} of the ledger: the ledger is tidy." for number in range(6)]
        messages[2] = "I made a backup of the database before deleting the test entries."
        session = session_of(*messages)
        terms = query_terms("ledger backups copies")
        self.assertEqual(match_snippet(session, terms, "ledger backups copies")["n"], 6)
        terms = query_terms("ledger backup copy")
        snippet = match_snippet(session, terms, "ledger backup copy")
        self.assertEqual(snippet["n"], 3)
        self.assertIn("backup of the database", snippet["text"])
        # A search knows how many sessions have each word, and says so: the same choice.
        self.assertEqual(match_snippet(session, terms, "ledger backup copy", rarity=[1 / 50, 1 / 3, 1 / 9])["n"], 3)
        # More words still beat a rarer word.
        messages[4] = "The ledger is tidy, with a copy on disk."
        self.assertEqual(match_snippet(session_of(*messages), terms, "ledger backup copy")["n"], 5)

    def test_the_search_quotes_the_message_with_the_rare_word(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            for number in range(4):
                write_jsonl(
                    home / f".claude/projects/-Users-alex-work/common-{number}.jsonl",
                    claude_turns(f"Open the ledger, part {number}", "The ledger is open."),
                )
            texts = [f"Ledger step {number}." for number in range(40)]
            texts[30] = "Before that I made a backup of the database."
            texts[33] = "The ledger needs one more pass."
            write_jsonl(home / ".claude/projects/-Users-alex-work/long-1.jsonl", claude_turns(*texts))
            _, output = run_raw("--home", tmp, "search", "ledger backup removed")
            row = output[output.index("claude:long-1") :].split("\nclaude:")[0]
            self.assertIn(" user: Before that I made a backup of the database.", row)
            self.assertNotIn("one more pass", row)

    def test_the_window_holds_the_words_that_are_close_together(self):
        filler = "lorem ipsum dolor sit amet " * 12
        text = f"export {filler}the nightly export of the invoice table failed again {filler}"
        session = session_of(text)
        for width in (60, 100, 160):
            snippet = match_snippet(session, ["invoice", "export"], "invoice export", width=width)["text"]
            self.assertIn("export of the invoice", snippet)
            self.assertLessEqual(len(snippet), width + 2)
            self.assertTrue(snippet.startswith("…") and snippet.endswith("…"))
            # Whole words at both ends.
            self.assertIn(f" {snippet.strip('…').split()[0]} ", f" {text} ")
            self.assertIn(f" {snippet.strip('…').split()[-1]} ", f" {text} ")
        # Words too far apart for one window: the rarer one is shown.
        apart = f"export {filler}{filler}{filler}invoice"
        snippet = match_snippet(session_of(apart, "export export"), ["invoice", "export"], "invoice export", width=60)
        self.assertIn("invoice", snippet["text"])


class ShortWordTests(unittest.TestCase):
    def test_a_short_word_matches_only_as_a_whole_word(self):
        for text, expected in (
            ("API zwraca błąd", 1),
            ("opis API-ego, api_v2 oraz (API).", 3),
            ("„API” — opis", 1),
            ("the capital report renders rapidly", 0),
            ("therapist and apiary, apis", 0),
            ("apiAPI API2 2API", 0),
            # A letter of another script next to it is still a letter.
            ("мирapi apiж", 0),
            ("→api← …api…", 2),
        ):
            counts = counts_of(session_of(text), "API")
            self.assertEqual(sum(counts.said[0]) if counts else 0, expected, text)
            self.assertEqual(find_word(fold(text), "api") >= 0, bool(expected), text)

    def test_longer_words_are_still_found_inside_words(self):
        counts = counts_of(session_of("we exported the reexport twice"), "export")
        self.assertEqual(counts.said, [[2]])

    def test_the_rule_holds_in_commands_and_in_the_project_path(self):
        session = session_of(
            "start", project="~/projects/sprint-rapid", commands=("$ dbt run --db prod", "$ db-migrate")
        )
        self.assertIsNone(counts_of(session, "api"))
        self.assertIsNone(counts_of(session, "pr"))
        counts = counts_of(session, "db")
        # `--db` and `db-migrate`, not `dbt`.
        self.assertEqual(counts.ran, [[2]])
        counts = counts_of(session_of("x", project="~/clients/api-gateway"), "api")
        self.assertEqual(counts.in_project, [True])

    def test_a_session_with_the_word_only_inside_other_words_is_not_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            write_jsonl(
                home / ".claude/projects/-Users-alex-work/noise.jsonl",
                claude_turns("The capital report renders rapidly", "Restarting the posting worker."),
            )
            write_jsonl(
                home / ".claude/projects/-Users-alex-work/bank.jsonl",
                claude_turns("Limit w API za wrzesień", "Podniesiony."),
            )
            code, output = run_raw("--home", tmp, "search", "API", "--json")
            data = json.loads(output)
            self.assertEqual(data["counts"]["Claude"], 1)
            self.assertEqual([row["session_id"] for row in data["results"]["Claude"]], ["bank"])
            # A word that only occurs inside other words is reported as missing.
            _, output = run_raw("--home", tmp, "search", "API posting")
            self.assertIn("Claude 0 (+2 partial)", output.splitlines()[0])
            _, output = run_raw("--home", tmp, "search", "est posting")
            self.assertIn("no session has: est", output.splitlines()[0])
            # inspect --grep follows the same rule.
            _, output = run_raw("--home", tmp, "inspect", "claude:noise", "--grep", "api")
            self.assertIn("no messages match", output)
            _, output = run_raw("--home", tmp, "inspect", "claude:bank", "--grep", "api")
            self.assertIn("Limit w API", output)

    def test_the_index_and_a_fresh_parse_agree(self):
        with tempfile.TemporaryDirectory() as tmp:
            import os
            from unittest import mock

            home = Path(tmp) / "home"
            write_jsonl(
                home / ".claude/projects/-Users-alex-work/a.jsonl",
                claude_turns("Wdrożenie API w sklepie", "API gotowe, apis listed, PR otwarty"),
            )
            _, fresh = run_raw("--home", str(home), "search", "wdrozenie api pr", "--json")
            with mock.patch.dict(os.environ, {"FIND_SESSION_CACHE": str(Path(tmp) / "cache")}):
                run_raw("--home", str(home), "search", "wdrozenie api pr", "--json")
                _, indexed = run_raw("--home", str(home), "search", "wdrozenie api pr", "--json")
            self.assertEqual(json.loads(indexed)["results"], json.loads(fresh)["results"])
            self.assertEqual(json.loads(fresh)["counts_all_words"]["Claude"], 1)


class SnippetCutTests(unittest.TestCase):
    def test_a_long_path_is_cut_between_two_of_its_parts(self):
        path = "/Users/alex/Documents/Reports-2026-quarterly-archive/exports/latest/export_active_listings.py"
        text = "$ python3 " + path + " --all " + "--flag value " * 20
        hit = text.index("export_active")
        shown = window_of_words(text, hit, hit + len("export_active"), 60)
        self.assertTrue(shown.startswith("…/"), shown)
        self.assertIn("export_active_listings.py", shown)
        # Every part that is shown is a whole part of the path.
        for part in shown.strip("…").split()[0].strip("/").split("/"):
            self.assertIn(f"/{part}", path, shown)
        # The end is cut the same way when the match is at the start of a long path.
        text = "see " + "word " * 30 + path
        hit = text.index("Documents")
        shown = window_of_words(text, hit, hit + len("Documents"), 50)
        self.assertTrue(shown.endswith("/…"), shown)


if __name__ == "__main__":
    unittest.main()
