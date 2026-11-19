"""The searchable text of one session, in the form the index stores and the scorer reads.

Everything a query is matched against is folded once (`fold`: lower case, no
diacritics), when a session is parsed, and kept as UTF-8 bytes: a search then
neither folds nor decodes text, it only looks for byte strings. That is exact,
not an approximation: UTF-8 is self-synchronising, so a folded query word
occurs in the bytes exactly where it occurs in the folded text, and
non-overlapping counts agree too.
"""

from __future__ import annotations

import marshal
import re
from bisect import bisect_right

from .model import Session

# Lone surrogates (from "\\ud800" in a JSON record) must survive the round trip.
_ENCODING = ("utf-8", "surrogatepass")


def encode(text: str) -> bytes:
    return text.encode(*_ENCODING)


_NON_ASCII = re.compile(r"[^\x00-\x7f]")

# Character -> what it is searched as, worked out the first time a character is
# met. Letters with a stroke have no decomposition: they are named here.
_FOLDED: dict[str, str] = {"ł": "l", "ø": "o", "đ": "d"}


def _folded_char(char: str) -> str:
    # Imported here: most searches and many sessions are plain ASCII.
    import unicodedata

    # The base letter: the canonical decomposition without its combining marks.
    folded = "".join(c for c in unicodedata.normalize("NFD", char) if not unicodedata.combining(c))
    _FOLDED[char] = folded
    return folded


def fold(text: str) -> str:
    """`text` as it is matched: lower case, diacritics dropped ("Wdrożenie" -> "wdrozenie").

    Stored text and query words both go through here, so a word typed with or
    without its diacritics finds both spellings. What is printed is never
    folded.
    """
    text = text.lower()
    if text.isascii():
        return text
    # A text has few different non-ASCII characters, and replacing each of
    # them in one pass is faster than looking every character up.
    for char in set(_NON_ASCII.findall(text)):
        folded = _FOLDED.get(char)
        if folded is None:
            folded = _folded_char(char)
        if folded != char:
            text = text.replace(char, folded)
    return text


def unfolded_span(text: str, start: int, end: int) -> tuple[int, int]:
    """Where `fold(text)[start:end]` lies in `text` itself.

    The same positions nearly always: folding changes the length only for a
    character that folds to none or to several (a combining mark, "İ").
    """
    if len(fold(text)) == len(text):
        return start, end
    positions = []
    done = 0
    wanted = [start, end]
    for index, char in enumerate(text):
        size = len(fold(char))
        while wanted and size and done >= wanted[0]:
            positions.append(index)
            wanted.pop(0)
        done += size
    positions += [len(text)] * len(wanted)
    return positions[0], positions[1]


# A query word of up to this many characters matches only as a whole word:
# "api" is found in "API-ego" and "api_v2", not in "capital" or "rapid".
# Longer words are found inside other words too ("export" in "exported"),
# which is what makes inflected forms findable without a stemmer.
SHORT_WORD = 3


def is_short(term: str) -> bool:
    return len(term) <= SHORT_WORD


_WORD_PATTERNS: dict[str, re.Pattern] = {}


def find_word(folded: str, term: str, start: int = 0) -> int:
    """Where `term` is first matched in `folded` text (-1: nowhere), by the rule for its length."""
    if not is_short(term):
        return folded.find(term, start)
    pattern = _WORD_PATTERNS.get(term)
    if pattern is None:
        # [^\W_] is a letter or a digit of any script.
        pattern = _WORD_PATTERNS[term] = re.compile(r"(?<![^\W_])" + re.escape(term) + r"(?![^\W_])")
    found = pattern.search(folded, start)
    return found.start() if found else -1


_BYTE_PATTERNS: dict[bytes, tuple[re.Pattern, re.Pattern, re.Pattern]] = {}


def _byte_patterns(needle: bytes) -> tuple[re.Pattern, re.Pattern, re.Pattern]:
    """The whole-word rule on UTF-8 bytes of folded text, in three patterns.

    `anywhere`: no ASCII letter or digit on either side (text is lower case).
    `plain`: both neighbours are ASCII, so that is the whole answer. `mixed`:
    a neighbour is a non-ASCII character, which has to be decoded to know
    whether it is a letter. `anywhere` is exactly `plain` or `mixed`.

    Each pattern begins with the word itself and looks back afterwards
    (`word(?<![a-z]word)`): a pattern that begins with literal text is searched
    for many times faster than one that begins with a look-behind.
    """
    patterns = _BYTE_PATTERNS.get(needle)
    if patterns is None:
        word = re.escape(needle)
        patterns = _BYTE_PATTERNS[needle] = (
            re.compile(word + rb"(?<![0-9a-z]" + word + rb")(?![0-9a-z])"),
            re.compile(word + rb"(?<![0-9a-z\x80-\xff]" + word + rb")(?![0-9a-z\x80-\xff])"),
            re.compile(
                word + rb"(?:(?<=[\x80-\xff]" + word + rb")(?![0-9a-z])"
                rb"|(?<![0-9a-z]" + word + rb")(?=[\x80-\xff]))"
            ),
        )
    return patterns


# A command counts in the ranking like any other text up to this many
# characters: its beginning says what was run. What follows in a long one (the
# later steps of a chain, the body of an inline script) is searched as well,
# but kept apart, because it counts for less (`ranking.LATE_COVERAGE`).
COMMAND_HEAD = 400

# The later part starts at a word: this far back from the limit at most, so a
# word of up to this length is whole in one of the two parts.
COMMAND_OVERLAP = 80


def later_part(text: str) -> str:
    """What a command has beyond COMMAND_HEAD characters, from the start of the
    word the limit falls in; "" for a short command."""
    if len(text) <= COMMAND_HEAD:
        return ""
    start = COMMAND_HEAD
    while start > COMMAND_HEAD - COMMAND_OVERLAP and not text[start - 1].isspace():
        start -= 1
    return text[start:]


class SearchText:
    """Folded text of a session, cut into segments.

    Segment 0 is the project path. Each message row i then has two segments:
    1 + 2i is what was said in message i, 2 + 2i the commands run after it
    (the first COMMAND_HEAD characters of each, joined with a newline, as the
    scorer has always seen them). A session without messages still has one
    row, for commands run before any message. After those come the later
    parts of long commands, one segment per row that has any: segment
    2 * rows + 1 + j belongs to row `late[j]`.

    `low` is the segments joined with "\\n"; `offsets[j]` is where segment j
    starts and `offsets[-1]` is one past the end, so segment j is
    `low[offsets[j] : offsets[j + 1] - 1]`. Query words never contain a
    newline, so a word found anywhere in `low` lies inside one segment.
    """

    __slots__ = ("low", "offsets", "rows", "summaries", "late")

    def __init__(
        self, low: bytes, offsets: list[int], rows: int, summaries: tuple[int, ...], late: tuple[int, ...] = ()
    ):
        self.low = low
        self.offsets = offsets
        self.rows = rows
        # Rows that are compaction summaries.
        self.summaries = summaries
        # Rows whose commands have a later part, in the order of their segments.
        self.late = late

    @classmethod
    def of(cls, session: Session) -> SearchText:
        turns = session.turns
        rows = max(len(turns), 1)
        commands: list[list[str]] = [[] for _ in range(rows)]
        later: dict[int, list[str]] = {}
        for after, text in session.tools:
            row = min(max(after - 1, 0), rows - 1)
            commands[row].append(fold(text[:COMMAND_HEAD]))
            rest = later_part(text)
            if rest:
                later.setdefault(row, []).append(fold(rest))
        segments = [encode(fold(session.project))]
        for row in range(rows):
            segments.append(encode(fold(turns[row][1])) if row < len(turns) else b"")
            segments.append(encode("\n".join(commands[row])))
        late = tuple(sorted(later))
        segments.extend(encode("\n".join(later[row])) for row in late)
        offsets = [0]
        position = 0
        for segment in segments:
            position += len(segment) + 1
            offsets.append(position)
        summaries = tuple(row for row, (role, _) in enumerate(turns) if role == "summary")
        return cls(b"\n".join(segments), offsets, rows, summaries, late)

    def layout(self) -> bytes:
        """Everything but the text, for storage next to it."""
        return marshal.dumps((self.rows, self.summaries, self.offsets, self.late))

    @classmethod
    def stored(cls, layout: bytes, low: bytes) -> SearchText:
        """From the index; ValueError when the two do not belong together."""
        try:
            rows, summaries, offsets, late = marshal.loads(layout)
        except (TypeError, EOFError) as error:
            raise ValueError(f"unreadable search text layout ({error})") from None
        if not (
            isinstance(rows, int)
            and isinstance(summaries, tuple)
            and isinstance(offsets, list)
            and isinstance(late, tuple)
        ):
            raise ValueError("unexpected search text layout")
        if len(offsets) != 2 * rows + 2 + len(late) or offsets[-1] != len(low) + 1:
            raise ValueError("search text layout does not fit its text")
        return cls(low, offsets, rows, summaries, late)

    def segment(self, index: int) -> str:
        return self.low[self.offsets[index] : self.offsets[index + 1] - 1].decode(*_ENCODING)

    def segments_with(self, needle: bytes, position: int | None = None):
        """(segment index, position of the first occurrence, segment end) for every
        segment that contains `needle` as a whole, in order. `position` is the
        first occurrence in `low` when the caller already looked for it."""
        low, offsets = self.low, self.offsets
        size = len(needle)
        if not size:
            return
        if position is None:
            position = low.find(needle)
        while position >= 0:
            index = bisect_right(offsets, position) - 1
            end = offsets[index + 1] - 1
            if position + size <= end:
                yield index, position, end
                position = low.find(needle, end)
            else:
                # Only a needle with a newline in it can straddle two segments.
                position = low.find(needle, position + 1)

    def whole_words(self, needle: bytes):
        """(segment index, occurrences) for every segment that has `needle` as a
        whole word, in order: the character before it and the one after it are
        neither a letter nor a digit. Exactly what `(?<![^\\W_])word(?![^\\W_])`
        finds in the folded text."""
        anywhere, plain, mixed = _byte_patterns(needle)
        low, offsets = self.low, self.offsets
        found = anywhere.search(low)
        while found:
            start = found.start()
            index = bisect_right(offsets, start) - 1
            end = offsets[index + 1] - 1
            # Nothing before `start` in this segment matched, so counting goes on from there.
            count = len(plain.findall(low, start, end))
            for match in mixed.finditer(low, start, end):
                if not (self._letter_before(match.start()) or self._letter_after(match.end())):
                    count += 1
            if count:
                yield index, count
            found = anywhere.search(low, end)

    def has_word(self, needle: bytes, short: bool) -> bool:
        """Whether a query word is matched anywhere in the session."""
        if self.low.find(needle) < 0:
            return False
        return not short or next(self.whole_words(needle), None) is not None

    def _letter_before(self, position: int) -> bool:
        """Whether the character ending at `position` is a non-ASCII letter or digit."""
        low = self.low
        if not position or low[position - 1] < 0x80:
            return False
        first = position - 1
        # Step back over the continuation bytes of one character.
        while first and low[first] & 0xC0 == 0x80:
            first -= 1
        return low[first:position].decode(*_ENCODING).isalnum()

    def _letter_after(self, position: int) -> bool:
        """Whether the character starting at `position` is a non-ASCII letter or digit."""
        low = self.low
        if position >= len(low) or low[position] < 0x80:
            return False
        lead = low[position]
        size = 2 if lead < 0xE0 else 3 if lead < 0xF0 else 4
        return low[position : position + size].decode(*_ENCODING).isalnum()

    def count_word_starts(self, needle: bytes, position: int, start: int, end: int) -> int:
        """Occurrences of `needle` in low[start:end] that begin a word, counted from the
        first occurrence at `position`.

        The rule for commands: the character before the word must not be a
        letter or a digit ("ars" is not found in "vars", but is in "my_ars" and
        "x.ars"). Exactly what `(?<![^\\W_])word` finds, without running a
        regular expression over every command.
        """
        low = self.low
        size = len(needle)
        count = 0
        while 0 <= position < end:
            if position == start:
                begins = True
            else:
                before = low[position - 1]
                if before < 0x80:
                    begins = before not in _ASCII_ALNUM
                else:
                    # Step back over the continuation bytes of one character.
                    first = position - 1
                    while first > start and low[first] & 0xC0 == 0x80:
                        first -= 1
                    begins = not low[first:position].decode(*_ENCODING).isalnum()
            if begins:
                count += 1
                position = low.find(needle, position + size, end)
            else:
                position = low.find(needle, position + 1, end)
        return count


_ASCII_ALNUM = frozenset(b"0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
