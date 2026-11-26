"""Role lexicon, action stems and light rule-based Russian stemming."""

from __future__ import annotations

import re

_WORD = re.compile(r"[A-Za-zА-Яа-яЁё0-9]+")

# Longest endings first.
_ENDINGS = sorted(
    ["ыми", "ями", "ами", "ого", "его", "ому", "ему", "ых", "их", "ой", "ей", "ом", "ем",
     "ам", "ям", "ах", "ях", "ов", "ев", "ою", "ею", "ую", "юю", "ая", "яя", "ые", "ие",
     "ий", "ый", "ия", "ию", "ии", "а", "я", "у", "ю", "е", "ы", "и", "о", "ь", "й"],
    key=len, reverse=True,
)
KEY_LEN = 6


def fold(text: str) -> str:
    return text.lower().replace("ё", "е")


def words(text: str) -> list[str]:
    """Lowercased, ё-folded word tokens (hyphen splits words)."""
    return _WORD.findall(fold(text))


def word_key(word: str) -> str:
    """Crude stem key of an already folded word."""
    for end in _ENDINGS:
        if word.endswith(end) and len(word) - len(end) >= 3:
            word = word[: -len(end)]
            break
    return word[:KEY_LEN]


# canonical role -> surface forms (abbreviations and full names)
ROLES: dict[str, list[str]] = {
    "рп": ["РП", "руководитель проекта"],
    "ам": ["АМ", "аккаунт-менеджер"],
    "ss": ["SS", "менеджер по продаже решений"],
    "па": ["ПА", "пресейл-архитектор"],
    "tps": ["TPS", "технический пресейл-специалист"],
    "bdm": ["BDM"],
    "tss": ["TSS"],
    "гип": ["ГИП", "главный инженер проекта"],
    "ап": ["АП", "архитектор проекта"],
    "архитектор": ["Архитектор"],
    "логист": ["Логист"],
    "координатор департамента": ["Координатор департамента"],
    "директор направления": ["Директор направления"],
    "ресурс-менеджер": ["Ресурс-менеджер"],
    "специалист по работе с подрядчиками": ["Специалист по работе с подрядчиками"],
    "менеджер по работе с договорами": ["Менеджер по работе с договорами"],
    "владелец бюджета": ["Владелец бюджета"],
    "заказчик": ["Заказчик"],
}

Pattern = dict[str, list[tuple[str, ...]]]


def _keys(form: str) -> tuple[str, ...]:
    return tuple(word_key(w) for w in words(form))


PATTERNS: Pattern = {c: [_keys(f) for f in forms] for c, forms in ROLES.items()}


def find_roles(tokens: list[str], patterns: Pattern | None = None) -> list[tuple[int, int, str]]:
    """Greedy longest role matches: (start, end_exclusive, canon)."""
    pats = patterns or PATTERNS
    keys = [word_key(t) for t in tokens]
    out: list[tuple[int, int, str]] = []
    i = 0
    while i < len(keys):
        best: tuple[int, str] | None = None
        for canon, forms in pats.items():
            for f in forms:
                n = len(f)
                if n and tuple(keys[i : i + n]) == f and (best is None or n > best[0]):
                    best = (n, canon)
        if best:
            out.append((i, i + best[0], best[1]))
            i += best[0]
        else:
            i += 1
    return out


def canonical_role(text: str) -> tuple[str, Pattern]:
    """Canonical role of a claim's actor string plus the pattern set to use
    (extended with an ad-hoc pattern for unknown roles)."""
    toks = words(text)
    found = find_roles(toks)
    if found:
        # prefer the span covering most of the text
        s, e, canon = max(found, key=lambda m: m[1] - m[0])
        return canon, PATTERNS
    canon = " ".join(toks)
    pats = dict(PATTERNS)
    pats[canon] = [tuple(word_key(t) for t in toks)]
    return canon, pats


# action prefixes -> canonical stem
_ACTION_PREFIXES: list[tuple[tuple[str, ...], str]] = [
    (("подпис",), "подпис"),
    (("соглас",), "соглас"),
    (("созда", "создан"), "созда"),
    (("заключ",), "заключ"),
    (("отвеча", "ответствен"), "отвеч"),
    (("назнач",), "назнач"),
    (("направл",), "направл"),
    (("привлек", "привлеч"), "привле"),
    (("утвер",), "утвер"),
]


def action_stem(word: str) -> str:
    w = fold(word)
    for prefixes, canon in _ACTION_PREFIXES:
        if w.startswith(prefixes):
            return canon
    return w[:5]


NEGATION_WORDS = ("не", "нельзя", "нет")
NEGATION_PREFIXES = ("запрещ",)

NUMBER_WORDS = {
    "два": "2", "две": "2", "три": "3", "четыре": "4", "пять": "5", "шесть": "6",
    "семь": "7", "восемь": "8", "девять": "9", "десять": "10",
}

QUALIFIERS = {
    "обязательно": "обязательно", "обязателен": "обязательно", "обязательный": "обязательно",
    "обязательная": "обязательно", "обязательное": "обязательно",
    "при наличии": "при наличии", "необязательно": "при наличии",
    "необязательный": "при наличии", "опционально": "при наличии",
}
