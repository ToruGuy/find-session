"""Score a session for a query and locate the best stretch of messages."""

from __future__ import annotations

import re
import time
from itertools import accumulate, repeat

from .model import Session
from .searchtext import SearchText, encode, find_word, fold, is_short, unfolded_span
from .text import local_time


def query_terms(query: str) -> list[str]:
    """The words of a query as they are looked for: folded like the stored text."""
    return re.findall(r"[\w-]+", fold(query))


def searchable_text(session: Session) -> str:
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
    ).lower()


# A chunk is this many consecutive messages (with the commands run between
# them). Long conversations are ranked by their best chunk, so a session that
# mentions every query word once, hundreds of messages apart, no longer beats
# one where they occur together.
CHUNK_MESSAGES = 8

# How much a word found only in a command counts against one said in the
# conversation (measured on logged searches: lower weights only lost hits once
# searches for the topic itself were left out of the commands).
TOOL_COVERAGE = 1.0

# The same for a word found only far into a long command (beyond
# `searchtext.COMMAND_HEAD` characters). Such a word lists the session, moves
# the hit to that command and counts for this much of a word that was said.
# Measured on 70 logged searches: counted in full, long inline scripts pushed
# the session that was opened down in 4 and up in 1; at a half and below, the
# ranks were those of an index without the later parts.
LATE_COVERAGE = 0.25


class Hit:
    """Score of one session; `first`..`last` are the message numbers (1-based,
    inclusive) around the words in the best chunk. `found` says, per query
    word, whether the session has it anywhere: it is reported, not scored."""

    __slots__ = ("score", "first", "last", "chunk_coverage", "found")

    def __init__(self, score: int, first: int = 0, last: int = 0, chunk_coverage: float = 0.0):
        self.score = score
        self.first = first
        self.last = last
        self.chunk_coverage = chunk_coverage
        self.found: tuple = ()

    def _key(self) -> tuple:
        return (self.score, self.first, self.last, self.chunk_coverage)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Hit) and self._key() == other._key()

    def __repr__(self) -> str:
        return f"Hit(score={self.score}, first={self.first}, last={self.last}, chunk_coverage={self.chunk_coverage})"


def score_session(
    session: Session,
    terms: list[str],
    query: str,
    *,
    chunk: int = CHUNK_MESSAGES,
    tool_coverage: float = TOOL_COVERAGE,
    use_tools: bool = True,
) -> Hit:
    return score_text(
        SearchText.of(session),
        session.mtime,
        terms,
        query,
        chunk=chunk,
        tool_coverage=tool_coverage,
        use_tools=use_tools,
    )


def score_text(
    text: SearchText,
    mtime: float,
    terms: list[str],
    query: str,
    *,
    chunk: int = CHUNK_MESSAGES,
    tool_coverage: float = TOOL_COVERAGE,
    use_tools: bool = True,
) -> Hit:
    """Score a session from its stored search text.

    Two steps, kept apart on purpose: `count_terms` finds where the words
    occur (the only step that reads text), `score_counts` turns those counts
    into a score (the only step that knows the weights).
    """
    phrase = fold(query) if len(terms) > 1 else ""
    counts = count_terms(text, terms, phrase, use_tools=use_tools)
    if counts is None:
        return Hit(0)
    hit = score_counts(counts, mtime, bool(phrase), chunk=chunk, tool_coverage=tool_coverage)
    hit.found = counts.found
    return hit


class Counts:
    """Where the query words occur in one session.

    `said[k][i]` and `ran[k][i]` are the occurrences of word k in message i
    and in the commands run after it; `retold[k]` those in compaction
    summaries; `in_project[k]` whether the project path has the word;
    `with_phrase[i]` whether row i has the whole query as one phrase;
    `found[k]` whether word k occurs anywhere in the session's text;
    `late[k]` like `ran[k]` for the later parts of long commands, or None when
    the word is in none of them.
    """

    __slots__ = ("rows", "in_project", "said", "ran", "retold", "with_phrase", "found", "late")

    def __init__(self, rows, in_project, said, ran, retold, with_phrase, found=(), late=None):
        self.rows = rows
        self.in_project = in_project
        self.said = said
        self.ran = ran
        self.retold = retold
        self.with_phrase = with_phrase
        self.found = found
        self.late = late if late is not None else [None] * len(said)


def count_terms(text: SearchText, terms: list[str], phrase: str, *, use_tools: bool = True) -> Counts | None:
    """Occurrences of each word per message; None when no word occurs at all.

    A word of more than SHORT_WORD characters is found anywhere in what was
    said, also inside a longer word, and in a command only where a word
    starts. A shorter one counts only as a whole word, in both.
    """
    needles = [encode(term) for term in terms]
    low = text.low
    # Most sessions contain none of the words: nothing else is done for them.
    firsts = [low.find(needle) for needle in needles]
    if max(firsts) < 0:
        return None
    short = [is_short(term) for term in terms]
    found = [first >= 0 and not whole for first, whole in zip(firsts, short)]
    rows = text.rows
    offsets = text.offsets
    in_project = [False] * len(terms)
    said: list[list[int]] = []
    ran: list[list[int]] = []
    late: list[list[int] | None] = []
    retold: list[int] = []
    # Segments after this one are the later parts of long commands.
    regular = 2 * rows
    with_phrase = [False] * rows
    # The phrase is the query as typed, so it normally contains each word: it
    # can then only be where one of them is, and is looked for just there
    # instead of in the whole text once more.
    phrase_needle = encode(phrase)
    # A short word will not do: it is looked for as a whole word, the phrase is not.
    anchor = (
        next((k for k, needle in enumerate(needles) if not short[k] and needle in phrase_needle), -1) if phrase else -1
    )
    for k, needle in enumerate(needles):
        said_k = [0] * rows
        ran_k = [0] * rows
        late_k: list[int] | None = None
        if firsts[k] < 0:
            occurrences = ()
        elif short[k]:
            occurrences = text.whole_words(needle)
        else:
            occurrences = _anywhere(text, needle, firsts[k])
        for index, count in occurrences:
            found[k] = True
            if index == 0:
                in_project[k] = True
                continue
            if index > regular:
                if use_tools and count:
                    if late_k is None:
                        late_k = [0] * rows
                    late_k[text.late[index - regular - 1]] = count
                continue
            if index & 1:
                row = index >> 1
                said_k[row] = count
            elif use_tools:
                row = (index >> 1) - 1
                ran_k[row] = count
            else:
                continue
            if (
                k == anchor
                and not with_phrase[row]
                and low.find(phrase_needle, offsets[index], offsets[index + 1] - 1) >= 0
            ):
                with_phrase[row] = True
        # A compaction summary retells the whole conversation, so it counts for
        # the session but must not decide where the matching stretch is.
        retold_k = 0
        for row in text.summaries:
            retold_k += said_k[row]
            said_k[row] = 0
        said.append(said_k)
        ran.append(ran_k)
        late.append(late_k)
        retold.append(retold_k)
    if phrase and anchor < 0:
        for index, _, _ in text.segments_with(phrase_needle):
            if index > regular:
                break
            if index & 1:
                with_phrase[index >> 1] = True
            elif index and use_tools:
                with_phrase[(index >> 1) - 1] = True
    if not any(found):
        return None
    return Counts(rows, in_project, said, ran, retold, with_phrase, tuple(found), late)


def has_every_word(text: SearchText, terms: list[str]) -> bool:
    """Whether the session has all the query words: a quick no for nearly every
    session, without counting anything. Looking stops at the first word that is
    missing, the longest words first (they are the rarest)."""
    for term in sorted(terms, key=len, reverse=True):
        if not text.has_word(encode(term), is_short(term)):
            return False
    return True


def _anywhere(text: SearchText, needle: bytes, first: int):
    """(segment index, occurrences) of a long word: anywhere in what was said, and
    in a command only where a word starts ("port" is not found in "export" there)."""
    low, offsets = text.low, text.offsets
    regular = 2 * text.rows
    for index, position, end in text.segments_with(needle, first):
        if index & 1 and index <= regular:
            yield index, low.count(needle, position, end)
        elif index:
            yield index, text.count_word_starts(needle, position, offsets[index], end)
        else:
            yield 0, 1


def score_counts(
    counts: Counts,
    mtime: float,
    has_phrase_query: bool,
    *,
    chunk: int = CHUNK_MESSAGES,
    tool_coverage: float = TOOL_COVERAGE,
) -> Hit:
    rows, in_project, said, ran = counts.rows, counts.in_project, counts.said, counts.ran
    with_phrase = counts.with_phrase
    late = counts.late
    late_coverage = LATE_COVERAGE * tool_coverage
    said_sums = [sum(said_k) for said_k in said]
    said_totals = [said_sum + retold_k for said_sum, retold_k in zip(said_sums, counts.retold)]
    ran_totals = [sum(ran_k) for ran_k in ran]
    coverage = sum(
        1.0 if said_total or project_hit else tool_coverage if ran_total else late_coverage if late_k else 0.0
        for said_total, ran_total, project_hit, late_k in zip(said_totals, ran_totals, in_project, late)
    )
    if not coverage:
        return Hit(0)

    width = rows if chunk <= 0 else min(chunk, rows)
    starts = rows - width + 1
    if starts == 1:
        # A short conversation is one stretch.
        start = 0
        chunk_coverage = 0.0
        hits = 0.0
        for said_sum, ran_total, project_hit, late_k in zip(said_sums, ran_totals, in_project, late):
            if said_sum or project_hit:
                chunk_coverage += 1
            elif ran_total:
                chunk_coverage += tool_coverage
            elif late_k:
                chunk_coverage += late_coverage
            hits += min(said_sum + ran_total * tool_coverage, 10)
        has_phrase = any(with_phrase)
    else:
        # Every stretch of `width` consecutive messages is rated; all stretches
        # are computed at once, one list per quantity, indexed by the first message.
        chunk_coverages = [0.0] * starts
        chunk_hits = [0.0] * starts
        for said_k, ran_k, late_k, project_hit, said_sum, ran_total in zip(
            said, ran, late, in_project, said_sums, ran_totals
        ):
            if project_hit:
                chunk_coverages = [value + 1 for value in chunk_coverages]
            elif late_k:
                # Far into a long command: it counts where nothing nearer the start of one does.
                in_late = _window_sums(late_k, width)
                if said_sum or ran_total:
                    near = [a or b for a, b in zip(_window_sums(said_k, width), _window_sums(ran_k, width))]
                    in_late = [0 if a else c for a, c in zip(near, in_late)]
                chunk_coverages = [value + late_coverage if c else value for value, c in zip(chunk_coverages, in_late)]
            if not (said_sum or ran_total):
                # The word is in no message or command (at most in a summary
                # or the project path): it adds nothing more to any stretch.
                continue
            in_said = _window_sums(said_k, width)
            if ran_total:
                in_ran = _window_sums(ran_k, width)
                if not project_hit:
                    chunk_coverages = [
                        value + 1 if a else value + tool_coverage if b else value
                        for value, a, b in zip(chunk_coverages, in_said, in_ran)
                    ]
                chunk_hits = [
                    value + min(a + b * tool_coverage, 10) for value, a, b in zip(chunk_hits, in_said, in_ran)
                ]
            else:
                # The word was only said, never run: the same sums, written without the commands.
                if not project_hit:
                    chunk_coverages = [value + 1 if a else value for value, a in zip(chunk_coverages, in_said)]
                chunk_hits = [value + (a if a < 10 else 10) for value, a in zip(chunk_hits, in_said)]
        phrase_in_chunk = map(bool, _window_sums(with_phrase, width)) if any(with_phrase) else repeat(False)
        # The best stretch: most words, then the phrase, then most occurrences, then the latest.
        chunk_coverage, has_phrase, hits, start = max(zip(chunk_coverages, phrase_in_chunk, chunk_hits, range(starts)))
    stop = start + width
    # Rows of the stretch in which any word occurs.
    columns = said + ran + [late_k for late_k in late if late_k]
    hit_rows = [
        start + offset for offset, found in enumerate(zip(*(column[start:stop] for column in columns))) if any(found)
    ] or [start]
    first, last = max(hit_rows[0] - 1, 0), min(hit_rows[-1] + 1, rows - 1)

    score = chunk_coverage * 1000 + coverage * 150 + hits * 2
    score += sum(min(a + b * tool_coverage, 20) for a, b in zip(said_totals, ran_totals)) / 2
    if has_phrase:
        score += 500
    elif has_phrase_query and any(with_phrase):
        score += 200
    score += sum(in_project) * 250
    # Within otherwise similar matches, recent work should beat an old
    # session that merely repeats the client name a few more times.
    age_days = max(0.0, (time.time() - mtime) / 86400)
    score += max(0, 120 - int(age_days))
    return Hit(int(score), first + 1, last + 1, chunk_coverage)


def _window_sums(values: list, width: int) -> list[int]:
    """Sum of every `width` consecutive values, by first index."""
    sums = [0, *accumulate(values)]
    return [high - low for low, high in zip(sums, sums[width:])]


def match_snippet(
    session: Session,
    terms: list[str],
    query: str,
    width: int = 160,
    span: tuple[int, int] | None = None,
    rarity: list[float] | None = None,
) -> dict | None:
    """The message (or command) that best explains the hit, cut around the match.

    `span` limits the choice to message numbers first..last (the best chunk).
    The message with the most query words wins. Among equals the one with the
    rarer words: `rarity` weighs each word (a search passes how few sessions
    have it; without it, how few messages of the stretch do), so a line that
    only repeats the word every result has does not stand for the hit. Then
    the latest one, because that is usually where the topic ended up.
    Compaction summaries and commands lose ties against what was said.
    """
    low, high = span or (1, len(session.turns))
    candidates = [
        (number, role, text) for number, (role, text) in enumerate(session.turns, start=1) if low <= number <= high
    ]
    candidates += [(max(after, 1), "tool", text) for after, text in session.tools if low <= max(after, 1) <= high]
    phrase = fold(query)
    rows = []
    for number, role, text in candidates:
        folded = fold(text)
        has = [find_word(folded, term) >= 0 for term in terms]
        if any(has):
            rows.append((number, role, text, folded, has))
    if not rows:
        return None
    if rarity is None:
        rarity = [1 / (1 + sum(row[4][k] for row in rows)) for k in range(len(terms))]
    best: tuple[tuple[float, float, int], int] | None = None
    for index, (number, role, _, folded, has) in enumerate(rows):
        coverage: float = sum(has) + (2 if phrase in folded else 0)
        if role == "tool":
            coverage -= 0.5  # only when nothing that was said covers as much
        elif role == "summary":
            coverage -= 1.5
        key = (coverage, sum(weight for weight, there in zip(rarity, has) if there), number)
        if coverage > 0 and (best is None or key >= best[0]):
            best = (key, index)
    if best is None:
        return None
    number, role, text, folded, _ = rows[best[1]]
    hit = folded.find(phrase)
    hit_end = hit + len(phrase)
    if hit < 0:
        hit, hit_end = densest_words(folded, terms, rarity, width)
    hit, hit_end = unfolded_span(text, hit, hit_end)
    stamp = session.times[number - 1] if 0 < number <= len(session.times) else ""
    moment = local_time(stamp)
    return {
        "n": number,
        "role": role,
        "time": moment.isoformat(timespec="minutes") if moment else None,
        "text": window_of_words(text, hit, hit_end, width),
    }


# Occurrences of one word that are looked at when a window is placed.
MAX_WORD_PLACES = 20


def densest_words(folded: str, terms: list[str], rarity: list[float], width: int) -> tuple[int, int]:
    """Where to cut a long text: (start, end) of the query words that fit into
    `width` characters together, the most of them, then the rarest, then the first."""
    places: list[tuple[int, int, int]] = []
    for k, term in enumerate(terms):
        position = find_word(folded, term)
        seen = 0
        while position >= 0 and seen < MAX_WORD_PLACES:
            places.append((position, position + len(term), k))
            seen += 1
            position = find_word(folded, term, position + len(term))
    places.sort()
    best: tuple[tuple[int, float], int, int] | None = None
    for index, (start, end, _) in enumerate(places):
        inside: dict[int, float] = {}
        last = end
        for other_start, other_end, k in places[index:]:
            if other_start - start >= width:
                break
            if other_end - start <= width:
                inside[k] = rarity[k]
                last = max(last, other_end)
        key = (len(inside), sum(inside.values()))
        if best is None or key > best[0]:
            best = (key, start, last)
    return (best[1], best[2]) if best else (0, 0)


def window_of_words(text: str, hit: int, hit_end: int, width: int) -> str:
    """At most `width` characters of `text` around `text[hit:hit_end]`, cut between words.

    The window starts a third of its width before the match, as it always did
    (later when the match, several words apart, needs the room: it is centred
    then), and is then moved to the next word start and shortened to the last
    whole word, so no word is shown in half. The match itself always stays
    inside: when it lies in one long unbroken token, that token is cut, a path
    between two of its parts ("…/reports/export.py", not "…ts/export.py").
    """
    if len(text) <= width:
        return text
    start = max(0, hit - min(width // 3, max(0, width - (hit_end - hit)) // 2))
    if start and not text[start - 1].isspace():
        position = start
        while position < hit and not text[position].isspace():
            position += 1
        if position < hit:
            start = position + 1
        elif text[start - 1] != "/":
            slash = text.find("/", start, hit)
            if slash >= 0:
                start = slash
    end = min(len(text), start + width)
    if end < len(text) and not text[end].isspace():
        position = text.rfind(" ", start, end)
        if position >= hit_end:
            end = position
        elif text[end - 1] != "/":
            slash = text.rfind("/", hit_end, end)
            if slash >= 0:
                end = slash + 1
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{text[start:end].strip()}{suffix}"
