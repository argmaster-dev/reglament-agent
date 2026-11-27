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


# --- glossary ---------------------------------------------------------------

import os
from functools import lru_cache
from pathlib import Path

_PACKAGED_GLOSSARY = Path(__file__).with_name("glossary.tsv")
_DEFAULT_GLOSSARY = (_PACKAGED_GLOSSARY if _PACKAGED_GLOSSARY.is_file()
                     else Path(__file__).resolve().parents[3] / "docx" / "glossary.tsv")
_glossary_path: Path | None = None
_slang_path: Path | None = None


def set_glossary_path(path: str | Path | None) -> None:
    """Override the glossary file (None restores env/default) and drop caches."""
    global _glossary_path
    _glossary_path = Path(path) if path else None
    _load.cache_clear()
    _load_slang.cache_clear()
    _patterns.cache_clear()


def set_slang_path(path: str | Path | None) -> None:
    """Override the optional jargon file (None restores env/default) and drop caches."""
    global _slang_path
    _slang_path = Path(path) if path else None
    _load.cache_clear()
    _load_slang.cache_clear()
    _patterns.cache_clear()


def glossary_path() -> Path:
    if _glossary_path:
        return _glossary_path
    env = os.environ.get("GLOSSARY_PATH")
    return Path(env) if env else _DEFAULT_GLOSSARY


def slang_path() -> Path:
    """Jargon file: override, env SLANG_PATH, else slang.tsv next to the glossary."""
    if _slang_path:
        return _slang_path
    env = os.environ.get("SLANG_PATH")
    return Path(env) if env else glossary_path().with_name("slang.tsv")


def _read_rows(path: Path) -> list[tuple[tuple[str, ...], str]]:
    rows: list[tuple[tuple[str, ...], str]] = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        if i == 0 or not line.strip() or "\t" not in line:
            continue
        abbr, exp = line.split("\t", 1)
        variants = tuple(v.strip() for v in abbr.split(",") if v.strip())
        rows.append((variants, exp.strip()))
    return rows


@lru_cache(maxsize=1)
def _load_slang() -> tuple[tuple[tuple[str, ...], str], ...]:
    """Jargon rows without any variant the glossary already defines (jargon -> official term only)."""
    slang = slang_path()
    if not slang.is_file():
        return ()
    taken = {fold(v) for variants, _ in _read_rows(glossary_path()) for v in variants}
    rows = ((tuple(v for v in variants if fold(v) not in taken), exp) for variants, exp in _read_rows(slang))
    return tuple(row for row in rows if row[0])


@lru_cache(maxsize=1)
def _load() -> tuple[tuple[tuple[str, ...], str], ...]:
    """Rows of (abbreviation variants, expansion): glossary first, then the optional jargon file."""
    return (*_read_rows(glossary_path()), *_load_slang())


def glossary() -> dict[str, str]:
    """abbreviation (every variant) -> expansion."""
    return {v: exp for variants, exp in _load() for v in variants}


def _clean(exp: str) -> str:
    return re.sub(r"\s*\(.*?\)", "", exp).strip()


# Glossary abbreviations that denote roles (everything else is not an actor).
ROLE_ABBREVIATIONS = ["АМ", "АП", "BDM", "ГИП", "ПА", "РП", "РМ", "РСП", "SS", "TSS"]

# Extra hand-written role forms used in the regulation texts.
EXTRA_ROLE_FORMS: dict[str, list[str]] = {
    "SS": ["менеджер по продаже решений"],
    "TPS": ["TPS", "технический пресейл-специалист"],
    "архитектор": ["Архитектор"],
    "логист": ["Логист"],
    "координатор департамента": ["Координатор департамента"],
    "директор направления": ["Директор направления"],
    "специалист по работе с подрядчиками": ["Специалист по работе с подрядчиками"],
    "менеджер по работе с договорами": ["Менеджер по работе с договорами"],
    "владелец бюджета": ["Владелец бюджета"],
    "заказчик": ["Заказчик"],
}

Pattern = dict[str, list[tuple[str, ...]]]


def _keys(form: str) -> tuple[str, ...]:
    return tuple(word_key(w) for w in words(form))


@lru_cache(maxsize=1)
def _patterns() -> Pattern:
    gl = glossary()
    pats: Pattern = {}
    for abbr in ROLE_ABBREVIATIONS:
        forms = [abbr] + ([_clean(gl[abbr])] if abbr in gl else [])
        pats[abbr.lower()] = [_keys(f) for f in forms]
    for canon, forms in EXTRA_ROLE_FORMS.items():
        pats.setdefault(canon.lower(), []).extend(_keys(f) for f in forms)
    return pats


def __getattr__(name: str):
    if name == "PATTERNS":
        return _patterns()
    raise AttributeError(name)


def find_roles(tokens: list[str], patterns: Pattern | None = None) -> list[tuple[int, int, str]]:
    """Greedy longest role matches: (start, end_exclusive, canon)."""
    pats = patterns or _patterns()
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
        s, e, canon = max(found, key=lambda m: m[1] - m[0])
        return canon, _patterns()
    canon = " ".join(toks)
    pats = dict(_patterns())
    pats[canon] = [tuple(word_key(t) for t in toks)]
    return canon, pats


def _seq_in(seq: list[str], sub: list[str]) -> bool:
    n = len(sub)
    return n > 0 and any(seq[i : i + n] == sub for i in range(len(seq) - n + 1))


def expand_query(text: str) -> str:
    """Append expansions of abbreviations found in the query and abbreviations
    of expansions found in it."""
    toks = words(text)
    keys = [word_key(t) for t in toks]
    low = text.lower()
    extra: list[str] = []
    slang = frozenset(_load_slang())
    for row in _load():
        variants, exp = row
        clean = _clean(exp)
        if any(_seq_in(toks, words(v)) for v in variants):
            if fold(clean) not in fold(text):
                extra.append(clean)
        elif row not in slang and _seq_in(keys, [word_key(w) for w in words(clean)]):
            extra.extend(v for v in variants if v.lower() not in low)
    return " ".join([text, *extra]) if extra else text


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
