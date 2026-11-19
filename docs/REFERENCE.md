# Reference

The detail behind the [README](../README.md): how to call the helper, how matching works, how to
read the output, configuration and the index.

## Which command to type

Agents call the helper from the skill folder by themselves. To run it yourself, `find-session` in
the examples is whichever of these you have:

| Installed with | Type |
| --- | --- |
| `npx find-session install` | `npx find-session …`, or without Node's start-up time `python3 ~/.local/share/find-session/skills/find-session/scripts/sessions.py …` |
| a clone | `python3 <clone>/scripts/sessions.py …` |
| plugin, skills.sh, copied folder | `python3 <skill folder>/scripts/sessions.py …` |
| `uv tool install` / `pipx` | `find-session …` |

`--help` after a command lists all of its options.

## Installer options

`npx find-session install` and `scripts/install_skill.py` take the same options. `--harness claude`
(repeatable) picks tools yourself, `--no-index` leaves indexing to the first search, `--dry-run`
previews. A different skill named `find-session` is kept unless you pass `--replace`, which first
moves it to `<skills-root>/../skill-backups/`. A find-session installed another way (a clone,
skills.sh, a Claude Code plugin) is recognised and kept, so no tool ends up with two copies.

## How words are matched

The query is split into words (letters, digits, `_` and `-`), whether it is typed as one quoted
argument or as several (`search invoice export` is `search "invoice export"`); case and diacritics
are ignored (`wdrozenie` finds `wdrożenie` and the other way round). A session is listed when it contains *any*
of the words, and ranks higher the more of them occur close together; the exact phrase and a recent
date add to the rank. In messages a word also matches inside a longer word (`export` finds
`exported`); in commands only where a word starts. A word of up to three characters matches only as
a whole word, so `api` finds `API` and `api-v2` but not `capital` or `rapid`. The project path is
searched too; titles are not. The header counts, per tool, the sessions that contain all the words,
and how many more contain only some (`+1 partial`); a word that no session contains is named
(`no session has: WORD`).

## Reading a result

The first token of a row is the selector for `inspect`; ids are shortened to the shortest unique
prefix of at least 8 characters. `title:` is the name the tool gave the session (Claude Code's
generated or `/rename`d title, a Codex thread name, an omp title); a session without one shows
`first:`, its first user message. `hit A:B` is the stretch of messages where the words occur
together; read it with `inspect … --range A:B`. The line quotes the message (or `command:`) of that
stretch that has the most query words, cut around them; among messages with equally many, the one
with the words fewer sessions have, so a line that only repeats the word every result contains does
not stand for the hit. Judge a result by this line: a session with an unrelated title can hold the
answer in one stretch. The date is the
time of the last record in the session file, not the file's own modification time, and a home
directory is shown as `~`, whichever machine or user the session came from: history copied or synced
from another machine reads like local history, and `--since` / `--until` work on it.

## The session you are in

An agent names its topic before it searches, so the session it is running in would often rank
first. It is left out of the ranking and the counts and gets one line under the results
(`this session also matches: …`); `--include-current` ranks it like any other and `--json` has it
under `current_session`. It is recognised by the id the tool exports to the commands it runs:
`CLAUDE_CODE_SESSION_ID` (Claude Code; for a subagent that is its parent session) or
`CODEX_THREAD_ID` / `CODEX_SESSION_ID` (Codex). No such variable is documented for omp, so under
omp, in a plain terminal, or when both a Claude Code and a Codex id are set (one tool started from
inside the other), nothing is left out.

## Reading a conversation

`inspect` accepts a full id, a unique part of one, or a prefixed selector (`claude:`, `codex:`,
`omp:`; a Claude Code subagent by its agent id, an omp one as `omp:<id>/<Task>`). Messages are
numbered and timed. The output is limited to about 20,000 characters (`--budget N`; 0 = no limit),
what an agent's tool still shows inline instead of moving it to a file. A longer conversation keeps
its first message and its most recent ones, and a `Next part:` line gives the call that prints the
next stretch of what was left out, with the same options (`inspect SELECTOR --range 13:27 --tools`):
that stretch fits the limit, so reading on never returns a part of a part. `--range A:B`, `--tail N`
and `--grep WORDS` pick a part yourself; `--message-chars 0` lifts the per-message limit.

`--tools` adds the commands run after each message. Each is cut to 400 characters, with
`… [+N chars]` for what is not printed (around the word when `--grep` found it further in);
`--message-chars 0` prints all that is stored, which is the first 2,000 characters of a command
(`… [+N chars not kept]` marks a longer one). Use `--tools` together with `--range` or `--grep`: a
whole conversation with its commands rarely fits the output limit.

A conversation continued from another project folder is stored there again under the same id.
`inspect` opens the copy whose last record is the latest and names the other file in one line;
`search`, `index` and `recent` list the conversation once.

Exit code 1 means no such session; when an id fits several sessions it lists them, each with a
selector that opens it, and exits with code 2 instead of guessing. An option that belongs to another
command gets one line that names the call (`--tools is an option of inspect, not of search: …`).

## What is searchable

What the user and the agents said, and the commands of tool calls (the shell command, or the path,
pattern or URL argument; never the output, file contents written by a tool, or patch bodies). Of
each command the first 2,000 characters are kept: enough for nearly every chained one-liner, while
most of a long inline script stays out. The first 400 characters of a command count in the ranking
like any other text; a word that only occurs further in still lists the session and points the hit
at that command, but counts for a quarter of a word, so a long script that happens to contain the
query words does not outrank the conversation about them.
Commands that looked up the history itself (any call of this tool, reading session files) are
skipped, so a session that only searched for a topic does not rank for it. Subagent sessions (of
all three tools: for Claude Code the files under `<session>/subagents/`, with the task a subagent
was given, what it said and ran, and its report) and Codex's automatic review threads are kept out
of the main rankings; a search says how many of them have all the words and `--children` ranks
them. Context-compaction summaries are never presented as something the user said.

## Configuration

Nothing is required. Defaults read the history of the current user (`~/.claude`, `~/.codex`,
`~/.omp`).

Environment variables:

| Variable | Effect |
| --- | --- |
| `FIND_SESSION_HOME` | Directory that contains `.claude`, `.codex` and `.omp` (e.g. a copy synced from another machine). Same as `--home`. |
| `CLAUDE_SESSIONS_ROOT`, `CODEX_SESSIONS_ROOT`, `OMP_SESSIONS_ROOT` | Point one source at an exact sessions directory. |
| `PI_CODING_AGENT_DIR`, `PI_CONFIG_DIR` | omp's own overrides, honoured the same way omp does. |
| `FIND_SESSION_CACHE` | Directory of the index (default `$XDG_CACHE_HOME/find-session`, i.e. `~/.cache/find-session`), or `off` to parse everything on every run. Same as `--no-cache`. |
| `FIND_SESSION_CONFIG` | Path of the config file (default `$XDG_CONFIG_HOME/find-session/config.json`, i.e. `~/.config/find-session/config.json`). |
| `FIND_SESSION_PROGRESS` | Seconds of indexing before progress lines start on stderr (default 2), or `off`. |

Optional config file, for machine-specific additions:

```json
{
  "home": "~/mirrors/laptop",
  "resume_label": "Resume on laptop",
  "hints": [
    {"label": "Inspect on server", "command": "ssh build-box find-session inspect {selector}"}
  ],
  "recent_include": ["notes/*/state.md"]
}
```

- `home` — same as `FIND_SESSION_HOME` (the environment variable wins).
- `resume_label` — label of the resume line (in `inspect` and `--full`), useful when the history
  was synced from another computer and the command has to run there.
- `hints` — extra lines with a command per session. `inspect` prints them filled in; the compact
  search output prints each template once under its header, `--full` under every result, and
  `--json` adds them as `hints`. Placeholders: `{selector}`, `{id}`, `{source}`, `{cwd}`, `{resume}`.
- `recent_include` — files (globs relative to `home`) appended to `recent` output, e.g. project
  state notes for a daily brief. Also available as `recent --include GLOB`.

## How a search stays fast

History is parsed once into an index in `~/.cache/find-session`: one SQLite file with, per session
file, a few facts, the text that queries are matched against (lower case, without diacritics), and
the parsed conversation. `install` builds it; otherwise the first search does (or `build-index`).
That first pass reads everything: a minute or two for several gigabytes. It reports its progress on
stderr (stdout stays parseable), saves what it has every few seconds, and when it is interrupted
(Ctrl-C, an agent's command timeout) the next run continues where it stopped. After that a search

- lists the session files and compares size and modification time with the index, so a new or
  changed file is always picked up by the very next search;
- parses only what changed, and for a Claude Code or Codex file that has only grown (the
  conversation you are in), only the lines added since the last search;
- looks for the query words in the stored text, ranks, and reads the full conversation only for the
  few sessions it prints.

Changed sessions are written one row at a time, so several agents can search at once. Results are
the same with and without the index (`--no-cache` parses everything and is the reference). The
index is rebuilt automatically when the parser, the Python version or the file itself is not as
expected.

## Development

```bash
python3 -m unittest discover -s tests          # synthetic fixtures only
uvx ruff check . && uvx ruff format --check .  # lint and format
node --test tests/npm/find-session.test.js     # the npx wrapper
```

[AGENTS.md](../AGENTS.md) describes the layout and the rules the code keeps,
[CONTRIBUTING.md](../CONTRIBUTING.md) how to contribute, and [RELEASING.md](RELEASING.md) how a
release is made.
