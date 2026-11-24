"""Claude Code subagent transcripts: found, kept apart from user conversations, and readable."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import claude_turns, run_raw, write_jsonl

from find_session import discovery
from find_session.cache import parse_candidate

PROJECT = ".claude/projects/-Users-alex-work"
PARENT = "6513270e-1111-4222-8333-444455556666"
AGENT = "a1b2c3d4e5f607182"
OTHER_AGENT = "b9c8d7e6f5a403921"


def agent_rows(agent: str, task: str, report: str) -> list[dict]:
    """A subagent's file as Claude Code writes it: the task, its own work, the report."""
    common = {"isSidechain": True, "agentId": agent, "sessionId": PARENT, "cwd": "/Users/alex/work"}
    return [
        {**common, "type": "user", "timestamp": "2026-09-22T10:05:00Z", "message": {"role": "user", "content": task}},
        # Copies of what was sent to the model: never part of the conversation.
        {"type": "api-request-blob", "hash": "h1", "message": {"role": "user", "content": "BLOBWORD instructions"}},
        {"type": "api-request", "id": "r1", "timestamp": "2026-09-22T10:05:01Z", "params": {}},
        {
            **common,
            "type": "assistant",
            "timestamp": "2026-09-22T10:05:10Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Looking at the AXOLOTLINNER module first."},
                    {
                        "type": "tool_use",
                        "id": "toolu_a",
                        "name": "Bash",
                        "input": {"command": "grep -rn NUMBATCMD src"},
                    },
                ],
            },
        },
        {
            **common,
            "type": "user",
            "timestamp": "2026-09-22T10:05:20Z",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "toolu_a", "content": "TOOLOUTPUTWORD"}],
            },
        },
        {
            **common,
            "type": "assistant",
            "timestamp": "2026-09-22T10:06:00Z",
            "message": {"role": "assistant", "content": [{"type": "text", "text": report}]},
        },
    ]


def make_home(tmp: str) -> Path:
    home = Path(tmp)
    rows = claude_turns("Investigate the FLAMINGO outage", "I will ask a subagent.", "Thanks", "Done.")
    rows.append({"type": "api-request-blob", "hash": "h0", "message": {"role": "assistant", "content": "BLOBWORD"}})
    write_jsonl(home / PROJECT / f"{PARENT}.jsonl", rows)
    write_jsonl(
        home / PROJECT / PARENT / "subagents" / f"agent-{AGENT}.jsonl",
        agent_rows(AGENT, "Find where the PANGOLIN outage starts", "Report: the QUOKKAREPORT cause is in ledger.py"),
    )
    (home / PROJECT / PARENT / "subagents" / f"agent-{AGENT}.meta.json").write_text('{"agentType": "Explore"}')
    # An agent started by an agent lies one folder deeper.
    write_jsonl(
        home / PROJECT / PARENT / "subagents" / "nested" / f"agent-{OTHER_AGENT}.jsonl",
        agent_rows(OTHER_AGENT, "Check the PANGOLIN logs", "Nothing in the logs."),
    )
    return home


class ClaudeSubagentTests(unittest.TestCase):
    def test_discovery_lists_subagent_files_only_when_asked(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = make_home(tmp)
            mains = discovery.discover(home)
            self.assertEqual([(c.session_id, c.kind) for c in mains], [(PARENT, "main")])
            found = discovery.discover(home, include_children=True)
            self.assertEqual(
                [(c.session_id, c.kind, c.parent_id) for c in found],
                [(PARENT, "main", ""), (AGENT, "child", PARENT), (OTHER_AGENT, "child", PARENT)],
            )
            # The listing with os.scandir and the one with pathlib are the same list.
            root = home / ".claude/projects"
            self.assertEqual(discovery.claude_files(root, True), discovery.glob_files(root, home / "none", True))
            self.assertEqual(discovery.claude_files(root), discovery.glob_files(root, home / "none"))

    def test_a_subagent_is_parsed_as_a_delegated_task_and_its_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = make_home(tmp)
            child = discovery.discover(home, include_children=True)[1]
            session = parse_candidate(child)
            self.assertEqual((session.source, session.kind, session.parent_id), ("Claude", "child", PARENT))
            self.assertEqual(session.session_id, AGENT)
            self.assertEqual(session.project, "~/work")
            self.assertEqual(
                session.turns,
                [
                    ("delegated", "Find where the PANGOLIN outage starts"),
                    ("assistant", "Looking at the AXOLOTLINNER module first."),
                    ("assistant", "Report: the QUOKKAREPORT cause is in ledger.py"),
                ],
            )
            self.assertEqual(session.user_messages, [])
            self.assertEqual([text for _, text in session.tools], ["$ grep -rn NUMBATCMD src"])

    def test_search_keeps_subagents_apart_and_children_shows_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_home(tmp)
            _, output = run_raw("--home", tmp, "search", "FLAMINGO")
            self.assertIn("Claude 1, ", output.splitlines()[0])
            self.assertNotIn("subagent", output)
            # What only a subagent said is not lost silently.
            for word, subagents in (("QUOKKAREPORT", 1), ("AXOLOTLINNER", 2), ("NUMBATCMD", 2)):
                _, output = run_raw("--home", tmp, "search", word)
                self.assertEqual(
                    output.splitlines()[0],
                    f'search "{word}": no matches (Claude 0, Codex 0, omp 0, agy 0); '
                    f"{subagents} more in subagents (--children)",
                )
            _, output = run_raw("--home", tmp, "search", "PANGOLIN", "--children")
            self.assertIn("subagents 2", output.splitlines()[0])
            self.assertIn(f"claude:{AGENT[:8]} 2026-09-22 ", output)
            self.assertIn(
                f"~/work · 3 msgs · subagent of {PARENT[:8]}\n task: Find where the PANGOLIN outage starts", output
            )
            # --source claude has a subagent section too.
            _, output = run_raw("--home", tmp, "search", "QUOKKAREPORT", "--children", "--source", "claude")
            self.assertIn(" hit 2:3 agent: Report: the QUOKKAREPORT cause is in ledger.py", output)
            for word in ("BLOBWORD", "TOOLOUTPUTWORD"):
                _, output = run_raw("--home", tmp, "search", word, "--children")
                self.assertIn("no matches", output, word)

    def test_inspect_reads_a_subagent_and_the_parent_names_its_subagents(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_home(tmp)
            code, output = run_raw("--home", tmp, "inspect", f"claude:{AGENT[:8]}", "--tools")
            self.assertEqual(code, 0, output)
            self.assertIn(f"# Claude · Claude subagent of session {PARENT} · ~/work · {AGENT} · ", output)
            self.assertIn("[1 · ", output)
            self.assertIn("DELEGATED TASK: Find where the PANGOLIN outage starts", output)
            self.assertIn("$ grep -rn NUMBATCMD src", output)
            self.assertIn("AGENT: Report: the QUOKKAREPORT cause is in ledger.py", output)
            self.assertNotIn("claude --resume", output)
            # The short id of the parent still names the parent alone.
            code, output = run_raw("--home", tmp, "inspect", f"claude:{PARENT[:8]}")
            self.assertEqual(code, 0, output)
            self.assertIn(
                f"Subagents of this session (inspect separately):\n  claude:{AGENT}\n  claude:{OTHER_AGENT}\n", output
            )
            self.assertIn(f"claude --resume {PARENT}", output)
            _, output = run_raw("--home", tmp, "inspect", f"claude:{PARENT[:8]}", "--json")
            self.assertEqual(json.loads(output)["children"], [f"claude:{AGENT}", f"claude:{OTHER_AGENT}"])
            self.assertEqual(json.loads(output)["total_messages"], 4)

    def test_a_long_list_of_subagents_is_cut(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = make_home(tmp)
            for number in range(30):
                agent = f"c{number:016d}"
                write_jsonl(
                    home / PROJECT / PARENT / "subagents" / f"agent-{agent}.jsonl", agent_rows(agent, "Task", "Done")
                )
            _, output = run_raw("--home", tmp, "inspect", f"claude:{PARENT[:8]}")
            listed = output.split("Subagents of this session (inspect separately):\n")[1].splitlines()
            self.assertEqual(len([line for line in listed if line.startswith("  claude:")]), 20)
            self.assertIn("  … 12 more (--json lists them all)", listed)

    def test_a_copied_subagent_file_is_one_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = make_home(tmp)
            # The session was forked into another project folder, subagent files and all.
            fork = "7624381f-1111-4222-8333-444455556666"
            other = home / ".claude/projects/-Users-alex-work-fork"
            write_jsonl(other / f"{fork}.jsonl", claude_turns("Investigate the FLAMINGO outage, again", "ok"))
            rows = agent_rows(AGENT, "Find where the PANGOLIN outage starts", "Report: the QUOKKAREPORT cause")
            write_jsonl(other / fork / "subagents" / f"agent-{AGENT}.jsonl", rows)
            children = [c for c in discovery.discover(home, include_children=True) if c.kind == "child"]
            self.assertEqual(sorted(c.session_id for c in children), sorted([AGENT, OTHER_AGENT]))
            code, output = run_raw("--home", tmp, "inspect", f"claude:{AGENT}")
            self.assertEqual(code, 0, output)
            # The copy that went on (the larger file) is the one that is read.
            rows.append(
                {
                    "type": "assistant",
                    "timestamp": "2026-09-22T11:00:00Z",
                    "message": {"role": "assistant", "content": [{"type": "text", "text": "One more ECHIDNAWORD."}]},
                }
            )
            write_jsonl(other / fork / "subagents" / f"agent-{AGENT}.jsonl", rows)
            [kept] = [c for c in discovery.discover(home, include_children=True) if c.session_id == AGENT]
            self.assertEqual(kept.parent_id, fork)
            _, output = run_raw("--home", tmp, "search", "ECHIDNAWORD", "--children")
            self.assertIn("subagents 1", output.splitlines()[0])

    def test_the_index_serves_subagents_like_a_fresh_parse(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            make_home(str(home))
            _, fresh = run_raw("--home", str(home), "search", "PANGOLIN QUOKKAREPORT", "--children", "--json")
            with mock.patch.dict(os.environ, {"FIND_SESSION_CACHE": str(Path(tmp) / "cache")}):
                run_raw("--home", str(home), "build-index")
                _, indexed = run_raw("--home", str(home), "search", "PANGOLIN QUOKKAREPORT", "--children", "--json")
                _, built = run_raw("--home", str(home), "build-index")
            self.assertEqual(json.loads(indexed), json.loads(fresh))
            self.assertIn("Index: 3 session files (Claude 3, Codex 0, omp 0, agy 0), all up to date", built)

    def test_request_copies_are_dropped_however_they_are_written(self):
        def message(role: str, text: str, minute: int) -> str:
            row = {"type": role, "timestamp": f"2026-09-22T10:{minute:02d}:00Z", "message": {"role": role}}
            row["message"]["content"] = text if role == "user" else [{"type": "text", "text": text}]
            return json.dumps(row, separators=(",", ":"))

        lines = [
            message("user", "RAWTOPIC question", 0),
            # As Claude Code writes them: recognised before the line is decoded.
            '{"type":"api-request-blob","hash":"h1","message":{"role":"user","content":"BLOBCOMPACT"}}',
            '{"type":"api-request","id":"r1","timestamp":"2027-01-01T00:00:00Z","params":{}}',
            # Keys in another order, or spaced: decoded, then dropped by their type.
            '{"hash":"h2","message":{"role":"user","content":"BLOBORDER"},"type":"api-request-blob"}',
            '{"id":"r2","timestamp":"2027-02-02T00:00:00Z","cwd":"/Users/alex/elsewhere","type":"api-request"}',
            '{"type": "api-request-blob", "message": {"role": "assistant", "content": "BLOBSPACED"}}',
            message("assistant", "RAWTOPIC answer", 1),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            path = home / PROJECT / "raw.jsonl"
            path.parent.mkdir(parents=True)
            path.write_text("\n".join(lines) + "\n")
            for word in ("BLOBCOMPACT", "BLOBORDER", "BLOBSPACED"):
                self.assertIn("no matches", run_raw("--home", str(home), "search", word)[1], word)
            _, fresh = run_raw("--home", str(home), "inspect", "claude:raw", "--json")
            self.assertEqual(json.loads(fresh)["total_messages"], 2)
            # A request record does not date the session.
            self.assertTrue(json.loads(fresh)["modified"].startswith("2026-09-22T"), fresh)
            self.assertNotEqual(json.loads(fresh)["cwd"], "/Users/alex/elsewhere")
            with mock.patch.dict(os.environ, {"FIND_SESSION_CACHE": str(Path(tmp) / "cache")}):
                run_raw("--home", str(home), "search", "RAWTOPIC")
                # The file grows by a request copy and a message: the parse is continued, with the same result.
                more = [lines[1], message("user", "RAWTOPIC follow-up", 2)]
                with path.open("a") as handle:
                    handle.write("\n".join(more) + "\n")
                _, indexed = run_raw("--home", str(home), "inspect", "claude:raw", "--json")
            _, fresh = run_raw("--home", str(home), "inspect", "claude:raw", "--json")
            self.assertEqual(json.loads(indexed), json.loads(fresh))
            self.assertEqual(json.loads(fresh)["total_messages"], 3)

    def test_request_copies_in_a_main_session_are_not_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_home(tmp)
            _, output = run_raw("--home", tmp, "search", "BLOBWORD")
            self.assertIn("no matches", output)
            _, output = run_raw("--home", tmp, "inspect", f"claude:{PARENT}", "--json")
            self.assertEqual(json.loads(output)["total_messages"], 4)


if __name__ == "__main__":
    unittest.main()
