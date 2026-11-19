## What and why

<!-- One or two sentences. Link the issue if there is one. -->

## Checklist

- [ ] The change is focused on one thing.
- [ ] `python3 -m unittest discover -s tests` passes.
- [ ] `uvx ruff check . && uvx ruff format --check .` passes.
- [ ] `node --test tests/npm/find-session.test.js` passes (if `npm/`, `package.json` or the skill folder changed).
- [ ] A line under `## [Unreleased]` in `CHANGELOG.md`; `VERSION` and the other version files are untouched.
- [ ] Tests use synthetic fixtures only: no real conversation text, paths, names, ids or tokens, here or in the description.
- [ ] `skills/find-session/SKILL.md` is updated if a command or the output changed.
- [ ] A change to what a parser extracts bumps `PARSER_VERSION` in `cache.py`.
