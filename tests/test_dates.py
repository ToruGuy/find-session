"""A session's date comes from its records, not from the file's modification time."""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from helpers import (
    claude_turns,
    codex_answer,
    codex_meta,
    codex_user,
    omp_header,
    omp_msg,
    run_raw,
    write_jsonl,
    write_omp,
)

from find_session import timefilter

FIXED_NOW = datetime(2026, 11, 19, 6, 0, tzinfo=timezone.utc)


def copied_history(home: Path) -> None:
    """Three sessions held on 2026-11-01 (around noon UTC, so on that day in
    most time zones), in files that were all written just now (a copy)."""
    write_jsonl(
        home / ".claude/projects/-Users-alex-work/old-claude.jsonl",
        claude_turns("Invoice COPYTOPIC for October", "Sent.", start="2026-11-01T12:00:00Z"),
    )
    write_jsonl(
        home / ".codex/sessions/2026/11/01/rollout-old-codex.jsonl",
        [
            codex_meta("old-codex"),
            {**codex_user("Invoice COPYTOPIC check"), "timestamp": "2026-11-01T12:10:00Z"},
            codex_answer("Checked.", timestamp="2026-11-01T12:15:00Z"),
        ],
    )
    rows = [omp_header("old-omp"), omp_msg("u1", None, "user", "Invoice COPYTOPIC in omp")]
    for row in rows:
        row["timestamp"] = "2026-11-01T12:20:00.000Z"
    write_omp(home / ".omp/agent/sessions/-work/2026-11-01T12-00-00-000Z_old-omp.jsonl", rows, title=None)


class DatesFromRecordsTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(timefilter, "now", lambda: FIXED_NOW.astimezone())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_copied_history_keeps_its_dates(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            copied_history(home)
            code, output = run_raw("--home", tmp, "search", "COPYTOPIC", "--json")
            data = json.loads(output)
            for source in ("Claude", "Codex", "omp"):
                [row] = data["results"][source]
                self.assertTrue(row["modified"].startswith("2026-11-01T"), row)
            _, compact = run_raw("--home", tmp, "search", "COPYTOPIC")
            self.assertEqual(compact.count(" 2026-11-01 "), 3, compact)
            _, inspected = run_raw("--home", tmp, "inspect", "claude:old-claude")
            self.assertRegex(inspected.splitlines()[0], r"2026-11-01 \d\d:\d\d → 2026-11-01 \d\d:\d\d · 2 messages")

    def test_since_and_until_follow_the_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            copied_history(home)
            for arguments, expected in (
                (["--since", "2026-11-18"], 0),
                (["--since", "yesterday"], 0),
                (["--since", "2026-10-30"], 3),
                (["--until", "2026-11-10"], 3),
                (["--until", "2026-10-30"], 0),
                (["--since", "2026-10-30", "--until", "2026-11-03"], 3),
            ):
                _, output = run_raw("--home", tmp, "search", "COPYTOPIC", "--json", *arguments)
                self.assertEqual(sum(json.loads(output)["counts"].values()), expected, arguments)

    def test_recent_and_index_use_the_same_dates(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            copied_history(home)
            write_jsonl(
                home / ".claude/projects/-Users-alex-work/new.jsonl",
                claude_turns("Fresh topic", "Ok.", start=datetime.now(timezone.utc).isoformat()),
            )
            _, recent = run_raw("--home", tmp, "recent", "--hours", "24")
            self.assertIn("· new\n", recent)
            self.assertNotIn("old-claude", recent)
            self.assertNotIn("old-codex", recent)
            _, listing = run_raw("--home", tmp, "index")
            self.assertEqual(listing.count(" 2026-11-01 "), 3, listing)
            # Newest first by the sessions' own times.
            self.assertTrue(listing.splitlines()[1].startswith("claude:new "), listing)
            _, listing = run_raw("--home", tmp, "index", "--since", "2026-11-18")
            self.assertEqual(len(listing.splitlines()), 2, listing)

    def test_a_file_without_timestamps_keeps_the_time_of_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".claude/projects/-Users-alex-work/bare.jsonl"
            write_jsonl(path, [{"message": {"role": "user", "content": "BARETOPIC without any time"}}])
            moment = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc).timestamp()
            os.utime(path, (moment, moment))
            _, output = run_raw("--home", tmp, "search", "BARETOPIC", "--json")
            [row] = json.loads(output)["results"]["Claude"]
            self.assertTrue(row["modified"].startswith("2026-10-05T"), row)

    def test_an_unreadable_timestamp_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".claude/projects/-Users-alex-work/odd.jsonl"
            rows = claude_turns("ODDTOPIC here", "ok")
            rows.append({"timestamp": "not a time", "type": "note"})
            rows.append({"timestamp": 12345, "type": "note"})
            write_jsonl(path, rows)
            moment = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc).timestamp()
            os.utime(path, (moment, moment))
            code, output = run_raw("--home", tmp, "search", "ODDTOPIC", "--json")
            self.assertEqual(code, 0)
            [row] = json.loads(output)["results"]["Claude"]
            self.assertTrue(row["modified"].startswith("2026-10-05T"), row)

    def test_the_index_notices_a_changed_file_and_keeps_the_record_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            path = home / ".claude/projects/-Users-alex-work/grow.jsonl"
            rows = claude_turns("GROWTOPIC first", "ok", start="2026-11-01T12:00:00Z")
            write_jsonl(path, rows)
            with mock.patch.dict(os.environ, {"FIND_SESSION_CACHE": str(Path(tmp) / "cache")}):
                _, output = run_raw("--home", str(home), "search", "GROWTOPIC", "--json")
                [row] = json.loads(output)["results"]["Claude"]
                self.assertTrue(row["modified"].startswith("2026-11-01T"), row)
                # Touched (copied again): parsed again, same date.
                later = time.time() + 5
                os.utime(path, (later, later))
                _, output = run_raw("--home", str(home), "search", "GROWTOPIC", "--json")
                [row] = json.loads(output)["results"]["Claude"]
                self.assertTrue(row["modified"].startswith("2026-11-01T"), row)
                # A message added on another day moves the date, also when the parse is continued.
                more = claude_turns("x", "y", "GROWTOPIC second", start="2026-11-03T12:00:00Z")[2:]
                with path.open("a") as handle:
                    handle.write("".join(json.dumps(item) + "\n" for item in more))
                _, output = run_raw("--home", str(home), "search", "GROWTOPIC", "--json")
                [row] = json.loads(output)["results"]["Claude"]
                self.assertTrue(row["modified"].startswith("2026-11-03T"), row)
                self.assertEqual(row["messages"], 3)


if __name__ == "__main__":
    unittest.main()
