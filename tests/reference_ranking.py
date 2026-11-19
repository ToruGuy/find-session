"""The scorer as it was before sessions were indexed, kept as plain as it was.

It reads a parsed `Session` and folds its text on every call: simple, slow, and
the definition of what a search returns. `test_scoring_equivalence.py` checks
the indexed scorer against it on generated sessions. When ranking changes on
purpose, change this file in the same commit (or drop it once the new
behaviour has tests of its own).

Three rules were added to the original on purpose: text and query are compared
without diacritics (`reference_fold`), a word of up to three characters counts
only as a whole word (`occurrences`), and a command counts as it always did up
to its first 400 characters, while a word found only further into it counts
for a quarter of a word (`head_and_rest`, `LATE_COVERAGE`).
"""

from __future__ import annotations

import re
import time
import unicodedata

from find_session.model import Session
from find_session.ranking import Hit


def reference_fold(text: str) -> str:
    """Lower case, canonical decomposition without its combining marks, stroke letters named."""
    decomposed = unicodedata.normalize("NFD", text.lower())
    kept = "".join(char for char in decomposed if not unicodedata.combining(char))
    return kept.replace("ł", "l").replace("ø", "o").replace("đ", "d")


def occurrences(term: str, text: str) -> int:
    """How often `term` occurs in what was said: anywhere, or as a whole word when it is short."""
    if len(term) > 3:
        return text.count(term)
    return len(re.findall(r"(?<![^\W_])" + re.escape(term) + r"(?![^\W_])", text))


def reference_searchable_text(session: Session) -> str:
    # Client/project names often live only in the working directory (for
    # example ~/clients/acme-shop), so the project path is searchable too.
    return "\n".join(
        [
            session.project,
            *session.user_messages,
            *session.delegated_messages,
            *session.assistant_messages,
            # Not the user's words, but they retell the conversation and carry
            # names and ids; leaving them out pushed relevant sessions down.
            *session.summaries,
            *(text for _, text in session.tools),
        ]
    )


# A chunk is this many consecutive messages (with the commands run between
# them). Long conversations are ranked by their best chunk, so a session that
# mentions every query word once, hundreds of messages apart, no longer beats
# one where they occur together.
CHUNK_MESSAGES = 8

# How much a word found only in a command counts against one said in the
# conversation (measured on logged searches: lower weights only lost hits once
# searches for the topic itself were left out of the commands).
TOOL_COVERAGE = 1.0


# A word found only beyond the first COMMAND_HEAD characters of a command.
LATE_COVERAGE = 0.25
COMMAND_HEAD = 400


def head_and_rest(command: str) -> tuple[str, str]:
    """The first 400 characters of a command, and what follows them, from the
    start of the word the limit falls in (looked for at most 80 characters back)."""
    if len(command) <= COMMAND_HEAD:
        return command, ""
    start = COMMAND_HEAD
    for back in range(COMMAND_HEAD, COMMAND_HEAD - 80, -1):
        if command[back - 1].isspace():
            break
        start = back - 1
    return command[:COMMAND_HEAD], command[start:]


def reference_score(
    session: Session,
    terms: list[str],
    query: str,
    *,
    chunk: int = CHUNK_MESSAGES,
    tool_coverage: float = TOOL_COVERAGE,
    use_tools: bool = True,
) -> Hit:
    haystack = reference_fold(reference_searchable_text(session))
    if not any(occurrences(term, haystack) for term in terms):
        return Hit(0)
    project = reference_fold(session.project)
    in_project = [bool(occurrences(term, project)) for term in terms]
    phrase = reference_fold(query) if len(terms) > 1 else ""
    turns = [reference_fold(text) for _, text in session.turns]
    n = len(turns)
    tools: list[list[str]] = [[] for _ in range(max(n, 1))]
    rests: list[list[str]] = [[] for _ in range(max(n, 1))]
    if use_tools:
        for after, text in session.tools:
            head, rest = head_and_rest(text)
            tools[min(max(after - 1, 0), len(tools) - 1)].append(reference_fold(head))
            rests[min(max(after - 1, 0), len(tools) - 1)].append(reference_fold(rest))
    tool_text = ["\n".join(row) for row in tools]
    rest_text = ["\n".join(row) for row in rests]
    # Per message: occurrences of each term in what was said and in commands.
    said = [[occurrences(term, text) for term in terms] for text in turns] or [[0] * len(terms)]
    # A compaction summary retells the whole conversation, so it counts for
    # the session but must not decide where the matching stretch is.
    retold = [0] * len(terms)
    for i, (role, _) in enumerate(session.turns):
        if role == "summary":
            retold = [a + b for a, b in zip(retold, said[i])]
            said[i] = [0] * len(terms)
    # In commands a word must start where the term starts ("vars" is not "myvars"),
    # and a short one must end there too.
    starts = [
        re.compile(r"(?<![^\W_])" + re.escape(term) + (r"(?![^\W_])" if len(term) <= 3 else "")) for term in terms
    ]
    ran = [
        [len(start.findall(text)) if term in text else 0 for term, start in zip(terms, starts)]
        if text
        else [0] * len(terms)
        for text in tool_text
    ]
    late = [
        [len(start.findall(text)) if term in text else 0 for term, start in zip(terms, starts)] for text in rest_text
    ]
    late_coverage = LATE_COVERAGE * tool_coverage
    said_totals = [sum(row[k] for row in said) + retold[k] for k in range(len(terms))]
    ran_totals = [sum(row[k] for row in ran) for k in range(len(terms))]
    late_totals = [sum(row[k] for row in late) for k in range(len(terms))]
    coverage = sum(
        1.0 if said_total or project_hit else tool_coverage if ran_total else late_coverage if late_total else 0.0
        for said_total, ran_total, project_hit, late_total in zip(said_totals, ran_totals, in_project, late_totals)
    )
    if not coverage:
        return Hit(0)
    with_phrase = [bool(phrase) and (phrase in a or phrase in b) for a, b in zip(turns or [""], tool_text)]

    rows = max(n, 1)
    width = rows if chunk <= 0 else min(chunk, rows)
    # Prefix sums: occurrences of term k in messages [0, i).
    said_sum = [[0] * (rows + 1) for _ in terms]
    ran_sum = [[0] * (rows + 1) for _ in terms]
    for k in range(len(terms)):
        for i in range(rows):
            said_sum[k][i + 1] = said_sum[k][i] + said[i][k]
            ran_sum[k][i + 1] = ran_sum[k][i] + ran[i][k]
    best: tuple | None = None
    for start in range(0, rows - width + 1):
        stop = start + width
        chunk_coverage = 0.0
        hits = 0.0
        for k in range(len(terms)):
            in_said = said_sum[k][stop] - said_sum[k][start]
            in_ran = ran_sum[k][stop] - ran_sum[k][start]
            if in_said or in_project[k]:
                chunk_coverage += 1
            elif in_ran:
                chunk_coverage += tool_coverage
            elif any(late[i][k] for i in range(start, stop)):
                chunk_coverage += late_coverage
            hits += min(in_said + in_ran * tool_coverage, 10)
        key = (chunk_coverage, any(with_phrase[start:stop]), hits, start)
        if best is None or key >= best:
            best = key
    chunk_coverage, has_phrase, hits, start = best
    stop = start + width
    hit_rows = [i for i in range(start, stop) if any(said[i]) or any(ran[i]) or any(late[i])] or [start]
    first, last = max(hit_rows[0] - 1, 0), min(hit_rows[-1] + 1, rows - 1)

    score = chunk_coverage * 1000 + coverage * 150 + hits * 2
    score += sum(min(a + b * tool_coverage, 20) for a, b in zip(said_totals, ran_totals)) / 2
    if has_phrase:
        score += 500
    elif phrase and any(with_phrase):
        score += 200
    score += sum(in_project) * 250
    # Within otherwise similar matches, recent work should beat an old
    # session that merely repeats the client name a few more times.
    age_days = max(0.0, (time.time() - session.mtime) / 86400)
    score += max(0, 120 - int(age_days))
    return Hit(int(score), first + 1, last + 1, chunk_coverage)
