"""The scorer that reads stored search text must agree with the original one, hit for hit."""

from __future__ import annotations

import random
import unittest
from pathlib import Path
from unittest import mock

import helpers  # noqa: F401  (puts the package on the path)

from find_session.model import Session
from find_session.ranking import count_terms, has_every_word, query_terms, score_session, score_text
from find_session.searchtext import SearchText
from reference_ranking import occurrences, reference_fold, reference_score, reference_searchable_text

# Synthetic vocabulary: plain words, Polish words, identifiers, words inside
# longer words, characters whose lower case is longer than they are, and a lone
# surrogate (JSON can carry one).
WORDS = [
    "cache",
    "index",
    "Indexing",
    "reindex",
    "vars",
    "ars",
    "zażółć",
    "ZAŻÓŁĆ",
    "gęślą",
    "jaźń",
    "płatność",
    "faktura",
    "FAKTURA_2026",
    "invoice-export",
    "module.py",
    "a",
    "aa",
    "aaa",
    "İstanbul",
    "straße",
    "x\ud800y",
    "deploy",
    "re-deploy",
    "_private",
    "to",
    "śćź",
    # What may stand before a word in a command: letters and digits of any
    # script do not start a word, punctuation and the underscore do.
    "łars",
    "²ars",
    "ǅars",
    "‿ars",
    "ars_ars",
    "arsars",
    "ßdeploy",
    "x.deploy",
    "\ud800ars",
]
ROLES = ["user", "assistant", "assistant", "delegated", "summary"]
QUERIES = [
    "cache",
    "index",
    "cache index",
    "ars",
    "zażółć gęślą",
    "ZAŻÓŁĆ",
    "faktura płatność",
    "invoice-export",
    "module.py",
    "a",
    "aa a",
    "istanbul",
    "i̇stanbul",
    "straße deploy",
    "re-deploy deploy",
    "_private",
    "to index cache deploy",
    "demo",
    "projects demo",
    "x\ud800y",
    "nothing-here",
    "ść",
]


OPTIONS = [
    {"use_tools": False},
    {"chunk": 0},
    {"chunk": 3, "tool_coverage": 0.5},
    {"tool_coverage": 0.0},
    {"chunk": 1},
]


def sentence(rng: random.Random, low: int = 1, high: int = 12) -> str:
    joiner = rng.choice([" ", " ", " ", "\n", "/", "_", "-", ""])
    return joiner.join(rng.choice(WORDS) for _ in range(rng.randint(low, high)))


def generated_session(rng: random.Random) -> Session:
    session = Session(
        "Claude",
        f"gen-{rng.randrange(10**6)}",
        rng.choice(["~/projects/demo", "~/Clients/Zażółć-Shop", "unknown", "~/x/cache"]),
        Path("generated.jsonl"),
        rng.choice([0.0, 1_700_000_000.0, 1_795_000_000.0]),
    )
    for _ in range(rng.choice([0, 0, 1, 2, 5, 9, 17, 40])):
        if rng.random() < 0.5:
            # Commands before the first message and between messages.
            for _ in range(rng.randint(0, 3)):
                session.tools.append((len(session.turns), sentence(rng, 1, 6)))
        session.append(rng.choice(ROLES), sentence(rng), "")
    for _ in range(rng.randint(0, 3)):
        session.tools.append((rng.randint(0, len(session.turns) + 2), sentence(rng, 1, 6)))
    # Long commands: words beyond the part that counts in full, with and without a space to cut at.
    for _ in range(rng.choice([0, 0, 1, 2])):
        session.tools.append((rng.randint(0, len(session.turns) + 2), sentence(rng, 40, 160)))
    return session


def terms_of(query: str) -> list[str]:
    return query_terms(query)


class ScoringEquivalenceTests(unittest.TestCase):
    def test_generated_sessions_score_exactly_like_the_reference(self):
        rng = random.Random(20261119)
        checked = matched = late = 0
        with mock.patch("time.time", new=lambda: 1_795_050_000.0):
            for number in range(120):
                session = generated_session(rng)
                stored = SearchText.stored(SearchText.of(session).layout(), SearchText.of(session).low)
                for position, query in enumerate(QUERIES):
                    terms = terms_of(query)
                    for options in ({}, OPTIONS[(number + position) % len(OPTIONS)]):
                        expected = reference_score(session, terms, query, **options)
                        self.assertEqual(score_session(session, terms, query, **options), expected, (query, options))
                        self.assertEqual(
                            score_text(stored, session.mtime, terms, query, **options), expected, (query, options)
                        )
                        checked += 1
                        matched += bool(expected.score)
                        # A word found only far into a long command adds a fraction of a word.
                        late += expected.chunk_coverage % 0.5 != 0
                    haystack = reference_fold(reference_searchable_text(session))
                    present = tuple(bool(occurrences(term, haystack)) for term in terms)
                    self.assertEqual(has_every_word(stored, terms), all(present), query)
                    for term, there in zip(terms, present):
                        self.assertEqual(has_every_word(stored, [term]), there, term)
        # The generator must exercise both outcomes, or the test proves nothing.
        self.assertGreater(matched, checked // 10)
        self.assertGreater(checked - matched, checked // 10)
        self.assertGreater(late, 20)

    def test_phrase_is_found_even_when_it_contains_none_of_the_words(self):
        # Lower-casing a whole query can differ from lower-casing its words
        # (Greek final sigma), so the phrase is not always made of the words.
        session = Session("Claude", "s", "~/p", Path("s.jsonl"), 0.0)
        session.append("user", "alpha only", "")
        session.append("assistant", "beta gamma here, and alpha", "")
        session.tools.append((2, "run beta gamma"))
        session.append("user", "beta\ngamma", "")
        text = SearchText.of(session)
        self.assertEqual(count_terms(text, ["alpha"], "beta gamma").with_phrase, [False, True, False])
        self.assertEqual(count_terms(text, ["alpha"], "beta gamma", use_tools=False).with_phrase, [False, True, False])
        self.assertEqual(count_terms(text, ["run"], "beta gamma").with_phrase, [False, True, False])
        self.assertEqual(count_terms(text, ["beta"], "beta gamma").with_phrase, [False, True, False])
        self.assertEqual(count_terms(text, ["gamma", "alpha"], "alpha only").with_phrase, [True, False, False])
        self.assertEqual("ΑΣ".lower() in "ΑΣ'Β".lower(), False)
        # Stored text joins messages and commands with newlines; a phrase with
        # a newline in it must still lie inside one message or one command row.
        session = Session("Claude", "s", "~/p", Path("s.jsonl"), 0.0)
        session.append("user", "x alpha", "")
        session.tools.append((1, "beta y"))
        session.append("assistant", "alpha\nbeta", "")
        text = SearchText.of(session)
        self.assertEqual(count_terms(text, ["x"], "alpha\nbeta").with_phrase, [False, True])

    def test_phrase_with_a_newline_never_spans_two_messages(self):
        session = Session("Claude", "s", "~/p", Path("s.jsonl"), 0.0)
        session.append("user", "alpha", "")
        session.tools.append((1, "beta"))
        session.append("assistant", "alpha\nbeta gamma", "")
        query = "alpha\nbeta"
        terms = terms_of(query)
        with mock.patch("time.time", new=lambda: 1_795_050_000.0):
            self.assertEqual(score_session(session, terms, query), reference_score(session, terms, query))


if __name__ == "__main__":
    unittest.main()
