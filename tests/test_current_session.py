"""The session a search is run from is left out of the ranking and gets one line instead."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import claude_turns, codex_answer, codex_meta, codex_user, run_raw, write_jsonl

from find_session import index as index_module
from find_session.search import calling_session

CALLER = "0b7c2f1e-aaaa-4000-8000-000000000001"
OTHER = "0b7c2f1e-bbbb-4000-8000-000000000002"
THREAD = "0199aaaa-0000-7000-8000-00000000000a"
QUERY = "CALLERTOPIC rollout"


def claude_env(session_id: str = CALLER) -> dict:
    return {"CLAUDE_CODE_SESSION_ID": session_id}


class CurrentSessionCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "history"
        self.cache_dir = Path(tmp.name) / "cache"
        claude = self.home / ".claude/projects/-Users-alex-work"
        # The caller talks about the topic more than the session that is looked for.
        write_jsonl(
            claude / f"{CALLER}.jsonl",
            claude_turns(
                "Find where we planned the CALLERTOPIC rollout",
                "Searching for the CALLERTOPIC rollout",
                "CALLERTOPIC rollout again",
                "Still on the CALLERTOPIC rollout",
            ),
        )
        write_jsonl(claude / f"{OTHER}.jsonl", claude_turns("Plan the CALLERTOPIC rollout", "Planned."))
        codex = self.home / ".codex/sessions/2026/09/22"
        write_jsonl(
            codex / "rollout-2026-09-22T10-00-00-file-name-differs.jsonl",
            [codex_meta(THREAD), codex_user("Review the CALLERTOPIC rollout"), codex_answer("Reviewed.")],
        )
        # The same id as the Claude caller, in another harness: a different session.
        write_jsonl(
            codex / f"rollout-{CALLER}.jsonl",
            [codex_meta(CALLER), codex_user("Unrelated CALLERTOPIC rollout notes"), codex_answer("Noted.")],
        )

    def run_search(self, *extra: str, env: dict | None = None, leading: tuple = (), query: str = QUERY) -> str:
        with mock.patch.dict(os.environ, env or {}):
            code, output = run_raw("--home", str(self.home), *leading, "search", query, *extra)
        self.assertEqual(code, 0, output)
        return output

    def with_index(self):
        patcher = mock.patch.dict(os.environ, {"FIND_SESSION_CACHE": str(self.cache_dir)})
        patcher.start()
        self.addCleanup(patcher.stop)


class CallerDetectionTests(unittest.TestCase):
    def detect(self, **variables: str):
        with mock.patch.dict(os.environ, variables):
            return calling_session()

    def test_one_variable_per_harness(self):
        self.assertIsNone(self.detect())
        self.assertEqual(self.detect(CLAUDE_CODE_SESSION_ID=CALLER), ("Claude", CALLER))
        self.assertEqual(self.detect(CODEX_THREAD_ID=THREAD), ("Codex", THREAD))
        self.assertEqual(self.detect(CODEX_SESSION_ID=THREAD), ("Codex", THREAD))
        self.assertEqual(self.detect(CODEX_THREAD_ID=THREAD, CODEX_SESSION_ID="older-name"), ("Codex", THREAD))
        self.assertEqual(self.detect(CLAUDE_CODE_SESSION_ID=f" {CALLER}\n"), ("Claude", CALLER))
        self.assertIsNone(self.detect(CLAUDE_CODE_SESSION_ID=" "))

    def test_two_harnesses_at_once_name_no_caller(self):
        # One harness was started from inside the other: the outer id is a different session.
        self.assertIsNone(self.detect(CLAUDE_CODE_SESSION_ID=CALLER, CODEX_THREAD_ID=THREAD))
        self.assertIsNone(self.detect(CLAUDE_CODE_SESSION_ID=CALLER, CODEX_SESSION_ID=THREAD))


class CurrentSessionTests(CurrentSessionCase):
    def test_without_a_caller_nothing_changes(self):
        plain = self.run_search()
        self.assertIn('search "CALLERTOPIC rollout": Claude 2, Codex 2, omp 0\n', plain)
        self.assertNotIn("this session", plain)
        # The caller ranks first: that is the problem.
        self.assertLess(plain.index("claude:0b7c2f1e-a "), plain.index("claude:0b7c2f1e-b "))
        for extra in ((), ("--full",), ("--json",)):
            expected = self.run_search(*extra)
            # An id that is no session here, an id for two harnesses, and the flag: all as before.
            self.assertEqual(self.run_search(*extra, env=claude_env("no-such-session")), expected)
            self.assertEqual(self.run_search(*extra, env={**claude_env(), "CODEX_THREAD_ID": THREAD}), expected)
            self.assertEqual(self.run_search(*extra, "--include-current", env=claude_env()), expected)
            self.assertEqual(self.run_search(*extra, "--include-current"), expected)

    def test_caller_is_left_out_and_gets_one_line(self):
        output = self.run_search(env=claude_env())
        lines = output.splitlines()
        self.assertEqual(lines[0], 'search "CALLERTOPIC rollout": Claude 1, Codex 2, omp 0')
        self.assertNotIn("claude:0b7c2f1e-a 20", output)
        self.assertIn("claude:0b7c2f1e-b ", output)
        # The Codex session with the same id is another session and stays.
        self.assertIn(f"codex:{CALLER[:8]} ", output)
        self.assertEqual(lines[-2:], ["", "this session also matches: claude:0b7c2f1e-a hit 1:4"])
        self.assertEqual(output.count("this session"), 1)
        # The line is short, and what it replaces is a whole row.
        self.assertLessEqual(len(lines[-1]), 55)
        self.assertLess(len(output), len(self.run_search()))
        # The selector on the line works.
        code, transcript = run_raw("--home", str(self.home), "inspect", "claude:0b7c2f1e-a", "--range", "1:4")
        self.assertEqual(code, 0)
        self.assertIn("Find where we planned the CALLERTOPIC rollout", transcript)

    def test_limit_counts_other_sessions_only(self):
        output = self.run_search("--limit", "1", "--source", "claude", env=claude_env())
        self.assertIn("claude:0b7c2f1e-b ", output)
        self.assertNotIn("top 1 of", output)

    def test_only_the_caller_matches(self):
        output = self.run_search(env=claude_env(), query="searching")
        self.assertEqual(
            output,
            'search "searching": no matches (Claude 0, Codex 0, omp 0)\n'
            "this session also matches: claude:0b7c2f1e-a hit 1:3\n",
        )

    def test_filters_that_exclude_the_caller_leave_no_line(self):
        self.assertNotIn("this session", self.run_search("--source", "codex", env=claude_env()))
        self.assertNotIn("this session", self.run_search("--project", "elsewhere", env=claude_env()))
        self.assertNotIn("this session", self.run_search(env=claude_env(), query="nothing-like-this"))

    def test_full_output(self):
        output = self.run_search("--full", env=claude_env())
        self.assertIn("(Claude 1, Codex 2, omp 0)", output)
        self.assertNotIn(f"Session: {CALLER}\nStart: Find where", output)
        self.assertIn(f"Session: {OTHER}", output)
        self.assertEqual(
            output.splitlines()[-2:],
            ["", f"Not listed: this session also matches: claude:{CALLER} hit 1:4 (--include-current lists it)"],
        )

    def test_json_output(self):
        plain = json.loads(self.run_search("--json"))
        self.assertIsNone(plain["current_session"])
        [first, second] = plain["results"]["Claude"]
        self.assertEqual(first["session_id"], CALLER)
        payload = json.loads(self.run_search("--json", env=claude_env()))
        self.assertEqual(payload["counts"], {"Claude": 1, "Codex": 2, "omp": 0})
        self.assertEqual(payload["results"]["Claude"], [second])
        self.assertEqual(payload["results"]["Codex"], plain["results"]["Codex"])
        # The same record it had in the ranking, under a key of its own.
        self.assertEqual(payload["current_session"], first)
        self.assertEqual(payload["current_session"]["chunk"], {"first": 1, "last": 4})
        included = json.loads(self.run_search("--json", "--include-current", env=claude_env()))
        self.assertEqual(included, plain)

    def test_codex_caller_is_matched_by_the_id_inside_the_file(self):
        for name in ("CODEX_THREAD_ID", "CODEX_SESSION_ID"):
            output = self.run_search(env={name: THREAD})
            self.assertEqual(output.splitlines()[0], 'search "CALLERTOPIC rollout": Claude 2, Codex 1, omp 0', name)
            self.assertEqual(output.splitlines()[-1], f"this session also matches: codex:{THREAD[:8]} hit 1:2", name)
            # The Claude session that would be the caller under Claude Code is listed.
            self.assertIn("claude:0b7c2f1e-a ", output)
        # A Codex id never hides a Claude session, nor the other way round.
        output = self.run_search(env={"CODEX_THREAD_ID": CALLER})
        self.assertIn("claude:0b7c2f1e-a ", output)
        self.assertEqual(output.splitlines()[-1], f"this session also matches: codex:{CALLER[:8]} hit 1:2")

    def test_codex_subagent_as_caller(self):
        child = codex_meta("child-thread", source={"subagent": {"thread_spawn": {"parent_thread_id": THREAD}}})
        write_jsonl(
            self.home / ".codex/sessions/2026/09/22/rollout-child.jsonl",
            [child, codex_user("Look up the CALLERTOPIC rollout"), codex_answer("CALLERTOPIC rollout found")],
        )
        self.assertIn("; 1 more in subagents (--children)", self.run_search())
        # Its own session is neither counted among the hidden ones nor pointed at; its parent stays listed.
        output = self.run_search(env={"CODEX_THREAD_ID": "child-thread"})
        self.assertEqual(output.splitlines()[0], 'search "CALLERTOPIC rollout": Claude 2, Codex 2, omp 0')
        self.assertNotIn("this session", output)
        self.assertIn(f"codex:{THREAD[:8]} ", output)
        output = self.run_search("--children", env={"CODEX_THREAD_ID": "child-thread"})
        self.assertIn("Claude 2, Codex 2, omp 0, subagents 0", output)
        self.assertEqual(output.splitlines()[-1], "this session also matches: codex:child-th hit 1:2")

    def test_same_with_the_index_and_no_conversation_is_read_for_the_line(self):
        expected = {
            extra: self.run_search(*extra, env=claude_env(), leading=("--no-cache",))
            for extra in ((), ("--full",), ("--json",), ("--include-current",))
        }
        self.with_index()
        for extra, output in expected.items():
            self.assertEqual(self.run_search(*extra, env=claude_env()), output, extra)
        loaded: list[tuple[str, str]] = []
        real = index_module.SessionIndex.load_session

        def recording(index, entry):
            loaded.append((entry.source, entry.session_id))
            return real(index, entry)

        with mock.patch.object(index_module.SessionIndex, "load_session", recording):
            with mock.patch.object(index_module, "parse_candidate", side_effect=AssertionError("parsed")):
                output = self.run_search(env=claude_env())
        self.assertEqual(output, expected[()])
        self.assertIn(("Claude", OTHER), loaded)
        self.assertNotIn(("Claude", CALLER), loaded)


if __name__ == "__main__":
    unittest.main()
