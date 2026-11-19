# Contributing

Issues and pull requests are welcome.

1. Fork, branch, and keep the change focused.
2. Run the checks:
   ```bash
   python3 -m unittest discover -s tests
   uvx ruff check . && uvx ruff format --check .
   node --test tests/npm/find-session.test.js   # when npm/, package.json or the skill folder changed
   ```
3. Add a line under `## [Unreleased]` in `CHANGELOG.md`; do not change `VERSION` or the other version
   files (the number is stamped at release, see [docs/RELEASING.md](docs/RELEASING.md)).
4. Tests use synthetic fixtures only. Never include real conversation text, paths, names, ids or
   tokens from your own history, not even in an issue: describe the shape instead.

Adding support for another harness means one new module in `skills/find-session/scripts/find_session/parsers/`, an entry in
`discovery.py`, and fixtures in `tests/helpers.py`. Open an issue first with a description of the
on-disk format (where sessions live, how user and assistant turns and tool calls are recorded).

`AGENTS.md` describes the layout and the rules the code keeps.

Optional: `git config core.hooksPath .githooks` turns on a pre-commit hook that reminds you (it never
blocks) when code changes without a `CHANGELOG.md` line.
