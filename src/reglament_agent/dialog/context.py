"""Resolve explicit and conversational scope before retrieval."""

import re

from .lexicon import fold, glossary, word_key, words


def department_in(text, departments):
    tokens = words(text)
    for dept in sorted(departments, key=len, reverse=True):
        parts = words(dept)
        equivalent = [parts]
        if len(parts) > 1:
            equivalent.append(["".join(p[0] for p in parts)])
            if len(parts[-1]) <= 3:
                equivalent.append([parts[-1]])
        for alias in equivalent:
            keys = [word_key(w) for w in alias]
            if any([word_key(w) for w in tokens[i : i + len(keys)]] == keys for i in range(len(tokens))):
                return dept
    return None


STAGE_LABELS = {"presale": "Пресейл", "project": "Проект"}


def stage_in(text):
    value = fold(text).strip(" ?.!")
    for stage, label in STAGE_LABELS.items():
        if value == fold(label):
            return stage
    if re.search(r"\b(на |этап\w* |стади\w* )(пресейл|предпродаж)", fold(text)):
        return "presale"
    if re.search(r"\b(на этапе проекта|на проектной стадии|на проекте|проектная деятельность)", fold(text)):
        return "project"
    return None


def personal(text):
    return bool(
        re.search(
            r"\b(я (?:теперь |сейчас )?(?:работаю |из |в |переш[её]л)|мо[её] подразделение)", fold(text)
        )
    )


def scope_only(text, dept):
    if not dept:
        return False
    return len(words(text)) <= 6 and not re.search(
        r"кто|сколько|можно|долж|нуж|как|когда|зачем|почему|подпис|договор|заявк", fold(text)
    )


def definition(text):
    match = re.fullmatch(
        r"(?:что (?:такое|значит|означает)|расшифруй(?:те)?|как расшифровывается)\s+([\w-]+)[?!. ]*",
        text.strip(),
        re.IGNORECASE,
    )
    if match:
        return next(
            (f"{abbr} — {meaning}" for abbr, meaning in glossary().items() if fold(abbr) == fold(match[1])),
            None,
        )
    return None
