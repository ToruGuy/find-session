"""Which session files are found, and in which order."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from helpers import write_jsonl

from find_session import discovery


def rows() -> list[dict]:
    return [{"message": {"role": "user", "content": "hello"}}]


@unittest.skipUnless(os.sep == "/", "the fast listing is used on POSIX only")
class FastListingTests(unittest.TestCase):
    """The scandir listing must return what the pathlib one defines: same files, same order."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "home"
        self.claude = self.home / ".claude/projects"
        self.codex = self.home / ".codex/sessions"
        # Names chosen so that sorting paths as text and sorting them part by
        # part disagree ("a-b" sorts before "a/…" as text, after it by parts).
        for name in (
            "-Users-alex-a/one.jsonl",
            "-Users-alex-a/two.jsonl",
            "-Users-alex-a-b/zero.jsonl",
            "-Users-alex-a.b/dotted.jsonl",
            "-Users-alex-a/.hidden.jsonl",
            ".hidden-project/inside.jsonl",
            "subagents/skipped.jsonl",
            "-Users-alex-a/subagents/too-deep.jsonl",
            "-Users-alex-a/notes.txt",
            "-Users-alex-a/UPPER.JSONL",
        ):
            write_jsonl(self.claude / name, rows())
        write_jsonl(self.claude / "top-level.jsonl", rows())
        (self.claude / "-Users-alex-a/directory.jsonl").mkdir()
        os.symlink(self.claude / "-Users-alex-a", self.claude / "-linked-project")
        os.symlink(self.claude / "-Users-alex-a/one.jsonl", self.claude / "-Users-alex-a-b/linked.jsonl")
        os.symlink(self.claude / "missing.jsonl", self.claude / "-Users-alex-a-b/broken.jsonl")
        for name in (
            "rollout-top.jsonl",
            "2026/09/22/rollout-b.jsonl",
            "2026/09/22/rollout-a.jsonl",
            "2026/09-22/rollout-c.jsonl",
            "2026/09/rollout-month.jsonl",
            "2026/10/01/plain-name.jsonl",
            "2026/10/01/.jsonl",
            "2026/10/01/readme.md",
            "archive.jsonl/rollout-inside.jsonl",
        ):
            write_jsonl(self.codex / name, rows())
        os.symlink(self.codex / "2026/09", self.codex / "2026/linked-month")
        os.symlink(self.codex / "2026/09/22/rollout-a.jsonl", self.codex / "2026/10/linked-file.jsonl")

    def assert_same(self, claude: Path, codex: Path) -> list:
        expected = discovery.glob_files(claude, codex)
        found = discovery.claude_files(claude) + discovery.codex_files(codex)
        self.assertEqual(
            [(c.source, c.location, c.session_id, c.kind, c.parent_id) for c in found],
            [(c.source, c.location, c.session_id, c.kind, c.parent_id) for c in expected],
        )
        self.assertEqual([c.path for c in found], [c.path for c in expected])
        return found

    def test_same_files_in_the_same_order_as_pathlib(self):
        found = self.assert_same(self.claude, self.codex)
        names = [c.name for c in found]
        self.assertIn("one.jsonl", names)
        self.assertIn("linked.jsonl", names)
        self.assertIn("rollout-inside.jsonl", names)
        self.assertNotIn("skipped.jsonl", names)
        self.assertNotIn("too-deep.jsonl", names)
        self.assertNotIn("UPPER.JSONL", names)
        # Reached through a symlinked project directory, but not through a symlinked Codex directory.
        self.assertEqual(sum(c.location.endswith("-linked-project/one.jsonl") for c in found), 1)
        self.assertEqual(sum("linked-month" in c.location for c in found), 0)

    def test_missing_empty_and_relative_roots(self):
        self.assert_same(self.home / "nowhere", self.home / "nowhere-else")
        empty = self.home / "empty"
        empty.mkdir()
        self.assertEqual(self.assert_same(empty, empty), [])
        previous = os.getcwd()
        os.chdir(self.home)
        try:
            self.assertTrue(self.assert_same(Path(".claude/projects"), Path(".codex/sessions")))
            os.chdir(self.claude)
            self.assertTrue(self.assert_same(Path("."), Path(".")))
        finally:
            os.chdir(previous)

    def test_root_below_a_subagents_directory_finds_nothing(self):
        root = self.home / "subagents" / "projects"
        write_jsonl(root / "-Users-alex-a/one.jsonl", rows())
        self.assertEqual(self.assert_same(root, self.codex)[0].source, "Codex")

    def test_listing_carries_size_and_mtime(self):
        for candidate in discovery.discover(self.home):
            if candidate.name == "broken.jsonl":
                self.assertIsNone(candidate.stat)
                continue
            real = os.stat(candidate.location)
            self.assertEqual((candidate.stat.st_size, candidate.stat.st_mtime_ns), (real.st_size, real.st_mtime_ns))


if __name__ == "__main__":
    unittest.main()
