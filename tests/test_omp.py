"""omp session trees, subagents, selectors and transcripts."""

from __future__ import annotations

import contextlib
import gzip
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import (
    MAIN_STEM,
    OMP_BUCKET,
    make_omp_home,
    omp_header,
    omp_msg,
    run_full,
    write_jsonl,
    write_omp,
)

from find_session import (
    discovery,
    jsonl,
    model,
    recent,
)
from find_session.parsers import omp


class OmpHistoryTests(unittest.TestCase):
    run_cli = staticmethod(run_full)

    def make_home(self, tmp: str) -> Path:
        return make_omp_home(tmp)

    def test_active_branch_only_and_non_conversation_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = self.make_home(tmp)
            parsed = omp.parse_omp(home / OMP_BUCKET / f"{MAIN_STEM}.jsonl")
            self.assertEqual(parsed.source, "omp")
            self.assertEqual(parsed.session_id, "omp-main-1")
            self.assertEqual(parsed.project, "~/projects/demo")
            self.assertEqual(parsed.user_messages, ["Plan the pilot PILOTTOPIC", "New branch"])
            self.assertEqual(parsed.assistant_messages, ["Plan ready", "New branch result"])
            code, output = self.run_cli("--home", tmp, "inspect", "omp-main-1")
            self.assertEqual(code, 0)
            self.assertEqual(output.count("Plan the pilot"), 1)
            for leaked in ("SYNTH_", "ABANDONED", "PARTI"):
                self.assertNotIn(leaked, output)

    def test_malformed_headerless_and_oversized_lines_do_not_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "2026-09-22T10-00-00-000Z_fallback-id.jsonl"
            write_omp(
                path,
                [
                    "\x00\xff{broken\n",
                    "[1, 2]\n",
                    json.dumps(omp_msg("u1", None, "user", "X" * 5000)) + "\n",
                    omp_msg("u2", None, "user", "No header"),
                ],
                title=None,
                tail='{"type":',
            )
            with mock.patch.object(jsonl, "MAX_LINE_BYTES", 1024):
                parsed = omp.parse_omp(path)
            self.assertEqual(parsed.session_id, "fallback-id")
            self.assertEqual(parsed.user_messages, ["No header"])

    def test_cycle_in_parent_chain_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "2026-09-22T10-00-00-000Z_cycle.jsonl"
            write_omp(
                path,
                [
                    omp_header("cycle"),
                    omp_msg("a", "b", "user", "First"),
                    omp_msg("b", "a", "user", "Second"),
                ],
            )
            parsed = omp.parse_omp(path)
            self.assertEqual(parsed.user_messages, ["First", "Second"])

    def test_gzip_archive_is_read_and_live_file_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            bucket = home / OMP_BUCKET
            bucket.mkdir(parents=True)
            archived = [omp_header("archived-1"), omp_msg("u1", None, "user", "Archive ARCHTOPIC")]
            data = "".join(json.dumps(row) + "\n" for row in archived).encode()
            with gzip.open(bucket / "2026-01-01T00-00-00-000Z_archived-1.jsonl.gz", "wb") as handle:
                handle.write(data)
            write_omp(bucket / "2026-01-02T00-00-00-000Z_both.jsonl", [omp_header("both")])
            with gzip.open(bucket / "2026-01-02T00-00-00-000Z_both.jsonl.gz", "wb") as handle:
                handle.write(data)
            mains = [c for c in discovery.discover(home) if c.source == "omp"]
            self.assertEqual(sorted(c.session_id for c in mains), ["archived-1", "both"])
            both = next(c for c in mains if c.session_id == "both")
            self.assertTrue(both.path.name.endswith(".jsonl"))
            code, output = self.run_cli("--home", tmp, "search", "ARCHTOPIC")
            self.assertEqual(code, 0)
            self.assertIn("Session: archived-1", output)

    def test_children_are_inspectable_but_not_ranked_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.make_home(tmp)
            code, output = self.run_cli("--home", tmp, "search", "CHILDTOPIC")
            self.assertEqual(code, 0)
            self.assertIn("=== omp — top 0 of 0 ===", output)
            self.assertNotIn("Task", output)
            self.assertIn("Also 2 subagent or review session(s) containing all the words", output)

            code, output = self.run_cli("--home", tmp, "search", "CHILDTOPIC", "--children")
            self.assertIn("=== subagents", output)
            self.assertIn("omp:omp-main-1/Task", output)
            self.assertIn("omp:omp-main-1/Task/Sub", output)
            self.assertNotIn("ADVISOR", output)

            code, output = self.run_cli("--home", tmp, "inspect", "omp:omp-main-1/Task")
            self.assertEqual(code, 0)
            self.assertIn("Child task CHILDTOPIC", output)
            self.assertIn("Child result", output)
            self.assertNotIn("Grandchild", output)

            code, output = self.run_cli("--home", tmp, "inspect", "omp-main-1")
            self.assertIn("omp:omp-main-1/Task", output)
            self.assertNotIn("__advisor", output)

    def test_source_collision_requires_prefixed_selector(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            write_jsonl(
                home / ".claude/projects/-Users-alex-projects-demo/shared-1.jsonl",
                [{"message": {"role": "user", "content": "From Claude"}}],
            )
            write_omp(
                home / OMP_BUCKET / "2026-09-22T10-00-00-000Z_shared-1.jsonl",
                [omp_header("shared-1"), omp_msg("u1", None, "user", "From omp")],
            )
            code, output = self.run_cli("--home", tmp, "inspect", "shared-1")
            self.assertEqual(code, 2)
            self.assertIn("claude:shared-1", output)
            self.assertIn("omp:shared-1", output)
            self.assertNotIn("From omp", output)

            code, output = self.run_cli("--home", tmp, "inspect", "omp:shared-1")
            self.assertEqual(code, 0)
            self.assertIn("From omp", output)
            self.assertNotIn("From Claude", output)
            code, output = self.run_cli("--home", tmp, "inspect", "claude:shared-1")
            self.assertIn("From Claude", output)

    def test_three_independent_rankings_and_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = self.make_home(tmp)
            for number in range(3):
                write_jsonl(
                    home / f".claude/projects/-Users-alex-projects-demo/c-{number}.jsonl",
                    [{"message": {"role": "user", "content": "PILOTTOPIC claude"}}],
                )
            code, output = self.run_cli("--home", tmp, "search", "PILOTTOPIC", "--limit", "1")
            self.assertIn("=== Claude — top 1 of 3 ===", output)
            self.assertIn("=== Codex — top 0 of 0 ===", output)
            self.assertIn("=== omp — top 1 of 1 ===", output)
            self.assertIn("omp --resume omp-main-1", output)

            code, output = self.run_cli("--home", tmp, "search", "PILOTTOPIC", "--json")
            payload = json.loads(output)
            self.assertEqual(payload["counts"], {"Claude": 3, "Codex": 0, "omp": 1, "agy": 0})
            omp_rows = payload["results"]["omp"]
            self.assertEqual(omp_rows[0]["selector"], "omp:omp-main-1")
            self.assertEqual(omp_rows[0]["kind"], "main")

            code, output = self.run_cli("--home", tmp, "inspect", "omp:omp-main-1", "--json")
            payload = json.loads(output)
            self.assertEqual(payload["source"], "omp")
            self.assertEqual([m["role"] for m in payload["messages"]], ["user", "assistant", "user", "assistant"])
            self.assertIn("omp:omp-main-1/Task", payload["children"])

    def test_missing_directories_and_sources_diagnostic(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, output = self.run_cli("--home", tmp, "search", "anything")
            self.assertEqual(code, 0)
            self.assertIn("(no matches in omp)", output)
            with mock.patch.dict(os.environ, {"OMP_SESSIONS_ROOT": str(Path(tmp) / "nope")}):
                code, output = self.run_cli("--home", tmp, "sources", "--json")
            payload = json.loads(output)
            omp_info = payload["omp"]
            self.assertEqual(omp_info["roots"][0]["exists"], False)
            self.assertEqual(omp_info["main"], 0)

    def test_root_overrides_and_profiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            custom = Path(tmp) / "agent"
            write_omp(
                custom / "sessions/-x/2026-09-22T10-00-00-000Z_from-agent-dir.jsonl",
                [omp_header("from-agent-dir")],
            )
            write_omp(
                home / ".omp/profiles/work/agent/sessions/-x/2026-09-22T10-00-00-000Z_from-profile.jsonl",
                [omp_header("from-profile")],
            )
            with mock.patch.dict(os.environ, {"PI_CODING_AGENT_DIR": str(custom)}):
                ids = {c.session_id for c in discovery.discover(home) if c.source == "omp"}
            self.assertEqual(ids, {"from-agent-dir", "from-profile"})
            with mock.patch.dict(os.environ, {"OMP_SESSIONS_ROOT": str(custom / "sessions")}):
                ids = {c.session_id for c in discovery.discover(home) if c.source == "omp"}
            self.assertEqual(ids, {"from-agent-dir"})

    def test_index_and_brief_count_omp_mains_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = self.make_home(tmp)
            out = home / "index.md"
            with contextlib.redirect_stdout(io.StringIO()):
                recent.build_index(home, out)
            index = out.read_text()
            self.assertIn("Claude 0, Codex 0, omp 1", index)
            self.assertIn("\nomp:omp-main ", index)
            self.assertNotIn("CHILDTOPIC", index)
            captured = io.StringIO()
            with contextlib.redirect_stdout(captured):
                recent.build_recent(home, 1_000_000, 10)
            self.assertIn("Claude Code + Codex + omp", captured.getvalue())
            self.assertIn("PILOTTOPIC", captured.getvalue())


class OmpSelectorAndTranscriptTests(unittest.TestCase):
    run_cli = staticmethod(run_full)

    def test_omp_parse_keeps_only_text_in_memory(self):
        import tracemalloc

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "2026-09-22T10-00-00-000Z_big.jsonl"
            rows: list = [omp_header("big"), omp_msg("u1", None, "user", "Large results")]
            parent = "u1"
            for number in range(10):
                result_id = f"r{number}"
                rows.append(omp_msg(result_id, parent, "toolResult", [{"type": "text", "text": "Z" * 5_000_000}]))
                parent = result_id
            rows.append(omp_msg("a1", parent, "assistant", [{"type": "text", "text": "Done"}]))
            write_omp(path, rows)
            tracemalloc.start()
            parsed = omp.parse_omp(path)
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            self.assertEqual(parsed.assistant_messages, ["Done"])
            # One 5 MB record at a time, not all ten retained until the tree walk.
            self.assertLess(peak, 25_000_000)

    def test_header_id_differs_from_filename_roundtrips(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            main = home / OMP_BUCKET / "2026-09-22T10-00-00-000Z_file-id.jsonl"
            write_omp(main, [omp_header("header-id"), omp_msg("u1", None, "user", "Moved MOVEDTOPIC")])
            write_omp(
                main.with_suffix("") / "Task.jsonl",
                [omp_header("c"), omp_msg("u1", None, "user", "Child MOVEDTOPIC")],
            )
            code, output = self.run_cli("--home", tmp, "search", "MOVEDTOPIC", "--children", "--json")
            payload = json.loads(output)
            self.assertEqual(payload["results"]["omp"][0]["selector"], "omp:header-id")
            self.assertEqual(payload["results"]["subagents"][0]["selector"], "omp:header-id/Task")
            code, output = self.run_cli("--home", tmp, "inspect", "omp:header-id", "--json")
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output)["children"], ["omp:header-id/Task"])
            code, output = self.run_cli("--home", tmp, "inspect", "omp:header-id/Task")
            self.assertEqual(code, 0)
            self.assertIn("Child MOVEDTOPIC", output)

    def test_inspect_is_chronological_for_omp_and_claude(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            write_omp(
                home / OMP_BUCKET / "2026-09-22T10-00-00-000Z_chrono.jsonl",
                [
                    omp_header("chrono"),
                    omp_msg("u1", None, "user", "U1"),
                    omp_msg("a1", "u1", "assistant", [{"type": "text", "text": "A1"}]),
                    omp_msg("r1", "a1", "toolResult", [{"type": "text", "text": "x"}]),
                    omp_msg("a2", "r1", "assistant", [{"type": "text", "text": "A2"}]),
                    omp_msg("u2", "a2", "user", "U2"),
                    omp_msg("a3", "u2", "assistant", [{"type": "text", "text": "A3"}]),
                ],
            )
            write_jsonl(
                home / ".claude/projects/-Users-alex-x/chrono-claude.jsonl",
                [
                    {"message": {"role": "user", "content": "U1"}},
                    {"message": {"role": "assistant", "content": [{"type": "text", "text": "A1"}]}},
                    {"message": {"role": "assistant", "content": [{"type": "text", "text": "A2"}]}},
                    {"message": {"role": "user", "content": "U2"}},
                ],
            )
            code, output = self.run_cli("--home", tmp, "inspect", "omp:chrono", "--json")
            texts = [(m["role"], m["text"]) for m in json.loads(output)["messages"]]
            self.assertEqual(
                texts,
                [("user", "U1"), ("assistant", "A1"), ("assistant", "A2"), ("user", "U2"), ("assistant", "A3")],
            )
            code, output = self.run_cli("--home", tmp, "inspect", "chrono-claude", "--json")
            texts = [m["text"] for m in json.loads(output)["messages"]]
            self.assertEqual(texts, ["U1", "A1", "A2", "U2"])
            code, output = self.run_cli("--home", tmp, "inspect", "chrono-claude")
            self.assertLess(output.index("AGENT: A2"), output.index("USER: U2"))

    def test_agent_authored_and_async_messages_are_not_human(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            main = home / OMP_BUCKET / "2026-09-22T10-00-00-000Z_async.jsonl"
            async_result = omp_msg("n1", "a1", "custom", "<system-notice>ASYNCSYNTH</system-notice>")
            async_result["message"]["customType"] = "async-result"
            agent_prompt = omp_msg("p1", "n1", "user", "AGENTPROMPT continue")
            agent_prompt["message"]["attribution"] = "agent"
            reminder = omp_msg("d1", "p1", "developer", "REMINDERSYNTH")
            reminder["message"]["synthetic"] = True
            human = omp_msg("u1", None, "user", "Human asks")
            human["message"]["attribution"] = "user"
            write_omp(
                main,
                [
                    omp_header("async"),
                    human,
                    omp_msg("a1", "u1", "assistant", [{"type": "text", "text": "Answer"}]),
                    async_result,
                    agent_prompt,
                    reminder,
                ],
            )
            child_prompt = omp_msg("u1", "i1", "user", "Implement DELEGTOPIC")
            child_prompt["message"]["attribution"] = "agent"
            write_omp(
                main.with_suffix("") / "Task.jsonl",
                [
                    omp_header("child"),
                    {"type": "session_init", "id": "i1", "parentId": None, "task": "Implement DELEGTOPIC"},
                    child_prompt,
                    omp_msg("a1", "u1", "assistant", [{"type": "text", "text": "Finished"}]),
                ],
            )
            write_omp(
                main.with_suffix("") / "Init.jsonl",
                [
                    omp_header("init-only"),
                    {"type": "session_init", "id": "i1", "parentId": None, "task": "Only INITTOPIC"},
                ],
            )
            parsed = omp.parse_omp(main)
            self.assertEqual(parsed.user_messages, ["Human asks"])
            code, output = self.run_cli("--home", tmp, "inspect", "omp:async", "--json")
            messages = json.loads(output)["messages"]
            self.assertEqual([m["role"] for m in messages], ["user", "assistant", "delegated"])
            self.assertNotIn("ASYNCSYNTH", output)
            self.assertNotIn("REMINDERSYNTH", output)

            code, output = self.run_cli("--home", tmp, "inspect", "omp:async/Task", "--json")
            roles = [(m["role"], m["text"]) for m in json.loads(output)["messages"]]
            self.assertEqual(roles, [("delegated", "Implement DELEGTOPIC"), ("assistant", "Finished")])
            code, output = self.run_cli("--home", tmp, "inspect", "omp:async/Task")
            self.assertIn("DELEGATED TASK: Implement DELEGTOPIC", output)
            self.assertNotIn("USER:", output)
            code, output = self.run_cli("--home", tmp, "inspect", "omp:async/Init", "--json")
            self.assertEqual(
                [(m["role"], m["text"]) for m in json.loads(output)["messages"]],
                [("delegated", "Only INITTOPIC")],
            )
            code, output = self.run_cli("--home", tmp, "search", "DELEGTOPIC", "--children")
            self.assertIn("[delegated task] Implement DELEGTOPIC", output)

    def test_exact_selector_wins_beyond_substring_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            project = home / ".claude/projects/-Users-alex-x"
            write_jsonl(project / "dup.jsonl", [{"message": {"role": "user", "content": "Exact"}}])
            for number in range(model.MAX_AMBIGUOUS_LOAD + 1):
                write_jsonl(project / f"dup-{number}.jsonl", [{"message": {"role": "user", "content": "Other"}}])
            code, output = self.run_cli("--home", tmp, "inspect", "dup")
            self.assertEqual(code, 0)
            self.assertIn("Exact", output)


if __name__ == "__main__":
    unittest.main()
