"""Typo repair against the vocabulary of the active regulations."""

import re
from difflib import SequenceMatcher

from .lexicon import fold, word_key, words

_MIN_LENGTH = 5
_CUTOFF = 0.84


class Vocabulary:
    def __init__(self, forms):
        self.forms = frozenset(forms)
        self.keys = frozenset(word_key(w) for w in self.forms)
        self._by_head = {}
        for form in self.forms:
            self._by_head.setdefault(form[:1], []).append(form)

    @classmethod
    def from_texts(cls, texts):
        return cls(w for text in texts for w in words(text) if len(w) >= _MIN_LENGTH and not w.isdigit())

    def known(self, word):
        return word in self.forms or word_key(word) in self.keys

    def closest(self, word):
        best, score = None, _CUTOFF
        # A typo may hit the first letter, so compare with forms starting with the first two letters or the last.
        pool = {f for head in {word[:1], word[1:2]} for f in self._by_head.get(head, [])}
        for form in pool:
            if abs(len(form) - len(word)) > 2:
                continue
            ratio = SequenceMatcher(None, word, form).ratio()
            if ratio > score:
                best, score = form, ratio
        return best


def correct(text, vocabulary):
    """Replace words that are unknown to the corpus and very close to one of its words."""
    if not vocabulary.forms:
        return text

    def fix(match):
        raw = match.group(0)
        word = fold(raw)
        if len(word) < _MIN_LENGTH or word.isdigit() or vocabulary.known(word) or raw.isupper():
            return raw
        replacement = vocabulary.closest(word)
        if not replacement:
            return raw
        return replacement.capitalize() if raw[:1].isupper() else replacement

    return re.sub(r"[A-Za-zА-Яа-яЁё]+", fix, text)
