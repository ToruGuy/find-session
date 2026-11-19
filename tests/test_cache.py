"""The index of parsed sessions (the cache)."""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import claude_turns, codex_answer, codex_meta, codex_user, make_omp_home, run_full, run_raw, write_jsonl

from find_session import cache as cache_module
from find_session import index as index_module

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "find-session" / "scripts"

# Every way of asking, so that "same result with and without the index" means all of them.
COMMANDS = [
    ["search", "CACHETOPIC"],
    ["search", "CACHETOPIC", "--full"],
    ["search", "CACHETOPIC", "--json", "--children"],
    ["search", "cachetopic zażółć"],
    ["search", "zażółć gęślą", "--json"],
    ["search", "ŻÓŁĆ", "--children"],
    ["search", "ars", "--full"],
    ["search", "pytest", "--json"],
    ["search", "demo"],
    ["search", "cachetopic", "--source", "codex", "--full"],
    ["search", "cachetopic", "--project", "garden", "--json"],
    ["search", "cachetopic", "--since", "2026-09-01", "--until", "2027-01-01"],
    ["search", "cachetopic", "--limit", "1"],
    ["search", "nothing-like-this"],
    ["inspect", "cached-1"],
    ["inspect", "cached-1", "--json", "--tools"],
    ["inspect", "long-1", "--range", "3:6"],
    ["inspect", "long-1", "--tail", "2", "--grep", "cachetopic"],
    ["inspect", "codex:codex-parent", "--json"],
    ["inspect", "omp:omp-main-1"],
    ["inspect", "no-such-session"],
    ["index"],
    ["recent", "--hours", "1000000"],
    ["sources", "--json"],
]


def build_history(home: Path) -> None:
    """Synthetic sessions of every source and kind, with Polish text and commands."""
    make_omp_home(home)
    write_jsonl(
        home / ".claude/projects/-Users-alex-x/cached-1.jsonl",
        [
            {"cwd": "/Users/alex/garden", "message": {"role": "user", "content": "CACHETOPIC first Zażółć gęślą jaźń"}},
            {
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Running the checks"},
                        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "pytest -k vars"}},
                    ],
                }
            },
        ],
    )
    write_jsonl(
        home / ".claude/projects/-Users-alex-x/long-1.jsonl",
        claude_turns(
            *[
                text
                for n in range(12)
                for text in (f"question {n} cachetopic" if n % 4 == 0 else f"question {n}", f"answer {n}")
            ]
        ),
    )
    write_jsonl(
        home / ".codex/sessions/2026/09/22/rollout-codex-parent.jsonl",
        [codex_meta("codex-parent"), codex_user("CACHETOPIC from codex"), codex_answer("ŻÓŁĆ and ars")],
    )
    child_meta = codex_meta("codex-child")
    child_meta["payload"]["source"] = {"subagent": {"thread_spawn": {"parent_thread_id": "codex-parent"}}}
    write_jsonl(
        home / ".codex/sessions/2026/09/22/rollout-codex-child.jsonl",
        [child_meta, codex_user("Delegated CACHETOPIC task"), codex_answer("child done")],
    )


def outputs(home: Path, *leading: str) -> list[tuple[int, str]]:
    results = []
    for command in COMMANDS:
        code, output = run_raw("--home", str(home), *leading, *command)
        # `index` and `recent` print the minute they ran in.
        results.append((code, re.sub(r"(built|collected) \d{4}-\d\d-\d\d \d\d:\d\d", r"\1 NOW", output)))
    return results


class IndexCase(unittest.TestCase):
    run_cli = staticmethod(run_full)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "history"
        self.cache_dir = Path(self.tmp.name) / "cache"
        self.session = self.home / ".claude/projects/-Users-alex-x/cached-1.jsonl"
        patcher = mock.patch.dict(os.environ, {"FIND_SESSION_CACHE": str(self.cache_dir)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def index_files(self) -> list[Path]:
        return sorted(self.cache_dir.glob("index-*.sqlite3"))

    def stored_paths(self) -> list[str]:
        [index] = self.index_files()
        db = sqlite3.connect(index)
        try:
            return [Path(path).name for (path,) in db.execute("SELECT path FROM sessions ORDER BY path")]
        finally:
            db.close()

    def stored_ids(self) -> list[int]:
        [index] = self.index_files()
        db = sqlite3.connect(index)
        try:
            return [row_id for (row_id,) in db.execute("SELECT id FROM sessions")]
        finally:
            db.close()

    def no_parsing(self):
        return mock.patch.object(index_module, "parse_candidate", side_effect=AssertionError("parsed"))


class CacheTests(IndexCase):
    def setUp(self):
        super().setUp()
        write_jsonl(self.session, [{"message": {"role": "user", "content": "CACHETOPIC first"}}])

    def test_cached_output_equals_uncached_and_file_is_private(self):
        _, cold = self.run_cli("--home", str(self.home), "search", "CACHETOPIC", "--json")
        _, warm = self.run_cli("--home", str(self.home), "search", "CACHETOPIC", "--json")
        _, off = self.run_cli("--home", str(self.home), "--no-cache", "search", "CACHETOPIC", "--json")
        self.assertEqual(json.loads(cold), json.loads(warm))
        self.assertEqual(json.loads(warm), json.loads(off))
        [index] = self.index_files()
        self.assertEqual(index.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.cache_dir.stat().st_mode & 0o777, 0o700)
        with self.no_parsing():
            code, output = self.run_cli("--home", str(self.home), "search", "CACHETOPIC")
        self.assertEqual(code, 0)
        self.assertIn("Session: cached-1", output)

    def test_every_file_next_to_the_index_is_private(self):
        self.run_cli("--home", str(self.home), "search", "CACHETOPIC")
        [index] = self.index_files()
        # An open connection that has written keeps the -wal and -shm files in place.
        db = sqlite3.connect(index)
        self.addCleanup(db.close)
        db.execute("CREATE TABLE probe (x)")
        names = sorted(path.name for path in self.cache_dir.iterdir())
        self.assertIn(index.name + "-wal", names)
        self.assertIn(index.name + "-shm", names)
        for path in self.cache_dir.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600, path.name)

    def test_appended_session_is_parsed_again(self):
        self.run_cli("--home", str(self.home), "search", "CACHETOPIC")
        with self.session.open("a") as handle:
            handle.write(json.dumps({"message": {"role": "user", "content": "CACHETOPIC NEWWORD"}}) + "\n")
        _, output = self.run_cli("--home", str(self.home), "search", "NEWWORD")
        self.assertIn("Session: cached-1", output)
        self.assertIn("Last request: CACHETOPIC NEWWORD", output)
        self.assertEqual(self.stored_paths(), ["cached-1.jsonl"])
        with self.no_parsing():
            _, output = self.run_cli("--home", str(self.home), "search", "NEWWORD")
        self.assertIn("Last request: CACHETOPIC NEWWORD", output)

    def test_corrupt_cache_is_rebuilt_and_deleted_sessions_pruned(self):
        self.run_cli("--home", str(self.home), "search", "CACHETOPIC")
        [index] = self.index_files()
        for leftover in self.cache_dir.glob("*-wal"):
            leftover.unlink()
        index.write_bytes(b"{broken")
        code, output = self.run_cli("--home", str(self.home), "search", "CACHETOPIC")
        self.assertEqual(code, 0)
        self.assertIn("Session: cached-1", output)
        self.assertNotIn("cache not saved", output)
        self.assertEqual(self.stored_paths(), ["cached-1.jsonl"])
        with self.no_parsing():
            self.run_cli("--home", str(self.home), "search", "CACHETOPIC")
        self.session.unlink()
        self.run_cli("--home", str(self.home), "search", "CACHETOPIC")
        self.assertEqual(self.stored_paths(), [])
        # The text of the deleted session is gone from the files, not only from the tables.
        for path in self.cache_dir.iterdir():
            self.assertNotIn(b"cachetopic", path.read_bytes().lower(), path.name)

    def test_damage_inside_the_index_is_rebuilt(self):
        self.run_cli("--home", str(self.home), "search", "CACHETOPIC")
        [index] = self.index_files()
        db = sqlite3.connect(index)
        db.execute("UPDATE records SET record = x'00ff00'")
        db.execute("UPDATE texts SET layout = x'00ff00'")
        db.commit()
        db.close()
        code, output = self.run_cli("--home", str(self.home), "search", "CACHETOPIC")
        self.assertEqual(code, 0)
        self.assertIn("Session: cached-1", output)
        with self.no_parsing():
            code, output = self.run_cli("--home", str(self.home), "inspect", "cached-1")
        self.assertIn("CACHETOPIC first", output)

    def test_partial_runs_keep_other_entries(self):
        codex = self.home / ".codex/sessions/2026/09/22/rollout-codex-cached.jsonl"
        write_jsonl(codex, [codex_meta("codex-cached"), codex_user("CACHETOPIC from codex")])
        self.run_cli("--home", str(self.home), "search", "CACHETOPIC")
        self.run_cli("--home", str(self.home), "search", "CACHETOPIC", "--source", "claude")
        self.run_cli("--home", str(self.home), "sources")
        self.run_cli("--home", str(self.home), "inspect", "cached-1")
        self.assertEqual(self.stored_paths(), ["cached-1.jsonl", "rollout-codex-cached.jsonl"])

    def test_caches_of_older_versions_are_removed(self):
        self.cache_dir.mkdir(parents=True)
        old = [
            self.cache_dir / "sessions-0123456789ab.json",
            self.cache_dir / "sessions-0123456789ab-py310.bin",
            self.cache_dir / "sessions-ba9876543210-py39.bin",
            self.cache_dir / ".sessions-ba9876543210-py39.bin.4242.tmp",
        ]
        for path in old:
            path.write_bytes(b"old conversation text")
        keep = self.cache_dir / "notes.txt"
        keep.write_text("not ours")
        self.run_cli("--home", str(self.home), "search", "CACHETOPIC")
        self.assertEqual([path for path in old if path.exists()], [])
        self.assertTrue(keep.exists())
        self.assertEqual(len(self.index_files()), 1)

    def test_uninstall_of_the_npx_wrapper_knows_every_file_the_index_leaves(self):
        # The wrapper deletes cache files by name without asking Python, so its
        # pattern has to follow the names used here.
        source = (SCRIPTS.parents[2] / "npm" / "find-session.js").read_text()
        pattern = re.compile(re.search(r"const CACHE_FILE =\s*/(.+)/;", source).group(1))
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            build_history(home)
            with mock.patch.dict(os.environ, {"FIND_SESSION_CACHE": str(Path(tmp) / "cache")}):
                location = cache_module.cache_path(home)
                index = index_module.SessionIndex(location)
                index.open()
                # While the index is open SQLite has its companions on disk as well.
                names = {path.name for path in location.parent.iterdir()}
                index.save()
            self.assertIn(location.name, names)
            names |= {location.name + suffix for suffix in index_module.COMPANIONS}
            names |= {
                "sessions-0123456789ab-py312.bin",
                "sessions-0123456789ab.json",
                ".sessions-0123456789ab-py39.bin.7.tmp",
            }
            for name in sorted(names):
                self.assertTrue(pattern.match(name), f"uninstall would leave {name} behind")
            for name in ("index.html", "index-notes.sqlite3", "notes.txt", "sessions.json", location.name + ".bak"):
                self.assertFalse(pattern.match(name), f"uninstall would delete {name}")

    def test_other_parser_version_rebuilds(self):
        self.run_cli("--home", str(self.home), "search", "CACHETOPIC")
        ids_before = self.stored_ids()
        with mock.patch.object(index_module, "FORMAT", "another-format"):
            with self.assertRaises(AssertionError):
                with self.no_parsing():
                    self.run_cli("--home", str(self.home), "search", "CACHETOPIC")
            _, output = self.run_cli("--home", str(self.home), "search", "CACHETOPIC")
            self.assertIn("Session: cached-1", output)
            with self.no_parsing():
                self.run_cli("--home", str(self.home), "search", "CACHETOPIC")
        self.assertEqual(self.stored_paths(), ["cached-1.jsonl"])
        # A rebuild never hands out an id again: a search running meanwhile
        # still holds the old ones.
        self.assertGreater(min(self.stored_ids()), max(ids_before))

    def test_cache_can_be_disabled(self):
        with mock.patch.dict(os.environ, {"FIND_SESSION_CACHE": "off"}):
            self.run_cli("--home", str(self.home), "search", "CACHETOPIC")
        with mock.patch.object(cache_module, "cache_path", side_effect=AssertionError("cache used")):
            self.run_cli("--home", str(self.home), "--no-cache", "search", "CACHETOPIC")
        self.assertFalse(self.cache_dir.exists())

    def test_unwritable_cache_still_answers(self):
        blocker = Path(self.tmp.name) / "file"
        blocker.write_text("")
        with mock.patch.dict(os.environ, {"FIND_SESSION_CACHE": str(blocker / "below")}):
            code, output = self.run_cli("--home", str(self.home), "search", "CACHETOPIC")
        self.assertEqual(code, 0)
        self.assertIn("Session: cached-1", output)
        self.assertIn("cache not saved", output)


class SameResultsTests(IndexCase):
    """With the index, without it, and after the history changed: the same output."""

    def setUp(self):
        super().setUp()
        build_history(self.home)

    def assert_same_as_parsing(self, label: str) -> None:
        expected = outputs(self.home, "--no-cache")
        self.assertEqual(outputs(self.home), expected, label)
        return expected

    def test_index_gives_the_same_output_as_parsing(self):
        expected = self.assert_same_as_parsing("first run")
        with self.no_parsing():
            self.assertEqual(outputs(self.home), expected, "served from the index")
        self.assertTrue(any("cached-1" in output for _, output in expected))

    def test_same_output_after_the_history_changes(self):
        self.assert_same_as_parsing("built")
        with self.session.open("a") as handle:
            handle.write(json.dumps({"message": {"role": "user", "content": "now also PÓŹNIEJ cachetopic"}}) + "\n")
        self.assert_same_as_parsing("file appended")
        write_jsonl(
            self.home / ".claude/projects/-Users-alex-y/added-1.jsonl",
            claude_turns("brand new cachetopic session", "noted"),
        )
        write_jsonl(
            self.home / ".codex/sessions/2026/09/23/rollout-codex-added.jsonl",
            [codex_meta("codex-added"), codex_user("cachetopic again"), codex_answer("ok")],
        )
        self.assert_same_as_parsing("files added")
        (self.home / ".claude/projects/-Users-alex-x/long-1.jsonl").unlink()
        (self.home / ".codex/sessions/2026/09/22/rollout-codex-child.jsonl").unlink()
        expected = self.assert_same_as_parsing("files deleted")
        self.assertNotIn("long-1.jsonl", self.stored_paths())
        self.assertNotIn("rollout-codex-child.jsonl", self.stored_paths())
        self.assertIn("added-1.jsonl", self.stored_paths())
        with self.no_parsing():
            self.assertEqual(outputs(self.home), expected, "served from the index")

    def test_rewritten_file_of_the_same_size_is_parsed_again(self):
        self.run_cli("--home", str(self.home), "search", "CACHETOPIC")
        before = self.session.stat()
        text = self.session.read_text()
        self.session.write_text(text.replace("CACHETOPIC", "OTHERTOPIC"))
        os.utime(self.session, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000))
        self.assertEqual(self.session.stat().st_size, before.st_size)
        _, output = self.run_cli("--home", str(self.home), "search", "OTHERTOPIC")
        self.assertIn("Session: cached-1", output)

    def test_only_printed_sessions_are_read_in_full(self):
        self.run_cli("--home", str(self.home), "search", "CACHETOPIC")
        loaded: list[str] = []
        real = index_module.session_from_record

        def counting(path, record):
            loaded.append(path.name)
            return real(path, record)

        with mock.patch.object(index_module, "session_from_record", side_effect=counting):
            run_raw("--home", str(self.home), "search", "CACHETOPIC", "--limit", "1", "--source", "claude")
            self.assertEqual(len(loaded), 1)
            del loaded[:]
            run_raw("--home", str(self.home), "inspect", "cached-1")
            self.assertEqual(loaded, ["cached-1.jsonl"])

    def test_no_text_is_stored_before_redaction(self):
        secret = "hunter2" + "Zx9" * 4
        with self.session.open("a") as handle:
            handle.write(
                json.dumps({"message": {"role": "user", "content": f"password={secret} for cachetopic"}}) + "\n"
            )
        self.run_cli("--home", str(self.home), "search", "CACHETOPIC")
        for path in self.cache_dir.iterdir():
            data = path.read_bytes()
            self.assertNotIn(secret.encode(), data, path.name)
            self.assertNotIn(secret.lower().encode(), data, path.name)


class StartupTests(IndexCase):
    def test_search_from_the_index_loads_no_parser_and_no_heavy_module(self):
        build_history(self.home)
        # omp sessions are told apart by their header, which discovery reads with the omp parser.
        shutil.rmtree(self.home / ".omp")
        self.run_cli("--home", str(self.home), "search", "CACHETOPIC")
        program = (
            "import sys\n"
            "from find_session.cli import main\n"
            "code = main(sys.argv[1:])\n"
            "print('LOADED', ' '.join(sorted(sys.modules)), file=sys.stderr)\n"
            "raise SystemExit(code)\n"
        )
        env = dict(os.environ, FIND_SESSION_CACHE=str(self.cache_dir), PYTHONPATH=str(SCRIPTS))
        for command in (["search", "cachetopic zażółć", "--children"], ["inspect", "cached-1", "--tools"]):
            result = subprocess.run(
                [sys.executable, "-c", program, "--home", str(self.home), *command],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("cached-1", result.stdout)
            loaded = set(result.stderr.split("LOADED", 1)[1].split())
            unwanted = {
                "find_session.redact",
                "find_session.titles",
                "find_session.tools",
                "find_session.jsonl",
                "find_session.parsers.claude",
                "find_session.parsers.codex",
                "find_session.parsers.omp",
                "dataclasses",
                "inspect",
                "hashlib",
                "gzip",
            }
            self.assertEqual(loaded & unwanted, set(), command)
            self.assertNotIn("json", loaded)


class ConcurrencyTests(IndexCase):
    def setUp(self):
        super().setUp()
        build_history(self.home)

    def run_processes(self, count: int, *command: str) -> list[subprocess.CompletedProcess]:
        env = dict(os.environ, FIND_SESSION_CACHE=str(self.cache_dir), PYTHONPATH=str(SCRIPTS))
        processes = [
            subprocess.Popen(
                [sys.executable, "-m", "find_session", "--home", str(self.home), *command],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                text=True,
            )
            for _ in range(count)
        ]
        done = []
        for process in processes:
            out, err = process.communicate(timeout=120)
            done.append(subprocess.CompletedProcess(process.args, process.returncode, out, err))
        return done

    def test_processes_building_and_searching_at_once_agree(self):
        command = ("search", "cachetopic", "--json", "--children")
        # All start without an index, so several build it at the same time.
        results = self.run_processes(8, *command)
        self.assertEqual({result.returncode for result in results}, {0})
        self.assertEqual({result.stderr for result in results}, {""})
        self.assertEqual(len({result.stdout for result in results}), 1)
        # Then a session changes while they search.
        with self.session.open("a") as handle:
            handle.write(json.dumps({"message": {"role": "user", "content": "cachetopic once more"}}) + "\n")
        results = self.run_processes(8, *command)
        self.assertEqual({result.stderr for result in results}, {""})
        self.assertEqual(len({result.stdout for result in results}), 1)
        _, expected = run_raw("--home", str(self.home), "--no-cache", *command)
        self.assertEqual(json.loads(results[0].stdout)["counts"], json.loads(expected)["counts"])
        [index] = self.index_files()
        db = sqlite3.connect(index)
        try:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone(), ("ok",))
            total, distinct = db.execute("SELECT count(*), count(DISTINCT path) FROM sessions").fetchone()
            self.assertEqual(total, distinct)
            self.assertEqual(db.execute("SELECT count(*) FROM texts").fetchone()[0], total)
            self.assertEqual(db.execute("SELECT count(*) FROM records").fetchone()[0], total)
        finally:
            db.close()
        with self.no_parsing():
            code, _ = run_raw("--home", str(self.home), *command)
        self.assertEqual(code, 0)

    def test_row_replaced_by_another_process_mid_search_is_parsed(self):
        self.run_cli("--home", str(self.home), "search", "CACHETOPIC")
        _, expected = run_raw("--home", str(self.home), "search", "cachetopic", "--json")
        real = index_module.SessionIndex.texts

        def texts_after_other_writer(index, batch):
            # Another process replaced every row after this one read the table.
            other = sqlite3.connect(index.path)
            for table in ("texts", "records", "sessions"):
                other.execute(f"DELETE FROM {table}")
            other.commit()
            other.close()
            return real(index, batch)

        with mock.patch.object(index_module.SessionIndex, "texts", texts_after_other_writer):
            _, output = run_raw("--home", str(self.home), "search", "cachetopic", "--json")
        self.assertEqual(json.loads(output), json.loads(expected))

    def test_row_id_taken_over_by_other_content_mid_search_is_not_trusted(self):
        self.run_cli("--home", str(self.home), "search", "CACHETOPIC")
        _, expected = run_raw("--home", str(self.home), "search", "cachetopic", "--json")
        real = index_module.SessionIndex.texts

        def texts_after_rebuild(index, batch):
            # As after a rebuild by another process: the ids this run read now
            # belong to other versions of the files, with other text.
            other = sqlite3.connect(index.path)
            for row_id, low in other.execute("SELECT id, low FROM texts").fetchall():
                other.execute(
                    "UPDATE texts SET low = ? WHERE id = ?", (low.replace(b"cachetopic", b"zzzzzzzzzz"), row_id)
                )
            other.execute("UPDATE sessions SET size = size + 1")
            other.commit()
            other.close()
            return real(index, batch)

        with mock.patch.object(index_module.SessionIndex, "texts", texts_after_rebuild):
            _, output = run_raw("--home", str(self.home), "search", "cachetopic", "--json")
        self.assertEqual(json.loads(output), json.loads(expected))


if __name__ == "__main__":
    unittest.main()
