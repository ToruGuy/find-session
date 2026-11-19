"""The examples in README.md, SKILL.md and assets/overview.svg are real output.

The same file keeps the other promises of the documents: every documented call
is accepted, every link leads somewhere, the one-line descriptions agree.

They are produced by running the tool on the synthetic history below, and the
documents must show exactly that. To see the current output (for pasting into
the documents after a change): `python3 tests/test_docs.py --print`.
"""

from __future__ import annotations

import html
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import helpers
from helpers import codex_answer, codex_meta, codex_user, write_jsonl

from find_session import cli

REPO = Path(__file__).resolve().parents[1]
ENTRY = REPO / "skills" / "find-session" / "scripts" / "sessions.py"
README = REPO / "README.md"
SKILL = REPO / "skills" / "find-session" / "SKILL.md"
OVERVIEW = REPO / "assets" / "overview.svg"
REFERENCE = REPO / "docs" / "REFERENCE.md"

# How the documents write the helper: the path differs per install, the examples use the command.
SHOWN_AS = "find-session"

LONG_ID = "0b7c2f1e-5d1a-4c55-9a43-2f1d8e0c9b11"
BLOG_ID = "5d1a9a43-77c2-4e0b-8f3a-6b2c1d0e9f55"
CALLER_ID = "7e3c9b02-1a4f-4d6e-b8c5-3f2a1e0d9c44"
CODEX_ID = "019a1aae-3c5e-7b21-9d4f-8a6b2c1e0f33"
CHILD_ID = "019a2bbf-4d6f-7c32-8e5a-9b7c3d2f1a44"


def stamp(day: int, hour: int, minute: int) -> float:
    return datetime(2026, 9, day, hour, minute, tzinfo=timezone.utc).timestamp()


def claude_message(role: str, text: str, number: int, cwd: str, last: tuple[float, int]) -> dict:
    """Message `number` of a session whose last message, number last[1], was written at last[0]."""
    content = text if role == "user" else [{"type": "text", "text": text}]
    moment = datetime.fromtimestamp(last[0] - 60 * (last[1] - number), tz=timezone.utc)
    when = moment.isoformat().replace("+00:00", "Z")
    return {"timestamp": when, "cwd": cwd, "message": {"role": role, "content": content}}


def build(home: Path, *, extras: bool) -> None:
    """A shop project discussed in Claude Code and Codex; `extras` adds a session
    with one of the words only, a Codex subagent and the session that is searching."""
    # The dates that are shown come from the records. The files themselves are
    # as new as this test run, like a history copied from another machine.
    shop = "/Users/alex/projects/shop"
    last = (stamp(22, 10, 0), 214)
    rows = []
    for number in range(1, 215):
        role = "user" if number % 2 else "assistant"
        text = f"Step {number}: tidy the ledger module." if role == "user" else f"Part {number} of the ledger is tidy."
        rows.append(claude_message(role, text, number, shop, last))
    # The stretch a search for "invoice export" points at, shown in README.md as `inspect --range`.
    stretch = {
        12: "The ledger module is tidy and its tests pass.",
        13: "Next: the invoices need a CSV file for the accountant.",
        14: "One row per document, or one row per line item? The second makes tax totals easy to check.",
        15: "Per line item, with the tax rate in its own column.",
        16: "I added an export command and a test for it.",
        17: "Use a semicolon as the separator, the accountant's spreadsheet expects it.",
        18: "The invoice export now writes one row per line item.",
        19: "Good. Now the tax report.",
    }
    for number, text in stretch.items():
        rows[number - 1] = claude_message("user" if number % 2 else "assistant", text, number, shop, last)
    rows.append({"type": "ai-title", "aiTitle": "CSV export for invoices", "sessionId": LONG_ID})
    path = home / f".claude/projects/-Users-alex-projects-shop/{LONG_ID}.jsonl"
    write_jsonl(path, rows)

    path = home / f".codex/sessions/2026/09/20/rollout-2026-09-20T16-00-00-{CODEX_ID}.jsonl"
    rows = [codex_meta(CODEX_ID, cwd=shop), codex_user("Why does the invoice export skip refunds?")]
    for number in range(2):
        rows += [
            codex_answer(f"Checked refund case {number}.", f"2026-09-20T16:{10 + number}:00Z"),
            codex_user(f"And case {number + 1}?"),
        ]
    rows.append(codex_answer("Refunds had the wrong status; fixed.", "2026-09-20T16:41:00Z"))
    write_jsonl(path, rows)
    if not extras:
        return

    blog = "/Users/alex/projects/blog"
    path = home / f".claude/projects/-Users-alex-projects-blog/{BLOG_ID}.jsonl"
    texts = ["Export the blog posts to Markdown", "Done: 42 posts.", "Also the drafts", "Added.", "Thanks", "Welcome."]
    last = (stamp(19, 9, 30), len(texts))
    write_jsonl(
        path,
        [claude_message("assistant" if n % 2 else "user", text, n + 1, blog, last) for n, text in enumerate(texts)],
    )

    path = home / f".claude/projects/-Users-alex-projects-shop/{CALLER_ID}.jsonl"
    texts = [
        "Hello",
        "Hi.",
        "Where is the code?",
        "In src.",
        "We did the invoice export before, find it.",
        "Searching.",
    ]
    last = (stamp(23, 9, 0), len(texts))
    write_jsonl(
        path,
        [claude_message("assistant" if n % 2 else "user", text, n + 1, shop, last) for n, text in enumerate(texts)],
    )

    path = home / f".codex/sessions/2026/09/20/rollout-2026-09-20T16-30-00-{CHILD_ID}.jsonl"
    write_jsonl(
        path,
        [
            codex_meta(CHILD_ID, cwd=shop, source={"subagent": {"thread_spawn": {"parent_thread_id": CODEX_ID}}}),
            codex_user("Find where the invoice export filters refunds"),
            codex_answer("The invoice export filters them in export.py.", "2026-09-20T16:50:00Z"),
        ],
    )


def run(home: Path, *argv: str, **env: str) -> str:
    """Output of the helper on the example history, as a user of that home sees it."""
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("FIND_SESSION_", "CLAUDE_CODE_", "CODEX_", "XDG_"))
    }
    environment.update({"HOME": str(home), "TZ": "UTC", "FIND_SESSION_CACHE": "off", "FIND_SESSION_COMMAND": SHOWN_AS})
    environment.update(env)
    done = subprocess.run([sys.executable, str(ENTRY), *argv], env=environment, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return done.stdout


def skill_example(home: Path) -> str:
    return run(home, "search", "invoice export", CLAUDE_CODE_SESSION_ID=CALLER_ID)


def overview_example(home: Path) -> str:
    # The picture shows the npx form, the shortest that works everywhere.
    return run(home, "search", "invoice export", FIND_SESSION_COMMAND="npx find-session")


def fenced_after(text: str, marker: str) -> str:
    """The first fenced block after `marker`."""
    return re.search(r"```[a-z]*\n(.*?)```", text[text.index(marker) :], re.S).group(1)


def helper_lines(text: str) -> list[list[str]]:
    """Every documented call of the helper, as its arguments: lines of code blocks and inline code."""
    found, inside = [], False
    for line in text.splitlines():
        if line.startswith("```"):
            inside = not inside
        elif inside:
            found.append(line.split("  #")[0].strip())
        else:
            found += re.findall(r"`((?:npx )?find-session [^`]+)`", line)
    calls = []
    for line in found:
        if line.startswith(("find-session ", "npx find-session ")) and "…" not in line and "<" not in line:
            words = shlex.split(line.split(" > ")[0])
            calls.append(words[words.index("find-session") + 1 :])
    return calls


class DocumentedOutputTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve() / "alex"

    def test_skill_example_is_the_real_default_output(self):
        build(self.home, extras=True)
        self.assertEqual(fenced_after(SKILL.read_text(), "The default output"), skill_example(self.home))

    def test_readme_example_is_part_of_the_real_output(self):
        build(self.home, extras=True)
        shown = fenced_after(README.read_text(), "A search prints")
        self.assertEqual(shown, skill_example(self.home))

    def test_readme_reads_the_stretch_its_search_example_points_at(self):
        build(self.home, extras=True)
        readme = README.read_text()
        marker = "The agent reads those"
        selector, stretch = re.search(
            r"`find-session inspect (\S+) --range (\d+:\d+)`", readme[readme.index(marker) :]
        ).groups()
        # The command reads the first hit of the search printed above it.
        search = fenced_after(readme, "A search prints")
        self.assertIn(f"{selector} ", search)
        self.assertIn(f" hit {stretch} ", search)
        first, last = (int(number) for number in stretch.split(":"))
        total = int(re.search(rf"{selector} .*· (\d+) msgs", search).group(1))
        self.assertIn(f"those {last - first + 1}\nmessages, not all {total}", readme)
        # What is shown is the real output up to its last line, the resume command, which the text describes.
        real = run(self.home, "inspect", selector, "--range", stretch)
        self.assertEqual(fenced_after(readme, marker), real[: real.index("\nResume: ")])
        self.assertIn("claude --resume", real[real.index("\nResume: ") :])
        self.assertIn("`claude --resume …`", readme)

    def test_overview_picture_shows_real_output_line_by_line(self):
        build(self.home, extras=False)
        svg = OVERVIEW.read_text()
        terminal = re.search(r'<g class="mono" id="terminal"[^>]*>(.*?)</g>', svg, re.S).group(1)
        lines = [
            html.unescape(re.sub(r"<[^>]+>", "", line)).strip() for line in re.findall(r"<text.*?</text>", terminal)
        ]
        commands = [line[2:] for line in lines if line.startswith("$ ")]
        printed = [line for line in lines if not line.startswith("$ ")]
        real = [line.strip() for line in overview_example(self.home).splitlines() if line.strip()]
        self.assertEqual(printed, real)
        self.assertEqual(commands[0], 'npx find-session search "invoice export"')
        # The second command reads the stretch the result points at: it has to exist and be that long.
        inspect = shlex.split(commands[1])
        self.assertEqual(inspect[:2], ["npx", "find-session"])
        transcript = run(self.home, *inspect[2:])
        first, last = (int(number) for number in inspect[-1].split(":"))
        self.assertEqual(len(re.findall(r"^\[\d+ · ", transcript, re.M)), last - first + 1)
        caption = html.unescape(re.sub(r"<[^>]+>", "", re.search(r'<text id="caption".*?</text>', svg, re.S).group(0)))
        self.assertIn(f"{last - first + 1} messages", caption)
        self.assertIn("214", caption)
        # The request above the terminal uses the words that are searched for.
        request = html.unescape(re.sub(r"<[^>]+>", "", re.search(r'<text id="request".*?</text>', svg, re.S).group(0)))
        self.assertIn("invoice export", request)

    def test_every_documented_call_is_accepted_by_the_parser(self):
        for document, at_least in ((README, 15), (SKILL, 3), (REFERENCE, 0)):
            calls = helper_lines(document.read_text())
            self.assertGreater(len(calls), at_least, document.name)
            for arguments in calls:
                if arguments[0] in {"install", "update", "uninstall"}:
                    continue  # the npx wrapper's own commands
                try:
                    cli.parser().parse_args(arguments)
                except SystemExit:
                    self.fail(f"{document.name}: not accepted: {' '.join(arguments)}")

    def test_the_paste_in_prompt_falls_back_whenever_the_command_fails(self):
        prompt = fenced_after(README.read_text(), "Paste this into Claude Code, Codex or omp")
        self.assertIn("npx -y find-session install", prompt)
        self.assertIn("If that command fails", prompt)
        self.assertNotIn("Only if Node is not installed", prompt)
        self.assertIn("scripts/install_skill.py", prompt)

    def test_documents_say_what_is_matched_and_what_is_listed(self):
        readme, skill = README.read_text(), SKILL.read_text()
        for text in (readme, skill):
            self.assertIn("diacritics", text)
            self.assertIn("only as a whole word", text)
            self.assertIn("the newest 50", text)
        self.assertNotIn("lists\nevery session", skill)
        self.assertIn("| Subagent sessions | separate ranking | separate ranking | separate ranking |", readme)
        # The promise about secrets names the sentence forms that are recognised, and what is not.
        for phrase in ("the password for staging is", "the secret is", "hasło to", "docker login", "passes"):
            self.assertIn(phrase, readme)

    def test_every_link_in_the_documents_leads_somewhere(self):
        for document in (README, REFERENCE, REPO / "CONTRIBUTING.md"):
            text = document.read_text()
            targets = re.findall(r"\]\(([^)\s]+)\)", text) + re.findall(r'<img src="([^"]+)"', text)
            self.assertTrue(targets, document.name)
            for target in targets:
                if target.startswith(("https://", "http://")):
                    continue
                path, _, anchor = target.partition("#")
                found = (document.parent / path).resolve() if path else document
                self.assertTrue(found.exists(), f"{document.name}: {target} does not exist")
                if anchor:
                    headings = re.findall(r"^#+ (.+)$", found.read_text(), re.M)
                    anchors = {re.sub(r"[^a-z0-9 -]", "", heading.lower()).replace(" ", "-") for heading in headings}
                    self.assertIn(anchor, anchors, f"{document.name}: {target}")

    def test_readme_keeps_what_other_documents_and_the_launch_rely_on(self):
        readme = README.read_text()
        # docs/RELEASING.md tells the maintainer to edit this row after the first PyPI upload.
        self.assertIn("| A command on your `PATH` |", readme)
        self.assertIn("command on your PATH", (REPO / "docs" / "RELEASING.md").read_text())
        # The install command is the first code block of the page.
        self.assertEqual(re.search(r"```[a-z]*\n(.*?)```", readme, re.S).group(1), "npx find-session install\n")
        # Only the three supported tools are named as sources.
        self.assertEqual(readme.count("| History read from |"), 1)
        for promise in (
            "no network requests",
            "Session files are never\nmodified",
            "pattern matching, not a\nguarantee",
        ):
            self.assertIn(promise, readme)

    def test_one_line_descriptions_name_the_three_tools(self):
        plugin = json.loads((REPO / ".claude-plugin" / "plugin.json").read_text())
        market = json.loads((REPO / ".claude-plugin" / "marketplace.json").read_text())
        package = json.loads((REPO / "package.json").read_text())
        pyproject = re.search(r'^description = "(.*)"$', (REPO / "pyproject.toml").read_text(), re.M).group(1)
        skill = re.search(r"^description: (.*)$", SKILL.read_text(), re.M).group(1)
        descriptions = {
            "plugin.json": plugin["description"],
            "marketplace.json": market["description"],
            "marketplace.json plugin": market["plugins"][0]["description"],
            "package.json": package["description"],
            "pyproject.toml": pyproject,
            "SKILL.md": skill,
        }
        for name, description in descriptions.items():
            for tool in ("Claude Code", "Codex", "omp"):
                self.assertIn(tool, description, name)
        # The phrases that make an agent load the skill.
        for trigger in (
            "find session",
            "what did we decide last week about",
            "szukaj sesji",
            "gdzie była rozmowa",
            # Questions about what was done, which a weaker model did not connect with "sessions".
            "which command did we use",
            "how did we do X last time",
            "run it again",
            "before grepping session folders",
        ):
            self.assertIn(trigger, skill)
        # A plain YAML scalar must not hold ": ", and a tool shows about this much of a description.
        self.assertNotIn(": ", skill)
        self.assertLess(len(skill), 1024)

    def test_skill_is_short_and_says_what_an_agent_needs(self):
        text = SKILL.read_text()
        # It is loaded into the agent's context on every use.
        self.assertLess(len(text), 5650)
        for needed in ("recent", "index", "--children", "any", "Exit code", "first", "npx find-session", "[REDACTED]"):
            self.assertIn(needed, text)
        # What the trial with fresh agents showed they need to be told.
        for needed in (
            "Judge a result by\n  its hit line, not by its title or project",
            "`inspect SELECTOR --range A:B --tools` shows it",
            "use it together with\n  `--range` or `--grep`",
            "`Next part:`",
            "only after the main results did not answer",
        ):
            self.assertIn(needed, text)
        for stale in ("Inspect:", "`Match`", "README", "Also N subagent"):
            self.assertNotIn(stale, text)


if __name__ == "__main__":
    if "--print" in sys.argv:
        with tempfile.TemporaryDirectory() as tmp:
            for name, extras, example in (
                ("SKILL.md, README.md", True, skill_example),
                ("overview.svg", False, overview_example),
            ):
                home = Path(tmp).resolve() / name.split(".")[0] / "alex"
                build(home, extras=extras)
                print(f"--- {name}\n{example(home)}")
        raise SystemExit(0)
    helpers.os.environ.setdefault("FIND_SESSION_CACHE", "off")
    unittest.main()
