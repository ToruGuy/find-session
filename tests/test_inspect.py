"""inspect: selection, limits and gaps."""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shlex
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import claude_turns, codex_answer, codex_meta, codex_user, run_full, run_raw, write_jsonl

from find_session import (
    cli,
    model,
    tools,
)
from find_session.parsers import claude


class AmbiguousSelectorTests(unittest.TestCase):
    def test_codex_candidates_are_listed_by_the_id_search_prints(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            ids = ("019a1aae-0000-7000-8000-00000000000a", "019a1aae-0000-7000-8000-00000000000b")
            for number, session_id in enumerate(ids):
                write_jsonl(
                    home / f".codex/sessions/2026/11/16/rollout-2026-11-16T03-0{number}-00-{session_id}.jsonl",
                    [codex_meta(session_id), codex_user("AMBIGUOUSTOPIC"), codex_answer("ok")],
                )
            code, output = run_raw("--home", tmp, "inspect", "codex:019a1aae")
            self.assertEqual(code, 2)
            self.assertNotIn("2026-11-16T03", output)
            for session_id in ids:
                self.assertIn(f"  codex:{session_id}  (Codex, main)", output)
                self.assertEqual(run_raw("--home", tmp, "inspect", f"codex:{session_id}")[0], 0)
            code, output = run_raw("--home", tmp, "inspect", "codex:019a1aae", "--json")
            self.assertEqual(code, 2)
            self.assertEqual([row["selector"] for row in json.loads(output)["candidates"]], [f"codex:{i}" for i in ids])

    def test_a_cut_list_of_candidates_says_how_many_more_there_are(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            for number in range(57):
                write_jsonl(
                    home / f".claude/projects/-Users-alex-x/01a1{number:04d}-0000-4000-8000-000000000000.jsonl",
                    [{"message": {"role": "user", "content": "MANYTOPIC"}}],
                )
            code, output = run_raw("--home", tmp, "inspect", "01a1")
            self.assertEqual(code, 2)
            lines = output.splitlines()
            self.assertEqual(len([line for line in lines if line.startswith("  claude:")]), 50)
            self.assertEqual(lines[-1], "  … 7 more: give more of the id")
            # Nothing is added when the whole list fits.
            code, output = run_raw("--home", tmp, "inspect", "01a1000")
            self.assertEqual(code, 2)
            self.assertNotIn("more:", output)


COPY_ID = "0c9d3a2b-1111-4222-8333-444455556666"


class SessionCopiesTests(unittest.TestCase):
    """One conversation stored in two project folders (it was continued from another directory)."""

    def make_home(self, tmp: str) -> Path:
        home = Path(tmp) / "home"
        shared = ["Plan the COPYTOPIC migration", "Started.", "Go on", "Half way."]
        # The older copy stops early and ends differently; the newer one went on.
        write_jsonl(
            home / f".claude/projects/-Users-alex-old/{COPY_ID}.jsonl",
            claude_turns(*shared, "Stop here", "Stopped in the OLDFOLDER."),
        )
        write_jsonl(
            home / f".claude/projects/-Users-alex-new/{COPY_ID}.jsonl",
            claude_turns(*shared, "Finish it", "Finished.", "Thanks", "The COPYTOPIC migration is live."),
        )
        write_jsonl(
            home / ".claude/projects/-Users-alex-new/0c9d3a2b-9999-4222-8333-444455556666.jsonl",
            claude_turns("Another COPYTOPIC talk", "ok"),
        )
        return home

    def check(self, home: Path) -> None:
        code, output = run_raw("--home", str(home), "inspect", f"claude:{COPY_ID}")
        self.assertEqual(code, 0, output)
        self.assertIn("· 8 messages", output.splitlines()[0])
        self.assertIn("The COPYTOPIC migration is live.", output)
        self.assertNotIn("OLDFOLDER", output)
        [note] = [line for line in output.splitlines() if "copies of this session" in line]
        self.assertRegex(
            note, r"^2 copies of this session; showing the one written last: \S+-Users-alex-new/\S+ \(8 msgs\)"
        )
        self.assertRegex(note, r"Not shown: \S+-Users-alex-old/\S+\.jsonl \(6 msgs, to 2026-09-22\)$")
        code, output = run_raw("--home", str(home), "inspect", f"claude:{COPY_ID}", "--json")
        payload = json.loads(output)
        self.assertIn("-Users-alex-new", payload["path"])
        [other] = payload["other_copies"]
        self.assertIn("-Users-alex-old", other["path"])
        self.assertEqual(other["messages"], 6)
        # A session stored once says nothing about copies.
        code, output = run_raw("--home", str(home), "inspect", "claude:0c9d3a2b-9999")
        self.assertEqual(code, 0)
        self.assertNotIn("copies of this session", output)

    def test_inspect_opens_the_copy_written_last_and_names_the_other(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.check(self.make_home(tmp))

    def test_the_same_holds_when_served_from_the_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = self.make_home(tmp)
            with mock.patch.dict(os.environ, {"FIND_SESSION_CACHE": str(Path(tmp) / "cache")}):
                self.check(home)
                self.check(home)
                _, output = run_raw("--home", str(home), "search", "copytopic")
                self.assertIn('search "copytopic": Claude 2,', output)

    def test_the_file_time_does_not_decide_which_copy_is_the_latest(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = self.make_home(tmp)
            # A history copied from another machine: the old copy's file is the newer file.
            os.utime(home / f".claude/projects/-Users-alex-new/{COPY_ID}.jsonl", (1_000_000_000, 1_000_000_000))
            self.check(home)

    def test_search_index_and_recent_list_the_conversation_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = self.make_home(tmp)
            _, output = run_raw("--home", str(home), "search", "copytopic")
            self.assertIn('search "copytopic": Claude 2,', output)
            self.assertEqual(len(re.findall(r"^claude:", output, re.M)), 2)
            self.assertIn("· 8 msgs", output)
            self.assertNotIn("· 6 msgs", output)
            # A word only the older copy has is not found: that copy is not the conversation any more.
            _, output = run_raw("--home", str(home), "search", "oldfolder")
            self.assertIn("no matches", output)
            _, output = run_raw("--home", str(home), "index")
            self.assertEqual(len(re.findall(r"^claude:", output, re.M)), 2)
            _, output = run_raw("--home", str(home), "recent", "--hours", "1000000")
            self.assertEqual(output.count("Plan the COPYTOPIC migration"), 1)

    def test_every_choice_of_an_ambiguous_id_is_different_and_opens_a_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = self.make_home(tmp)
            code, output = run_raw("--home", str(home), "inspect", "claude:0c9d3a2b")
            self.assertEqual(code, 2)
            choices = [line.split()[0] for line in output.splitlines()[1:]]
            self.assertEqual(len(choices), 2)
            self.assertEqual(len(set(choices)), 2)
            for choice in choices:
                self.assertEqual(run_raw("--home", str(home), "inspect", choice)[0], 0, choice)
            code, output = run_raw("--home", str(home), "inspect", "claude:0c9d3a2b", "--json")
            selectors = [row["selector"] for row in json.loads(output)["candidates"]]
            self.assertEqual(sorted(selectors), sorted(choices))

    def test_a_long_list_of_choices_names_no_session_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            for number in range(51):
                for folder in ("-Users-alex-a", "-Users-alex-b"):
                    write_jsonl(
                        home / f".claude/projects/{folder}/01a1{number:04d}-0000-4000-8000-000000000000.jsonl",
                        [{"message": {"role": "user", "content": "MANYTOPIC"}}],
                    )
            code, output = run_raw("--home", tmp, "inspect", "01a1")
            self.assertEqual(code, 2)
            choices = [line.split()[0] for line in output.splitlines() if line.startswith("  claude:")]
            self.assertEqual(len(choices), len(set(choices)))
            self.assertEqual(run_raw("--home", tmp, "inspect", choices[0])[0], 0)


def tool_call(command: str) -> dict:
    return {
        "timestamp": "2026-09-22T10:00:30Z",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": command}}],
        },
    }


class LongCommandTests(unittest.TestCase):
    """A chained one-liner: the step that matters is far into the command."""

    STEPS = " && ".join(f"step{number:02d} --flag value{number}" for number in range(40))
    COMMAND = f"cd /srv/shop && {STEPS} && curl -X POST https://deploy.example.com/api/v1/go?uuid=DEEPUUID && echo done"

    def make_home(self, tmp: str, command: str | None = None) -> Path:
        home = Path(tmp)
        rows = claude_turns("Release the shop", "Releasing.")
        rows.append(tool_call(command or self.COMMAND))
        rows += claude_turns("Thanks", "Done.", start="2026-09-22T11:00:00Z")
        write_jsonl(home / ".claude/projects/-Users-alex-shop/cmd-1.jsonl", rows)
        return home

    def test_a_word_far_into_a_command_is_found_and_quoted(self):
        self.assertGreater(self.COMMAND.index("DEEPUUID"), 800)
        with tempfile.TemporaryDirectory() as tmp:
            self.make_home(tmp)
            _, output = run_raw("--home", tmp, "search", "deepuuid")
            self.assertIn('search "deepuuid": Claude 1', output)
            [hit] = [line for line in output.splitlines() if line.startswith(" hit ")]
            self.assertIn("command: …", hit)
            self.assertIn("uuid=DEEPUUID", hit)
            self.assertLess(len(hit), 135)

    def test_inspect_prints_the_start_and_counts_the_rest(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.make_home(tmp)
            _, output = run_raw("--home", tmp, "inspect", "cmd-1", "--tools")
            [line] = [line for line in output.splitlines() if line.startswith("    $ ")]
            shown, _, left = line.strip().rpartition("… [+")
            self.assertLessEqual(len(shown), model.MAX_INSPECT_COMMAND)
            self.assertTrue(self.COMMAND.startswith(shown[2:]))
            self.assertEqual(left, f"{len(self.COMMAND) + 2 - len(shown)} chars]")
            self.assertNotIn("DEEPUUID", output)
            self.assertIn("(commands are cut to 400 characters: --message-chars 0 prints them whole)", output)

    def test_message_chars_0_prints_the_whole_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.make_home(tmp)
            _, output = run_raw("--home", tmp, "inspect", "cmd-1", "--range", "2", "--tools", "--message-chars", "0")
            self.assertIn(f"    $ {self.COMMAND}\n", output)
            self.assertNotIn("chars]", output)
            self.assertNotIn("commands are cut", output)

    def test_grep_shows_the_part_of_the_command_that_has_the_word(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.make_home(tmp)
            _, output = run_raw("--home", tmp, "inspect", "cmd-1", "--grep", "deepuuid", "--tools")
            [line] = [line for line in output.splitlines() if line.startswith("    ")]
            self.assertIn("uuid=DEEPUUID", line)
            self.assertRegex(line, r"^    ….*… \[\+\d+ chars\]$")
            self.assertLess(len(line), model.MAX_INSPECT_COMMAND + 30)

    def test_what_is_not_kept_is_counted_and_never_searched(self):
        long = "run " + "word " * 600 + "LASTWORD"
        self.assertGreater(len(long), tools.MAX_TOOL_TEXT)
        with tempfile.TemporaryDirectory() as tmp:
            self.make_home(tmp, long)
            _, output = run_raw("--home", tmp, "inspect", "cmd-1", "--tools", "--message-chars", "0", "--json")
            [command] = json.loads(output)["messages"][1]["commands"]
            left = len(long) + 2 - tools.MAX_TOOL_TEXT
            self.assertTrue(command.endswith(f"… [+{left} chars not kept]"), command[-40:])
            self.assertEqual(len(command), tools.MAX_TOOL_TEXT + len(f"… [+{left} chars not kept]"))
            for query in ("lastword", "chars", "kept", str(left)):
                self.assertIn("no matches", run_raw("--home", tmp, "search", query)[1], query)

    def test_the_index_keeps_the_same_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            self.make_home(str(home), "run " + "word " * 600 + "LASTWORD")
            with mock.patch.dict(os.environ, {"FIND_SESSION_CACHE": str(Path(tmp) / "cache")}):
                first = run_raw("--home", str(home), "inspect", "cmd-1", "--tools", "--message-chars", "0")
                again = run_raw("--home", str(home), "inspect", "cmd-1", "--tools", "--message-chars", "0")
            self.assertEqual(first, again)
            self.assertIn("chars not kept]", again[1])


class InspectSelectionTests(unittest.TestCase):
    run_cli = staticmethod(run_full)

    def make_long(self, tmp: str, count: int = 30, size: int = 3000) -> Path:
        home = Path(tmp)
        rows = []
        for number in range(1, count + 1):
            role = "user" if number % 2 else "assistant"
            text = f"M{number:02d} " + ("x" * size if number not in (1, count) else "edge")
            if number == 17:
                text = "M17 the NEEDLE decision"
            stamp = f"2026-09-{22 + number // 20}T10:{number:02d}:00Z"
            content = text if role == "user" else [{"type": "text", "text": text}]
            rows.append({"timestamp": stamp, "message": {"role": role, "content": content}})
        write_jsonl(home / ".claude/projects/-Users-alex-x/long-1.jsonl", rows)
        return home

    def test_over_budget_keeps_start_and_end_and_says_what_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.make_long(tmp)
            code, output = self.run_cli("--home", tmp, "inspect", "long-1")
            self.assertEqual(code, 0)
            self.assertIn("· 30 messages", output)
            self.assertIn("] USER: M01 edge", output)
            self.assertIn("] AGENT: M30 edge", output)
            self.assertRegex(
                output,
                r"\[… \d+ message\(s\), #2 to #\d+, left out to fit --budget 20000\. "
                r"Next part: inspect claude:long-1 --range 2:\d+, then from \d+ …\]",
            )
            self.assertLessEqual(len(output), model.MAX_INSPECT_TOTAL + 2000)
            self.assertIn("more chars: --range", output)
            self.assertIn("— 2026-09-22 —", output)
            self.assertIn("— 2026-09-23 —", output)

    def test_default_output_stays_within_what_a_harness_shows_inline(self):
        self.assertLessEqual(model.MAX_INSPECT_TOTAL, 20000)
        self.assertEqual(cli.parser().parse_args(["inspect", "x"]).budget, model.MAX_INSPECT_TOTAL)
        with tempfile.TemporaryDirectory() as tmp:
            self.make_long(tmp, count=60)
            for extra in ((), ("--tools",), ("--range", "5:55"), ("--message-chars", "0")):
                _, output = self.run_cli("--home", tmp, "inspect", "long-1", *extra)
                self.assertLessEqual(len(output), model.MAX_INSPECT_TOTAL, extra)

    def test_the_next_part_is_a_call_that_fits_and_follows_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.make_long(tmp)
            read = {1}
            _, output = self.run_cli("--home", tmp, "inspect", "long-1")
            read.update(int(number) for number in re.findall(r"^\[(\d+) · ", output, re.M))
            calls = 0
            end = re.search(r"#2 to #(\d+), left out", output).group(1)
            while "Next part: " in output or calls == 0:
                call, then = re.search(r"Next part: (inspect .+?)(?:, then from (\d+))? …\]", output).groups()
                # The call as printed, pasted after the helper.
                _, part = self.run_cli("--home", tmp, *shlex.split(call))
                self.assertLessEqual(len(part), model.MAX_INSPECT_TOTAL)
                numbers = [int(number) for number in re.findall(r"^\[(\d+) · ", part, re.M)]
                first, last = (int(number) for number in call.split("--range ")[1].split()[0].split(":"))
                # Everything it asks for is printed: no part of a part.
                self.assertEqual(numbers, list(range(first, last + 1)))
                self.assertNotIn("left out", part)
                read.update(numbers)
                calls += 1
                if not then:
                    break
                _, output = self.run_cli("--home", tmp, "inspect", "long-1", "--range", f"{then}:{end}")
                read.update(int(number) for number in re.findall(r"^\[(\d+) · ", output, re.M))
            self.assertEqual(read, set(range(1, 31)))
            self.assertGreater(calls, 1)

    def test_the_next_part_repeats_the_options_of_the_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.make_long(tmp)
            _, output = self.run_cli("--home", tmp, "inspect", "long-1", "--grep", "xxxx M17", "--tools", "--json")
            payload = json.loads(output)
            [gap] = payload["omitted"]
            self.assertEqual((gap["first"], gap["next"].split(":")[0]), (3, "3"))
            self.assertGreaterEqual(gap["last"], int(gap["next"].split(":")[1]))
            _, output = self.run_cli(
                "--home", tmp, "inspect", "long-1", "--grep", "xxxx M17", "--tools", "--message-chars", "2000"
            )
            self.assertRegex(
                output, r"Next part: inspect claude:long-1 --range 3:\d+ --grep 'xxxx M17' --tools --message-chars 2000"
            )

    def test_tail_range_grep_and_unlimited(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.make_long(tmp)
            code, output = self.run_cli("--home", tmp, "inspect", "long-1", "--tail", "2", "--json")
            payload = json.loads(output)
            self.assertEqual([m["n"] for m in payload["messages"]], [29, 30])
            self.assertEqual(payload["total_messages"], 30)
            self.assertFalse(payload["truncated"])
            code, output = self.run_cli("--home", tmp, "inspect", "long-1", "--range", "16:18", "--json")
            self.assertEqual([m["n"] for m in json.loads(output)["messages"]], [16, 17, 18])
            code, output = self.run_cli("--home", tmp, "inspect", "long-1", "--grep", "needle")
            self.assertIn("USER: M17 the NEEDLE decision", output)
            self.assertNotIn("M16", output)
            code, output = self.run_cli(
                "--home", tmp, "inspect", "long-1", "--range", "2", "--message-chars", "0", "--json"
            )
            [message] = json.loads(output)["messages"]
            self.assertEqual((message["n"], len(message["text"]), message["cut"]), (2, 3004, 0))
            code, output = self.run_cli("--home", tmp, "inspect", "long-1", "--budget", "0", "--json")
            self.assertEqual(len(json.loads(output)["messages"]), 30)

    def test_repeated_user_prompt_is_kept_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            write_jsonl(
                home / ".claude/projects/-Users-alex-x/dup-1.jsonl",
                [
                    {"message": {"role": "user", "content": "close it"}},
                    {"timestamp": "2026-09-22T10:00:00Z", "message": {"role": "user", "content": "close it"}},
                ],
            )
            parsed = claude.parse_claude(home / ".claude/projects/-Users-alex-x/dup-1.jsonl")
            self.assertEqual(parsed.user_messages, ["close it"])
            self.assertEqual(parsed.times, ["2026-09-22T10:00:00Z"])

    def test_invalid_range_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.make_long(tmp, count=3)
            with self.assertRaises(SystemExit):
                with contextlib.redirect_stderr(io.StringIO()):
                    cli.main(["--home", tmp, "inspect", "long-1", "--range", "5:2"])

    def test_compaction_summary_is_labelled_and_shortened(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            write_jsonl(
                home / ".claude/projects/-Users-alex-x/sum-1.jsonl",
                [
                    {"message": {"role": "user", "content": "Begin"}},
                    {"isCompactSummary": True, "message": {"role": "user", "content": "S" * 5000}},
                ],
            )
            code, output = self.run_cli("--home", tmp, "inspect", "sum-1")
            self.assertIn("SUMMARY (compaction): " + "S" * 400 + " […4600 more chars", output)
            self.assertNotIn("USER: SSS", output)


if __name__ == "__main__":
    unittest.main()
