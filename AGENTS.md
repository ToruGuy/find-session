# Notes for agents working on this repo

## Layout

- `skills/find-session/` — the skill, self-contained (it must keep working when only this folder is copied):
  - `SKILL.md` — what agents read. Keep it in sync with every CLI or output change.
  - `scripts/sessions.py` — entry point; `scripts/find_session/` — the package (standard library only, Python 3.9+):
    - `parsers/claude.py`, `parsers/codex.py`, `parsers/omp.py`, `parsers/agy.py` (Antigravity CLI) — one parser per harness; each turns a session file into a `Session`.
    - `model.py` (`Session`, `Candidate`), `jsonl.py`, `text.py`, `tools.py` (tool call → one searchable line), `redact.py` (secrets), `titles.py` (cleaning a title; Codex thread names; agy's summaries database).
    - `discovery.py` (where history lives), `lookup.py` (selectors, subagents).
    - `cache.py` (`Entry`: the few facts about a session that listing and ranking need, with the conversation loaded on demand; parsing), `index.py` (the SQLite index behind it), `searchtext.py` (`SearchText`: a session's text as stored and searched, folded to lower case without diacritics; `fold` and the whole-word rule for short words live there).
    - `ranking.py` (score and best stretch), `timefilter.py`, `search.py`, `transcript.py` (`inspect`), `recent.py`, `output.py`, `cli.py`.
- `scripts/sessions.py` — short path to the entry point from a clone; `scripts/install_skill.py` — links the skill into the harnesses from a clone; `scripts/release.py` — proposes and stamps the version.
- `package.json` + `npm/find-session.js` — the npm package behind `npx find-session` (zero dependencies, Node 18+). It ships `skills/find-session/` as it is; `install`/`update`/`uninstall` copy the skill to `~/.local/share/find-session` and link it, every other command runs the Python helper. It is not in `bin/`: a plugin's root `bin/` is offered to Claude Code as a binary. Its link rules are those of `install_skill.py`: `tests/install_cases.json` is run against both installers, so change both together and add a case. It also knows the helper's command names (`cli.COMMANDS`) and the names of the index files (`cache.cache_path`, `index.COMPANIONS`); tests fail when either drifts.
- `.claude-plugin/` — Claude Code plugin and marketplace manifests.
- `.github/workflows/` — `tests.yml` (lint, Python and Node tests, package install), `release.yml` (tag → GitHub Release, opt-in npm/PyPI; see `docs/RELEASING.md`).
- `tests/` — one file per area, shared synthetic fixtures in `tests/helpers.py`; `tests/npm/` for the npx wrapper. `tests/test_docs.py` holds the example history behind the output shown in `README.md`, `SKILL.md` and `assets/overview.svg`, and fails when they differ from real output (`python3 tests/test_docs.py --print` shows it); it also checks every documented call, every link and the one-line descriptions.
- `README.md` is for a first-time reader and stays short; detail goes to `docs/REFERENCE.md`.

## Rules

- Session files are read-only input. Never write to them.
- Never index tool output, file contents written by tools or reasoning. All text entering a `Session` goes through `Session.add` / `Session.add_tool`, which redact secrets; do not bypass them. A title goes through `titles.clean_title`.
- A command is stored up to `tools.MAX_TOOL_TEXT` characters, cut after redaction. How much was left out is kept apart from the text (`Session.tool_cuts`), so the number is never searched; `inspect` cuts once more for display (`transcript.shown_command`) and marks either cut as `… [+N chars]`. In `SearchText` the first `COMMAND_HEAD` characters of a command are the command segment the scorer has always seen; what follows goes into segments of its own after the regular ones and counts for `ranking.LATE_COVERAGE` of a word: counted in full, it lowered relevance on the logged searches.
- Two files with the same source, kind and id are copies of one conversation (a session continued from another project folder). `cache.latest_copies` keeps the one whose last record is the latest, not the newer file, for `search`, `index`, `recent` and `inspect`; the index holds both. A list of choices for an ambiguous id never names a session twice, and every selector in it opens a session.
- What `inspect` prints by default has to fit what a harness shows inline (`model.MAX_INSPECT_TOTAL`); what is left out is named with the call that prints the next part, and that part fits too.
- Arguments a command does not take are answered in one line that names the call that was meant (`cli.misplaced`), without the usage text.
- Everything that compares a query word with text uses `searchtext.fold` on both sides and `find_word` (or `SearchText.whole_words` on stored bytes) for the word rule: scoring, the snippet, `inspect --grep`, the header counts. What is printed is never folded.
- A session's `mtime` is the time of its last record (`cache.date_from_records`), not the file's. Whether a file changed is decided by the index from the file's own size and mtime, which are stored apart.
- A Claude Code record whose `type` starts with `api-request` is a copy of what was sent to the model, not conversation: `parsers/claude.py` skips it.
- A session's title is shown, never searched or scored: it stays out of `turns` and `SearchText`. A title kept outside the session file (Codex, agy) is not stored with the parsed session either: the index keeps it in `titles`, keyed by the titles file, and rereads that file when its size or mtime changes (for agy's SQLite database, together with its `-wal` file). agy's database is opened read-only, as an immutable file when no `-wal` exists, so reading it creates nothing in agy's directory.
- The calling session (`search.calling_session`) is taken only from a variable a harness is known to export, and matched on source and exact id. Do not add a guess (recent files, process names): hiding another session is worse than listing the caller.
- Fixtures must stay synthetic: never paste real session content, real paths, names, ids or hashes into tests. Secret-shaped test values are assembled at run time (`helpers.fake`).
- A change to what a parser extracts needs a `PARSER_VERSION` bump in `cache.py`, and a change to the index tables or to `SearchText` an `INDEX_SCHEMA` bump in `index.py`, so indexes rebuild.
- Search has three separate steps; keep them apart. Retrieval: `index.py` hands out entries and their `SearchText` and knows nothing about scores. Counting: `ranking.count_terms` finds where the query words occur. Scoring: `ranking.score_counts` is the only place that knows the weights. `--no-cache` runs the same counting and scoring on freshly parsed sessions, and tests compare the two paths.
- `tests/reference_ranking.py` is the scorer as it was before the index, kept as the definition of the results; `test_scoring_equivalence.py` checks the current scorer against it. Change both together when ranking changes on purpose.
- Speed is a feature (agents call this many times per task): a search served from the index must not import parsers, redaction, `json` or `dataclasses`, and must not read a conversation it does not print.
- Default search output is read by agents that pay per token: every added character needs a reason. No command prints an unbounded list by default (`index`, `recent`, the subagents of a session, the candidates of an ambiguous id).
- stdout is the result and stays parseable; progress and warnings go to stderr. `SKILL.md` is loaded into an agent's context on every use: keep it under the size `tests/test_docs.py` allows.
- A command that calls this tool is not indexed (`tools.HISTORY_LOOKUP`). A new way of calling it (another command name, another entry point) has to be added there.

## Checks

```bash
python3 -m unittest discover -s tests
uvx ruff check . && uvx ruff format --check .
node --test tests/npm/find-session.test.js
```

## Versioning

PRs add a readable line under `## [Unreleased]` in `CHANGELOG.md` and do not touch `VERSION`. The number is stamped on `main` at release (SemVer; pre-1.0 a breaking change bumps MINOR) by `python3 scripts/release.py --apply X.Y.Z`, in four places a test keeps equal: `VERSION`, `skills/find-session/VERSION`, `.claude-plugin/plugin.json` and `package.json`. The running version is shown by `sessions.py --version`. Releasing: `docs/RELEASING.md`.
