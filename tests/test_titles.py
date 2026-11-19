"""Session titles: where they come from, where they are shown, and that they change no ranking."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import (
    FAKE,
    OMP_BUCKET,
    claude_title,
    claude_turns,
    codex_answer,
    codex_meta,
    codex_user,
    omp_header,
    omp_msg,
    run_raw,
    write_codex_titles,
    write_jsonl,
    write_omp,
)

from find_session import index as index_module

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "find-session" / "scripts"

CLAUDE_DIR = ".claude/projects/-Users-alex-work"
CODEX_DIR = ".codex/sessions/2026/09/22"


def claude_session(home: Path, name: str, *titles: dict, texts: tuple[str, ...] = ()) -> Path:
    """A two-message Claude session about TITLETOPIC, with title records after the first message."""
    first, *rest = claude_turns(*(texts or ("Plan the TITLETOPIC migration", "Planned.")))
    path = home / CLAUDE_DIR / f"{name}.jsonl"
    write_jsonl(path, [first, *titles, *rest])
    return path


def codex_session(home: Path, session_id: str, text: str = "Review the TITLETOPIC rollout") -> Path:
    # The file name is not the session id: titles are attached by the id inside the file.
    path = home / CODEX_DIR / f"rollout-2026-09-22T10-00-00-{session_id}.jsonl"
    write_jsonl(path, [codex_meta(session_id), codex_user(text), codex_answer("Reviewed.")])
    return path


def search_json(home: Path, *leading: str, query: str = "TITLETOPIC") -> dict:
    code, output = run_raw("--home", str(home), *leading, "search", query, "--json")
    assert code == 0, output
    return json.loads(output)


def title_of(home: Path, source: str, session_id: str) -> str | None:
    for record in search_json(home)["results"][source]:
        if record["session_id"] == session_id:
            return record["title"]
    raise AssertionError(f"{session_id} not in the results")


class TitleCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "history"
        self.cache_dir = Path(tmp.name) / "cache"

    def with_index(self):
        patcher = mock.patch.dict(os.environ, {"FIND_SESSION_CACHE": str(self.cache_dir)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def no_parsing(self):
        return mock.patch.object(index_module, "parse_candidate", side_effect=AssertionError("parsed"))


class TitleSourceTests(TitleCase):
    def test_claude_title_records(self):
        claude_session(self.home, "untitled")
        claude_session(self.home, "generated", claude_title("ai", "First guess"), claude_title("ai", "Better guess"))
        claude_session(
            self.home,
            "named",
            claude_title("custom", "Old name"),
            claude_title("ai", "A guess made later"),
            claude_title("custom", "  Chosen\n name  "),
            claude_title("custom", ""),
        )
        claude_session(self.home, "named-first", claude_title("custom", "Kept name"), claude_title("ai", "Late guess"))
        self.assertIsNone(title_of(self.home, "Claude", "untitled"))
        # The last record of a kind counts.
        self.assertEqual(title_of(self.home, "Claude", "generated"), "Better guess")
        # The user's name wins over a generated one, even an earlier one; an empty record changes nothing.
        self.assertEqual(title_of(self.home, "Claude", "named"), "Chosen name")
        self.assertEqual(title_of(self.home, "Claude", "named-first"), "Kept name")

    def test_codex_thread_names(self):
        codex_session(self.home, "thread-named")
        codex_session(self.home, "thread-renamed")
        codex_session(self.home, "thread-blank")
        codex_session(self.home, "thread-unnamed")
        self.assertIsNone(title_of(self.home, "Codex", "thread-named"))
        path = write_codex_titles(
            self.home,
            ("thread-named", "Rollout review"),
            ("thread-renamed", "First name"),
            ("thread-blank", "Kept name"),
            ("thread-renamed", "Second name"),
            ("thread-blank", " "),
            ("thread-elsewhere", "No such session"),
        )
        with path.open("a") as handle:
            handle.write('not json\n[1]\n{"id": 5, "thread_name": "x"}\n{"id": "thread-named", "thread_name": "cut o')
        self.assertEqual(title_of(self.home, "Codex", "thread-named"), "Rollout review")
        self.assertEqual(title_of(self.home, "Codex", "thread-renamed"), "Second name")
        self.assertEqual(title_of(self.home, "Codex", "thread-blank"), "Kept name")
        self.assertIsNone(title_of(self.home, "Codex", "thread-unnamed"))

    def test_omp_title_slot_and_header(self):
        rows = [omp_msg("u1", None, "user", "Plan the TITLETOPIC pilot")]
        bucket = self.home / OMP_BUCKET
        write_omp(bucket / "2026-09-22T10-00-00-000Z_slot.jsonl", [omp_header("slot"), *rows], title="Pilot plan")
        write_omp(
            bucket / "2026-09-22T10-00-01-000Z_header.jsonl",
            [{**omp_header("header"), "title": "Older file"}, *rows],
            title=None,
        )
        write_omp(
            bucket / "2026-09-22T10-00-02-000Z_both.jsonl",
            [{**omp_header("both"), "title": "Header name"}, *rows],
            title="Slot name",
        )
        write_omp(bucket / "2026-09-22T10-00-03-000Z_none.jsonl", [omp_header("none"), *rows], title=None)
        self.assertEqual(title_of(self.home, "omp", "slot"), "Pilot plan")
        self.assertEqual(title_of(self.home, "omp", "header"), "Older file")
        self.assertEqual(title_of(self.home, "omp", "both"), "Slot name")
        self.assertIsNone(title_of(self.home, "omp", "none"))

    def test_titles_are_redacted_everywhere(self):
        self.with_index()
        secret = FAKE["github"]
        claude_session(self.home, "named", claude_title("custom", f"Deploy with {secret}"))
        claude_session(self.home, "generated", claude_title("ai", f"Token {secret} rotation"))
        codex_session(self.home, "thread-named")
        write_codex_titles(self.home, ("thread-named", f"Rollout, key {secret}"))
        write_omp(
            self.home / OMP_BUCKET / "2026-09-22T10-00-00-000Z_slot.jsonl",
            [omp_header("slot"), omp_msg("u1", None, "user", "Plan the TITLETOPIC pilot")],
            title=f"Pilot {secret}",
        )
        for leading in ((), ("--no-cache",)):
            for command in (
                ["search", "TITLETOPIC"],
                ["search", "TITLETOPIC", "--full"],
                ["search", "TITLETOPIC", "--json"],
                ["inspect", "claude:named"],
                ["inspect", "codex:thread-named", "--json"],
                ["inspect", "omp:slot"],
                ["index"],
                ["recent", "--hours", "1000000"],
            ):
                _, output = run_raw("--home", str(self.home), *leading, *command)
                self.assertNotIn(secret, output, command)
                self.assertIn("[REDACTED]", output, command)
        self.assertEqual(title_of(self.home, "Codex", "thread-named"), "Rollout, key [REDACTED]")
        for path in self.cache_dir.iterdir():
            self.assertNotIn(secret.encode(), path.read_bytes(), path.name)

    def test_long_title_is_bounded(self):
        claude_session(self.home, "wordy", claude_title("custom", "word " * 400))
        self.assertEqual(len(title_of(self.home, "Claude", "wordy")), 180)
        _, compact = run_raw("--home", str(self.home), "search", "TITLETOPIC")
        [line] = [line for line in compact.splitlines() if line.startswith(" title: ")]
        self.assertEqual(len(line), len(" title: ") + 100)


class TitleOutputTests(TitleCase):
    def setUp(self):
        super().setUp()
        claude_session(self.home, "named", claude_title("custom", "Migration plan"))
        claude_session(self.home, "untitled")
        codex_session(self.home, "thread-named")
        write_codex_titles(self.home, ("thread-named", "Rollout review"))

    def test_compact_rows_show_the_title_instead_of_the_first_message(self):
        _, output = run_raw("--home", str(self.home), "search", "TITLETOPIC")
        rows = re.split(r"\n(?=claude:|codex:)", output)
        named = next(row for row in rows if row.startswith("claude:named "))
        untitled = next(row for row in rows if row.startswith("claude:untitled "))
        codex = next(row for row in rows if row.startswith("codex:"))
        # The title replaces the first message; the word is not in the title, so the hit line shows it.
        # Rows are sliced, not taken whole: the last row of a section is followed by the next section's header.
        self.assertEqual(
            named.splitlines()[1:3], [" title: Migration plan", " hit 1:2 user: Plan the TITLETOPIC migration"]
        )
        self.assertEqual(
            codex.splitlines()[1:3], [" title: Rollout review", " hit 1:2 user: Review the TITLETOPIC rollout"]
        )
        # Without a title the row is as before, and the first message already shows the word.
        self.assertEqual(untitled.splitlines()[1:2], [" first: Plan the TITLETOPIC migration"])
        self.assertNotIn(" hit ", untitled.split("\n\n")[0])

    def test_no_hit_line_when_a_short_session_s_title_shows_the_words(self):
        claude_session(self.home, "telling", claude_title("ai", "TITLETOPIC migration plan"))
        _, output = run_raw("--home", str(self.home), "search", "titletopic plan")
        row = next(row for row in re.split(r"\n(?=claude:|codex:)", output) if row.startswith("claude:telling "))
        self.assertEqual(row.split("\n\n")[0].strip().splitlines()[1:], [" title: TITLETOPIC migration plan"])

    def test_last_request_is_never_in_a_compact_row(self):
        path = self.home / CLAUDE_DIR / "several.jsonl"
        write_jsonl(path, claude_turns("Start the TITLETOPIC work", "ok", "Now the LASTWORD part", "done"))
        _, output = run_raw("--home", str(self.home), "search", "TITLETOPIC")
        self.assertNotIn(" last:", output)
        self.assertNotIn("LASTWORD", output)
        # A word only the last request has is still found, and shown by the hit line.
        _, output = run_raw("--home", str(self.home), "search", "LASTWORD")
        self.assertIn(" first: Start the TITLETOPIC work\n hit 2:4 user: Now the LASTWORD part\n", output)
        # JSON keeps the key.
        [record] = search_json(self.home, query="LASTWORD")["results"]["Claude"]
        self.assertEqual(record["last"], "Now the LASTWORD part")
        self.assertIsNone(record["title"])

    def test_full_and_json_keep_what_they_had(self):
        _, full = run_raw("--home", str(self.home), "search", "TITLETOPIC", "--full")
        self.assertIn("Session: named\nTitle: Migration plan\nStart: Plan the TITLETOPIC migration\n", full)
        self.assertIn("Session: untitled\nStart: Plan the TITLETOPIC migration\n", full)
        self.assertEqual(full.count("Title: "), 2)
        records = {record["session_id"]: record for record in search_json(self.home)["results"]["Claude"]}
        self.assertEqual(records["named"]["title"], "Migration plan")
        self.assertEqual(records["named"]["first"], "Plan the TITLETOPIC migration")
        self.assertEqual(records["named"]["last"], "Plan the TITLETOPIC migration")
        self.assertIsNone(records["untitled"]["title"])

    def test_inspect_index_and_recent(self):
        _, transcript = run_raw("--home", str(self.home), "inspect", "claude:named")
        header, title, blank = transcript.splitlines()[:3]
        self.assertTrue(header.startswith("# Claude · ~/work · named · "), header)
        self.assertEqual((title, blank), ("Title: Migration plan", ""))
        _, transcript = run_raw("--home", str(self.home), "inspect", "claude:untitled")
        self.assertNotIn("Title:", transcript)
        self.assertEqual(transcript.splitlines()[1], "")
        _, output = run_raw("--home", str(self.home), "inspect", "codex:thread-named", "--json")
        self.assertEqual(json.loads(output)["title"], "Rollout review")
        _, output = run_raw("--home", str(self.home), "inspect", "claude:untitled", "--json")
        self.assertIsNone(json.loads(output)["title"])
        _, listing = run_raw("--home", str(self.home), "index")
        self.assertEqual(listing.splitlines()[0], "index: 3 sessions, newest first (in all: Claude 2, Codex 1, omp 0)")
        self.assertRegex(listing, r"\nclaude:named [\d-]+ [\d:]+ ~/work · 2 msgs · Migration plan\n")
        self.assertRegex(listing, r"\nclaude:untitled [\d-]+ [\d:]+ ~/work · 2 msgs · Plan the TITLETOPIC migration\n")
        self.assertRegex(listing, r"\ncodex:thread-n\S* [\d-]+ [\d:]+ \S+ · \d+ msgs · Rollout review\n")
        _, recent = run_raw("--home", str(self.home), "recent", "--hours", "1000000")
        self.assertIn("· named\n- title: Migration plan\n- start: Plan the TITLETOPIC migration\n", recent)
        self.assertIn("· untitled\n- start: Plan the TITLETOPIC migration\n", recent)

    def test_every_command_prints_the_same_with_and_without_the_index(self):
        commands = [
            ["search", "TITLETOPIC"],
            ["search", "TITLETOPIC", "--full"],
            ["search", "TITLETOPIC", "--json"],
            ["inspect", "claude:named"],
            ["inspect", "codex:thread-named", "--json"],
            ["index"],
            ["recent", "--hours", "1000000"],
        ]

        def outputs(*leading: str) -> list[str]:
            return [
                re.sub(r"(built|collected) [\d-]+ [\d:]+", "NOW", run_raw("--home", str(self.home), *leading, *c)[1])
                for c in commands
            ]

        expected = outputs("--no-cache")
        self.assertIn("Migration plan", expected[0])
        self.with_index()
        self.assertEqual(outputs(), expected, "index built")
        with self.no_parsing():
            self.assertEqual(outputs(), expected, "index used")


class TitleRankingTests(TitleCase):
    def test_a_title_is_neither_searched_nor_scored(self):
        texts = ("Plan the TITLETOPIC migration", "Planned.")
        claude_session(self.home, "plain", texts=texts)
        titled = claude_session(
            self.home, "titled", claude_title("custom", "TITLETOPIC titletopic ONLYINTITLE"), texts=texts
        )
        plain = self.home / CLAUDE_DIR / "plain.jsonl"
        os.utime(titled, (plain.stat().st_atime, plain.stat().st_mtime))
        for leading in ((), ("--no-cache",)):
            if not leading:
                self.with_index()
            records = {r["session_id"]: r for r in search_json(self.home, *leading)["results"]["Claude"]}
            self.assertEqual(records["titled"]["score"], records["plain"]["score"], leading)
            self.assertEqual(records["titled"]["chunk"], records["plain"]["chunk"], leading)
            _, output = run_raw("--home", str(self.home), *leading, "search", "ONLYINTITLE")
            self.assertEqual(output, 'search "ONLYINTITLE": no matches (Claude 0, Codex 0, omp 0)\n', leading)
            _, output = run_raw("--home", str(self.home), *leading, "inspect", "claude:titled", "--grep", "ONLYINTITLE")
            self.assertIn("(no messages match this selection)", output, leading)


class TitleIndexTests(TitleCase):
    def setUp(self):
        super().setUp()
        self.with_index()
        claude_session(self.home, "named", claude_title("custom", "Migration plan"))
        self.rollout = codex_session(self.home, "thread-named")
        codex_session(self.home, "thread-other")

    def codex_titles(self) -> dict[str, str | None]:
        return {r["session_id"]: r["title"] for r in search_json(self.home)["results"]["Codex"]}

    def test_codex_title_change_is_noticed_without_parsing_any_session(self):
        self.assertEqual(self.codex_titles(), {"thread-named": None, "thread-other": None})
        with self.no_parsing():
            # New file.
            path = write_codex_titles(self.home, ("thread-named", "Rollout review"))
            self.assertEqual(self.codex_titles(), {"thread-named": "Rollout review", "thread-other": None})
            # A rename and a first name, appended as Codex does.
            with path.open("a") as handle:
                handle.write(json.dumps({"id": "thread-named", "thread_name": "Rollout review, round two"}) + "\n")
                handle.write(json.dumps({"id": "thread-other", "thread_name": "Something else"}) + "\n")
            expected = {"thread-named": "Rollout review, round two", "thread-other": "Something else"}
            self.assertEqual(self.codex_titles(), expected)
            # Same size, other content, later time.
            text = path.read_text()
            path.write_text(text.replace("Something else", "Something new!"))
            stat = path.stat()
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))
            self.assertEqual(self.codex_titles()["thread-other"], "Something new!")
            # Unchanged file: the stored titles are used and the file is not read.
            with mock.patch("find_session.titles.read_codex_titles", side_effect=AssertionError("read again")):
                self.assertEqual(self.codex_titles()["thread-other"], "Something new!")
                _, compact = run_raw("--home", str(self.home), "search", "TITLETOPIC")
                self.assertIn(" title: Rollout review, round two\n", compact)
            # Gone again.
            path.unlink()
            self.assertEqual(self.codex_titles(), {"thread-named": None, "thread-other": None})
        for name in self.cache_dir.iterdir():
            self.assertNotIn(b"Rollout review", name.read_bytes(), name.name)

    def test_codex_title_is_kept_when_the_rollout_grows(self):
        write_codex_titles(self.home, ("thread-named", "Rollout review"))
        self.assertEqual(self.codex_titles()["thread-named"], "Rollout review")
        with self.rollout.open("a") as handle:
            handle.write(json.dumps(codex_user("And the TITLETOPIC follow-up")) + "\n")
        self.assertEqual(self.codex_titles()["thread-named"], "Rollout review")
        _, output = run_raw("--home", str(self.home), "inspect", "codex:thread-named")
        self.assertIn("Title: Rollout review", output)
        self.assertIn("And the TITLETOPIC follow-up", output)

    def test_claude_title_appended_to_an_indexed_session(self):
        self.assertEqual(title_of(self.home, "Claude", "named"), "Migration plan")
        path = self.home / CLAUDE_DIR / "named.jsonl"
        with path.open("a") as handle:
            handle.write(json.dumps(claude_title("custom", "Migration plan, final")) + "\n")
        self.assertEqual(title_of(self.home, "Claude", "named"), "Migration plan, final")
        with self.no_parsing():
            _, compact = run_raw("--home", str(self.home), "search", "TITLETOPIC")
        self.assertIn(" title: Migration plan, final\n", compact)

    def test_warm_search_shows_titles_without_loading_the_parsing_modules(self):
        write_codex_titles(self.home, ("thread-named", "Rollout review"))
        run_raw("--home", str(self.home), "search", "TITLETOPIC")
        program = (
            "import sys\n"
            "from find_session.cli import main\n"
            "code = main(sys.argv[1:])\n"
            "print('LOADED', ' '.join(sorted(sys.modules)), file=sys.stderr)\n"
            "raise SystemExit(code)\n"
        )
        env = dict(os.environ, FIND_SESSION_CACHE=str(self.cache_dir), PYTHONPATH=str(SCRIPTS))
        result = subprocess.run(
            [sys.executable, "-c", program, "--home", str(self.home), "search", "titletopic"],
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(" title: Rollout review\n", result.stdout)
        self.assertIn(" title: Migration plan\n", result.stdout)
        loaded = set(result.stderr.split("LOADED", 1)[1].split())
        unwanted = {"find_session.titles", "find_session.redact", "find_session.jsonl", "json"}
        self.assertEqual(loaded & unwanted, set())


if __name__ == "__main__":
    unittest.main()
