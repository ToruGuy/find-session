"""A session file that has grown is read from where the last parse stopped, with the same result."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import FAKE, claude_rows, claude_title, codex_answer, codex_meta, codex_tool_rows, codex_user

from find_session import cache as cache_module
from find_session import index as index_module
from find_session.cache import parse_candidate, session_record
from find_session.index import SessionIndex
from find_session.model import Candidate
from find_session.searchtext import SearchText


def lines(rows: list) -> list[bytes]:
    return [(row if isinstance(row, str) else json.dumps(row, ensure_ascii=False)).encode() + b"\n" for row in rows]


def assistant(text: str, **extra) -> dict:
    return {"timestamp": "2026-09-22T11:00:00Z", "message": {"role": "assistant", "content": text}, **extra}


def tool(tool_id: object, command: str) -> dict:
    block = {"type": "tool_use", "name": "Bash", "input": {"command": command}}
    if tool_id is not None:
        block["id"] = tool_id
    return {"message": {"role": "assistant", "content": [{"type": "text", "text": f"running {command}"}, block]}}


def codex_note(text: str, phase: str | None = None, timestamp: object = "2026-09-22T10:00:03Z") -> dict:
    payload = {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}
    if phase:
        payload["phase"] = phase
    return {"timestamp": timestamp, "type": "response_item", "payload": payload}


def codex_call(call_id: object, command: str) -> dict:
    payload = {"type": "function_call", "name": "exec_command", "arguments": json.dumps({"cmd": command})}
    if call_id is not None:
        payload["call_id"] = call_id
    return {"type": "response_item", "payload": payload}


CLAUDE_ROWS = [
    {"message": {"role": "user", "content": "Start without a working directory"}},
    claude_title("ai", "Generated name, first version"),
    *claude_rows(),
    tool("toolu_1", "REPEATED-CALL the same id again"),
    claude_title("ai", "Generated name, second version"),
    tool("toolu_2", "second-command --flag"),
    "this line is not JSON",
    "[1, 2, 3]",
    claude_title("custom", f"Name given by the user, token={FAKE['flag']}"),
    {"timestamp": "2026-09-22T10:20:00Z", "message": {"role": "user", "content": "Queued prompt"}},
    # A generated title written after the user's own must not replace it.
    claude_title("ai", "Generated name, third version"),
    {"timestamp": "2026-09-22T10:20:01Z", "message": {"role": "user", "content": "Queued prompt"}},
    claude_title("custom", "  "),
    {"type": "custom-title", "customTitle": 7},
    {
        "isCompactSummary": True,
        "message": {"role": "user", "content": "This session is being continued from a previous conversation: recap"},
    },
    {"cwd": "/Users/alex/elsewhere", "message": {"role": "user", "content": "Zażółć gęślą jaźń after the summary"}},
    tool("toolu_2", "second-command --flag"),
    tool("toolu_3", "third-command"),
    assistant("Done with everything"),
]

CODEX_ROWS = [
    *codex_tool_rows()[:-1],
    codex_note("Working on it"),
    codex_call("c1", "REPEATED-CALL with a known id"),
    codex_note("Still working", timestamp=12345),
    codex_meta("copied-parent-meta", cwd="/Users/alex/other"),
    codex_user("Second request, zażółć"),
    codex_call("c9", "ninth-command"),
    codex_answer("Released."),
    codex_note("An afterthought"),
    codex_user("Third request"),
]

CODEX_CHILD_ROWS = [
    codex_meta("codex-child", source={"subagent": {"thread_spawn": {"parent_thread_id": "codex-parent"}}}),
    codex_note("Thinking before any prompt"),
    codex_user("Delegated task"),
    codex_note("First draft"),
    codex_note("Second draft"),
    codex_call("k1", "child-command"),
]


class ResumeCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.index_path = self.root / "cache" / "index.sqlite3"
        self.resumed = 0

    def candidate(self, source: str) -> Candidate:
        if source == "Claude":
            path = self.root / ".claude/projects/-Users-alex-x/grow-1.jsonl"
            session_id = "grow-1"
        else:
            path = self.root / ".codex/sessions/2026/09/22/rollout-grow-2.jsonl"
            session_id = "grow-2"
        path.parent.mkdir(parents=True, exist_ok=True)
        return Candidate(source, path, session_id)

    def indexed(self, candidate: Candidate):
        """What a run with the index sees for the file as it is now (and stores)."""
        real = cache_module.resumable

        def counting(path, record, checkpoint):
            session = real(path, record, checkpoint)
            self.resumed += session is not None
            return session

        index = SessionIndex(self.index_path)
        with mock.patch.object(index_module, "resumable", counting):
            session = index.entry(candidate).session()
            index.save()
        return session

    def assert_same_as_full_parse(self, candidate: Candidate, label: object) -> None:
        got = self.indexed(candidate)
        expected = parse_candidate(candidate)
        self.assertEqual(session_record(got), session_record(expected), label)
        self.assertEqual(got.checkpoint, expected.checkpoint, label)
        self.assertEqual(
            (got.user_messages, got.assistant_messages, got.delegated_messages, got.summaries),
            (expected.user_messages, expected.assistant_messages, expected.delegated_messages, expected.summaries),
            label,
        )
        self.assertEqual(SearchText.of(got).low, SearchText.of(expected).low, label)
        # And what was stored is what a later run reads back.
        stored = SessionIndex(self.index_path)
        with mock.patch.object(index_module, "parse_candidate", side_effect=AssertionError("parsed again")):
            self.assertEqual(session_record(stored.entry(candidate).session()), session_record(expected), label)
        stored.save()


class ResumeTests(ResumeCase):
    def check_every_split(self, source: str, rows: list) -> None:
        candidate = self.candidate(source)
        data = lines(rows)
        for split in range(len(data) + 1):
            if self.index_path.exists():
                self.index_path.unlink()
            candidate.path.write_bytes(b"".join(data[:split]))
            self.assert_same_as_full_parse(candidate, (source, "first part", split))
            before = self.resumed
            candidate.path.write_bytes(b"".join(data))
            self.assert_same_as_full_parse(candidate, (source, "whole file", split))
            self.assertEqual(self.resumed, before + 1, (source, split))

    def test_claude_file_split_at_every_line(self):
        self.check_every_split("Claude", CLAUDE_ROWS)

    def test_codex_file_split_at_every_line(self):
        self.check_every_split("Codex", CODEX_ROWS)

    def test_codex_subagent_file_split_at_every_line(self):
        self.check_every_split("Codex", CODEX_CHILD_ROWS)

    def test_file_growing_line_by_line_and_mid_line(self):
        for source, rows in (("Claude", CLAUDE_ROWS), ("Codex", CODEX_ROWS), ("Codex", CODEX_CHILD_ROWS)):
            candidate = self.candidate(source)
            if self.index_path.exists():
                self.index_path.unlink()
            data = b"".join(lines(rows))
            # Every 37 bytes: most cuts fall inside a line, some inside a character.
            for cut in [*range(0, len(data), 37), len(data)]:
                candidate.path.write_bytes(data[:cut])
                self.assert_same_as_full_parse(candidate, (source, "bytes", cut))
            self.assertGreater(self.resumed, len(data) // 37 // 2)

    def test_last_line_without_newline_that_parses_is_not_counted_twice(self):
        candidate = self.candidate("Claude")
        data = lines(CLAUDE_ROWS)
        candidate.path.write_bytes(b"".join(data[:4]) + data[4].rstrip(b"\n"))
        self.assert_same_as_full_parse(candidate, "complete record, newline still missing")
        candidate.path.write_bytes(b"".join(data))
        self.assert_same_as_full_parse(candidate, "newline and more arrived")

    def test_changed_or_shortened_file_is_read_from_the_start(self):
        for source, rows in (("Claude", CLAUDE_ROWS), ("Codex", CODEX_ROWS)):
            candidate = self.candidate(source)
            if self.index_path.exists():
                self.index_path.unlink()
            data = lines(rows)
            candidate.path.write_bytes(b"".join(data[:6]))
            self.assert_same_as_full_parse(candidate, "built")
            before = self.resumed
            # Same length, one earlier byte different, then more lines.
            changed = bytearray(b"".join(data))
            position = changed.index(b"e", 40)
            changed[position : position + 1] = b"E"
            candidate.path.write_bytes(bytes(changed))
            self.assert_same_as_full_parse(candidate, "rewritten")
            candidate.path.write_bytes(b"".join(data[:3]))
            self.assert_same_as_full_parse(candidate, "shortened")
            candidate.path.write_bytes(b"".join(data[1:]))
            self.assert_same_as_full_parse(candidate, "start removed")
            self.assertEqual(self.resumed, before, source)

    def test_only_the_new_part_of_the_file_is_read(self):
        candidate = self.candidate("Claude")
        data = lines(CLAUDE_ROWS)
        candidate.path.write_bytes(b"".join(data[:-2]))
        self.indexed(candidate)
        candidate.path.write_bytes(b"".join(data))
        parsed: list[str] = []
        real = json.loads

        def counting(text, *args, **kwargs):
            parsed.append(text)
            return real(text, *args, **kwargs)

        with mock.patch("json.loads", counting):
            self.indexed(candidate)
        self.assertEqual(len(parsed), 2)

    def test_title_written_late_is_picked_up_without_reading_the_file_again(self):
        candidate = self.candidate("Claude")
        rows = [row for row in CLAUDE_ROWS if not (isinstance(row, dict) and "-title" in row.get("type", ""))]
        data = lines(rows)
        candidate.path.write_bytes(b"".join(data))
        self.assertEqual(self.indexed(candidate).title, "")
        for added, expected in (
            (claude_title("ai", "Generated late"), "Generated late"),
            (claude_title("custom", "Chosen late"), "Chosen late"),
            (claude_title("ai", "Generated again"), "Chosen late"),
            (claude_title("custom", "Chosen again"), "Chosen again"),
        ):
            data += lines([added])
            candidate.path.write_bytes(b"".join(data))
            before = self.resumed
            self.assert_same_as_full_parse(candidate, expected)
            self.assertEqual(self.resumed, before + 1, expected)
            self.assertEqual(self.indexed(candidate).title, expected)

    def test_title_in_the_checkpoint_is_stored_redacted(self):
        candidate = self.candidate("Claude")
        candidate.path.write_bytes(b"".join(lines(CLAUDE_ROWS)))
        session = self.indexed(candidate)
        self.assertEqual(session.title, "Name given by the user, token=[REDACTED]")
        self.assertEqual(session.checkpoint[2]["custom"], session.title)
        self.assertEqual(session.checkpoint[2]["ai"], "Generated name, third version")
        for path in self.index_path.parent.iterdir():
            self.assertNotIn(FAKE["flag"].encode(), path.read_bytes(), path.name)

    def test_touched_file_keeps_its_content_and_the_time_of_its_records(self):
        candidate = self.candidate("Codex")
        candidate.path.write_bytes(b"".join(lines(CODEX_ROWS)))
        first = self.indexed(candidate)
        stat = candidate.path.stat()
        os.utime(candidate.path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
        self.assert_same_as_full_parse(candidate, "touched")
        self.assertEqual(self.resumed, 1)
        # The session is dated by its last record, which a touch does not change.
        self.assertTrue(first.last_stamp)
        self.assertEqual(self.indexed(candidate).mtime, first.mtime)
        self.assertNotEqual(first.mtime, candidate.path.stat().st_mtime)

    def test_call_without_an_id_does_not_stop_resuming(self):
        candidate = self.candidate("Claude")
        rows = [*CLAUDE_ROWS[:3], tool(None, "no-id-command"), tool(None, "no-id-command"), *CLAUDE_ROWS[3:]]
        data = lines(rows)
        candidate.path.write_bytes(b"".join(data[:5]))
        self.assert_same_as_full_parse(candidate, "first part")
        candidate.path.write_bytes(b"".join(data))
        self.assert_same_as_full_parse(candidate, "whole file")
        self.assertEqual(self.resumed, 1)

    def test_unusual_call_id_falls_back_to_reading_everything(self):
        candidate = self.candidate("Claude")
        data = lines([*CLAUDE_ROWS[:3], tool(77, "numeric-id-command"), *CLAUDE_ROWS[3:]])
        candidate.path.write_bytes(b"".join(data[:5]))
        self.assert_same_as_full_parse(candidate, "first part")
        candidate.path.write_bytes(b"".join(data))
        self.assert_same_as_full_parse(candidate, "whole file")
        self.assertEqual(self.resumed, 0)


if __name__ == "__main__":
    unittest.main()
