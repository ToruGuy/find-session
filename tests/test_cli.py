"""The command line itself: names, help, errors, exit codes, pipes, progress."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import claude_turns, run_raw, write_jsonl

from find_session import cli
from find_session import index as index_module

REPO = Path(__file__).resolve().parents[1]
SKILL_ENTRY = REPO / "skills" / "find-session" / "scripts" / "sessions.py"
CLONE_ENTRY = REPO / "scripts" / "sessions.py"
PACKAGE_PARENT = SKILL_ENTRY.parent


def clean_env(**extra: str) -> dict:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("FIND_SESSION_", "CLAUDE_CODE_", "CODEX_"))
    }
    # No index and no config of the machine the tests run on.
    return {**env, "FIND_SESSION_CACHE": "off", "FIND_SESSION_CONFIG": os.environ["FIND_SESSION_CONFIG"], **extra}


def run_process(*argv: str, env: dict | None = None, **options) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *argv], env=env or clean_env(), capture_output=True, text=True, **options)


def help_of(*argv: str) -> str:
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured), contextlib.suppress(SystemExit):
        cli.main([*argv, "--help"])
    return " ".join(captured.getvalue().split())


class ProgramNameTests(unittest.TestCase):
    def test_usage_and_errors_name_the_program_as_it_was_started(self):
        for argv, name in (
            ([str(SKILL_ENTRY)], "sessions.py"),
            ([str(CLONE_ENTRY)], "sessions.py"),
            (["-m", "find_session"], "python3 -m find_session"),
        ):
            env = clean_env(PYTHONPATH=str(PACKAGE_PARENT))
            wrong = run_process(*argv, "serach", "x", env=env)
            self.assertEqual(wrong.returncode, 2)
            self.assertIn(f"usage: {name} ", wrong.stderr)
            self.assertIn(f"{name}: error: argument COMMAND: invalid choice: 'serach'", wrong.stderr)
            usage = run_process(*argv, "--help", env=env)
            self.assertIn(f"`{name} COMMAND --help`", " ".join(usage.stdout.split()))

    def test_installed_command_and_npx_wrapper_are_named_too(self):
        with mock.patch.object(sys, "argv", ["/home/alex/.local/bin/find-session"]):
            self.assertEqual(cli.program_name(), "find-session")
        with mock.patch.dict(os.environ, {"FIND_SESSION_PROG": "npx find-session"}):
            self.assertEqual(cli.program_name(), "npx find-session")

    def test_python_older_than_3_9_gets_one_line_and_exit_code_3(self):
        for entry in (SKILL_ENTRY, CLONE_ENTRY):
            code = (
                "import runpy, sys\n"
                "sys.version_info = (3, 8, 20, 'final', 0)\n"
                f"sys.argv = [{str(entry)!r}, 'sources']\n"
                f"runpy.run_path({str(entry)!r}, run_name='__main__')\n"
            )
            result = run_process("-c", code)
            self.assertEqual(result.returncode, 3, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertRegex(
                result.stderr, r"\Afind-session: Python 3\.9 or newer is needed, but .* is Python 3\.8\.\n\Z"
            )


class HelpTests(unittest.TestCase):
    def test_every_command_is_listed_with_what_it_does(self):
        text = help_of()
        for command in cli.COMMANDS:
            self.assertIn(f" {command} ", text)
        self.assertNotIn("brief-input", text)
        self.assertIn("any of them", text)

    def test_search_help_says_how_words_are_matched_and_what_children_are(self):
        text = help_of("search")
        self.assertIn("at least one of the words", text)
        self.assertIn("only where a word starts", text)
        self.assertIn("a word of up to three characters only as a whole word", text)
        self.assertIn("Case and diacritics are ignored", text)
        self.assertIn("subagent sessions and Codex review threads", text)
        self.assertNotIn("diagnostic", text)

    def test_every_option_has_a_description(self):
        for command in cli.COMMANDS:
            for action in cli.parser()._subparsers._group_actions[0].choices[command]._actions:
                self.assertTrue(action.help, f"{command}: {action.dest} has no help")

    def test_recent_help_gives_its_defaults(self):
        text = help_of("recent")
        self.assertIn("how far back to look (default 48)", text)

    def test_older_command_name_still_works_and_a_query_of_that_name_is_a_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_jsonl(Path(tmp) / ".claude/projects/-x/a.jsonl", claude_turns("The brief-input alias", "ok"))
            code, output = run_raw("--home", tmp, "brief-input", "--hours", "1000000")
            self.assertEqual(code, 0)
            self.assertIn("# Recent activity", output)
            code, output = run_raw("--home", tmp, "search", "brief-input")
            self.assertEqual(code, 0)
            self.assertIn('search "brief-input": Claude 1', output)


class ArgumentTests(unittest.TestCase):
    """Mistakes agents make with the arguments: taken as meant, or answered in one line."""

    def run_split(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_several_words_need_no_quotes(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_jsonl(Path(tmp) / ".claude/projects/-x/a.jsonl", claude_turns("The invoice export is slow", "ok"))
            quoted = run_raw("--home", tmp, "search", "invoice export")
            self.assertIn('search "invoice export": Claude 1', quoted[1])
            self.assertEqual(run_raw("--home", tmp, "search", "invoice", "export"), quoted)
            self.assertEqual(run_raw("--home", tmp, "search", "--limit", "5", "invoice", "export"), quoted)
            # Also around an option.
            self.assertEqual(run_raw("--home", tmp, "search", "invoice", "--limit", "5", "export"), quoted)
            _, output = run_raw("--home", tmp, "search", "invoice export", "slow")
            self.assertIn('search "invoice export slow": Claude 1', output)
            code, output = run_raw("--home", tmp, "search", "invoice", "export", "--json")
            self.assertEqual(json.loads(output)["query"], "invoice export")

    def test_an_option_of_another_command_gets_one_line_that_names_the_call(self):
        for argv, expected in (
            (
                ("search", "deploy", "--tools"),
                "sessions.py: error: --tools is an option of inspect, not of search: "
                "sessions.py inspect SELECTOR --range A:B --tools\n",
            ),
            (
                ("search", "deploy", "--range", "3:9"),
                "sessions.py: error: --range is an option of inspect, not of search: "
                "sessions.py inspect SELECTOR --range A:B\n",
            ),
            (
                ("search", "deploy", "--grep=word"),
                "sessions.py: error: --grep is an option of inspect, not of search: "
                "sessions.py inspect SELECTOR --grep WORDS\n",
            ),
            (
                ("inspect", "claude:x", "--since", "3d"),
                "sessions.py: error: --since is an option of search and index, not of inspect: "
                'sessions.py search "WORDS" --since WHEN\n',
            ),
            (
                ("recent", "--json"),
                "sessions.py: error: --json is an option of search and inspect, not of recent: "
                'sessions.py search "WORDS" --json\n',
            ),
            (
                ("search", "deploy", "--no-cache"),
                "sessions.py: error: --no-cache goes before the command: sessions.py --no-cache search …\n",
            ),
            (
                ("inspect", "claude:x", "--home", "/somewhere"),
                "sessions.py: error: --home goes before the command: sessions.py --home HOME inspect …\n",
            ),
            (
                ("search", "deploy", "--nearby", "3"),
                "sessions.py: error: unrecognized arguments: --nearby 3 "
                "(options of search: sessions.py search --help)\n",
            ),
            (
                ("inspect", "claude:x", "claude:y"),
                "sessions.py: error: unrecognized arguments: claude:y "
                "(options of inspect: sessions.py inspect --help)\n",
            ),
        ):
            with tempfile.TemporaryDirectory() as tmp, mock.patch.object(sys, "argv", ["/skill/scripts/sessions.py"]):
                code, out, err = self.run_split("--home", tmp, *argv)
            self.assertEqual((code, out, err), (2, "", expected), argv)

    def test_every_option_of_inspect_is_explained_on_search_and_the_other_way_round(self):
        parsers = cli.parser().parse_args(["sources"]).parsers
        own = {
            name: {option for action in parser._actions for option in action.option_strings if option != "-h"}
            for name, parser in parsers.items()
        }
        for command, other, rest in (("search", "inspect", ["words"]), ("inspect", "search", ["claude:x"])):
            for option in sorted(own[other] - own[command] - {"--help"}):
                with tempfile.TemporaryDirectory() as tmp:
                    code, out, err = self.run_split("--home", tmp, command, *rest, option)
                self.assertEqual(code, 2, option)
                self.assertEqual(len(err.splitlines()), 1, err)
                self.assertIn(f"{option} is an option of {other}", err)

    def test_source_is_taken_in_any_case(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_jsonl(Path(tmp) / ".claude/projects/-x/a.jsonl", claude_turns("CASETOPIC", "ok"))
            code, output = run_raw("--home", tmp, "search", "casetopic", "--source", "Claude")
            self.assertEqual(code, 0)
            self.assertIn('search "casetopic": Claude 1', output)


class PipeAndInterruptTests(unittest.TestCase):
    def test_reader_that_is_gone_gets_no_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_jsonl(Path(tmp) / "home/.claude/projects/-x/pipe.jsonl", claude_turns("PIPETOPIC", "ok"))
            (Path(tmp) / "install-home/.claude").mkdir(parents=True)
            for command in (
                [str(SKILL_ENTRY), "--home", f"{tmp}/home", "search", "PIPETOPIC"],
                [str(SKILL_ENTRY), "--home", f"{tmp}/home", "index"],
                [str(REPO / "scripts" / "install_skill.py"), "--home", f"{tmp}/install-home"],
            ):
                process = subprocess.Popen(
                    [sys.executable, *command], env=clean_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE
                )
                # Closed before the program has written anything: its first write finds no reader.
                process.stdout.close()
                error = process.stderr.read().decode()
                process.stderr.close()
                self.assertEqual(process.wait(), 0, error)
                self.assertEqual(error, "", command)

    def test_interrupt_keeps_what_was_indexed_and_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            for number in range(3):
                write_jsonl(home / f".claude/projects/-x/stop-{number}.jsonl", claude_turns("STOPTOPIC", "ok"))
            seen = []

            def interrupted(*args, **kwargs):
                # The files are parsed, then the user presses Ctrl-C.
                cache = cli.cache.CACHE
                seen.extend(entry.session_id for entry in cache.entries(cli_candidates(home)))
                raise KeyboardInterrupt

            with mock.patch.dict(os.environ, {"FIND_SESSION_CACHE": str(Path(tmp) / "cache")}):
                with mock.patch.object(cli, "run_command", interrupted):
                    code, output = run_raw("--home", str(home), "search", "STOPTOPIC")
                self.assertEqual(code, cli.EXIT_INTERRUPTED)
                self.assertIn("interrupted; run it again to go on from here", output)
                self.assertEqual(len(seen), 3)
                with mock.patch.object(index_module, "parse_candidate", side_effect=AssertionError("parsed again")):
                    code, output = run_raw("--home", str(home), "search", "STOPTOPIC")
                self.assertEqual(code, 0)
                self.assertIn("Claude 3", output)


def cli_candidates(home: Path):
    from find_session.discovery import discover

    return discover(home, include_children=True)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class ProgressTests(unittest.TestCase):
    def make(self, **env: str):
        clock = Clock()
        stream = io.StringIO()
        with mock.patch.dict(os.environ, env):
            os.environ.pop("FIND_SESSION_PROGRESS", None)
            os.environ.update(env)
            progress = index_module.Progress(clock=clock, stream=stream)
        return progress, clock, stream

    def test_quick_work_prints_nothing(self):
        progress, clock, stream = self.make()
        progress.total = 3
        for position in range(1, 4):
            progress.position = position
            clock.now += 0.5
            progress.parsed_one()
        progress.done()
        self.assertEqual(stream.getvalue(), "")

    def test_long_work_says_where_it_is_then_that_it_is_ready(self):
        progress, clock, stream = self.make()
        progress.total = 400
        for position in range(1, 401):
            progress.position = position
            clock.now += 0.1  # 40 seconds in all
            progress.parsed_one()
        progress.done()
        lines = stream.getvalue().splitlines()
        self.assertRegex(
            lines[0],
            r"^find-session: indexing your history, \d+/400 session files "
            r"\(one-time; if this is interrupted, run it again: it continues\)…$",
        )
        # One line when it starts to take long, then one every PROGRESS_EVERY seconds.
        self.assertEqual(len(lines), 1 + 3 + 1, lines)
        self.assertRegex(lines[1], r"^find-session: indexing, \d+/400 session files…$")
        self.assertEqual(lines[-1], "find-session: index ready, 400 session files parsed in 40 s.")

    def test_an_interrupted_run_says_how_far_it_got_not_that_it_is_ready(self):
        progress, clock, stream = self.make()
        progress.total = 400
        for position in range(1, 151):
            progress.position = position
            clock.now += 0.1
            progress.parsed_one()
        progress.done(interrupted=True)
        lines = stream.getvalue().splitlines()
        self.assertEqual(lines[-1], "find-session: stopped after 150 of 400 session files (15 s); they are kept.")
        self.assertNotIn("index ready", stream.getvalue())

    def test_can_be_turned_off_or_given_another_delay(self):
        progress, clock, stream = self.make(FIND_SESSION_PROGRESS="off")
        clock.now = 1000
        progress.parsed_one()
        progress.done()
        self.assertEqual(stream.getvalue(), "")
        progress, clock, stream = self.make(FIND_SESSION_PROGRESS="0")
        self.assertEqual(progress.after, float("inf"))
        progress, clock, stream = self.make(FIND_SESSION_PROGRESS="0.5")
        clock.now = 0.6
        progress.parsed_one()
        self.assertIn("indexing your history", stream.getvalue())

    def test_progress_goes_to_stderr_and_stdout_stays_parseable(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            for number in range(4):
                write_jsonl(home / f".claude/projects/-x/slow-{number}.jsonl", claude_turns("SLOWTOPIC here", "ok"))
            env = clean_env(FIND_SESSION_CACHE=str(Path(tmp) / "cache"), FIND_SESSION_PROGRESS="0.000001")
            first = run_process(str(SKILL_ENTRY), "--home", str(home), "search", "SLOWTOPIC", "--json", env=env)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(json.loads(first.stdout)["counts"]["Claude"], 4)
            self.assertIn("find-session: indexing your history, ", first.stderr)
            self.assertIn("find-session: index ready, 4 session files parsed", first.stderr)
            # Nothing to parse the second time: nothing is said.
            second = run_process(str(SKILL_ENTRY), "--home", str(home), "search", "SLOWTOPIC", "--json", env=env)
            self.assertEqual(second.stderr, "")
            self.assertEqual(json.loads(second.stdout)["counts"], json.loads(first.stdout)["counts"])


class BuildIndexTests(unittest.TestCase):
    def test_build_index_parses_once_and_later_searches_parse_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            for number in range(3):
                write_jsonl(home / f".claude/projects/-x/built-{number}.jsonl", claude_turns("BUILTTOPIC", "ok"))
            cache = Path(tmp) / "cache"
            with mock.patch.dict(os.environ, {"FIND_SESSION_CACHE": str(cache)}):
                code, output = run_raw("--home", str(home), "build-index")
                self.assertEqual(code, 0)
                self.assertRegex(output, r"Index: 3 session files \(Claude 3, Codex 0, omp 0\), 3 parsed in \d+ s\n")
                self.assertEqual(len(list(cache.glob("index-*.sqlite3"))), 1)
                code, output = run_raw("--home", str(home), "build-index")
                self.assertIn("3 session files (Claude 3, Codex 0, omp 0), all up to date", output)
                with mock.patch.object(index_module, "parse_candidate", side_effect=AssertionError("parsed again")):
                    code, output = run_raw("--home", str(home), "search", "BUILTTOPIC")
                self.assertIn("Claude 3", output)

    def test_without_an_index_it_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, output = run_raw("--home", tmp, "build-index")
            self.assertEqual(code, 0)
            self.assertIn("The index is turned off", output)

    def test_a_run_that_stopped_half_way_is_continued_not_repeated(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            for number in range(6):
                write_jsonl(home / f".claude/projects/-x/half-{number}.jsonl", claude_turns("HALFTOPIC", "ok"))
            parse = index_module.parse_candidate
            calls = []

            def dies_at_the_fifth(candidate, **options):
                if len(calls) == 4:
                    # Like a command timeout: the process is gone, nothing is tidied up.
                    raise SystemExit(137)
                calls.append(candidate.location)
                return parse(candidate, **options)

            with mock.patch.dict(os.environ, {"FIND_SESSION_CACHE": str(Path(tmp) / "cache")}):
                # Written every two sessions here, every WRITE_EVERY sessions or WRITE_SECONDS in real use.
                with mock.patch.object(index_module, "WRITE_EVERY", 2):
                    with mock.patch.object(index_module, "parse_candidate", dies_at_the_fifth):
                        with self.assertRaises(SystemExit):
                            run_raw("--home", str(home), "build-index")
                    self.assertEqual(len(calls), 4)
                    with mock.patch.object(index_module, "parse_candidate", dies_at_the_fifth):
                        calls.clear()
                        calls.extend(["x"] * 5)  # never dies again
                        code, output = run_raw("--home", str(home), "build-index")
                self.assertEqual(code, 0)
                # The four sessions of the first run were kept; only the other two are parsed.
                self.assertIn("6 session files (Claude 6, Codex 0, omp 0), 2 parsed", output)

    def test_what_was_parsed_is_written_when_time_has_passed(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            for number in range(3):
                write_jsonl(home / f".claude/projects/-x/timed-{number}.jsonl", claude_turns("TIMEDTOPIC", "ok"))
            index = index_module.SessionIndex(Path(tmp) / "cache" / "index-test.sqlite3")
            clock = Clock()
            index.progress = index_module.Progress(clock=clock, stream=io.StringIO())
            index.written = clock.now
            written = []
            write = index.write
            index.write = lambda *args: written.append(len(index.unsaved)) or write(*args)
            for _ in index.entries(cli_candidates(home)):
                clock.now += index_module.WRITE_SECONDS + 0.1
            # Three sessions, far fewer than WRITE_EVERY, each slower than the time limit:
            # the first two are written when the second is parsed, the third on its own.
            self.assertEqual(written, [2, 1])
            index.save()


if __name__ == "__main__":
    unittest.main()
