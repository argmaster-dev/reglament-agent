"""Resolve explicit and conversational scope before retrieval."""

import re
from datetime import datetime, timedelta, timezone

from .lexicon import find_roles, fold, glossary, word_key, words


def _aliases(dept):
    parts = words(dept)
    equivalent = [parts]
    if len(parts) > 1:
        equivalent.append(["".join(p[0] for p in parts)])
        if len(parts[-1]) <= 3:
            equivalent.append([parts[-1]])
    keys = [word_key(w) for w in parts]
    # An abbreviation from the glossary denotes the department when it expands to the same words.
    for abbr, meaning in glossary().items():
        if [word_key(w) for w in words(re.sub(r"\s*\(.*?\)", "", meaning))] == keys:
            equivalent.append(words(abbr))
    return equivalent


def _department_spans(keys, department):
    for alias in _aliases(department):
        target = [word_key(w) for w in alias]
        for start in range(len(keys) - len(target) + 1):
            if keys[start:start + len(target)] == target:
                yield start, start + len(target)


def departments_in(text, departments):
    """Every known department the text names, in order of appearance."""
    keys = [word_key(w) for w in words(text)]
    found = []
    for dept in departments:
        span = next(_department_spans(keys, dept), None)
        if span is not None:
            found.append((span[0], dept))
    # A longer name that contains a shorter one wins over it.
    names = [d for _, d in found]
    return [d for _, d in sorted(found) if not any(d != o and set(words(d)) < set(words(o)) for o in names)]


def department_in(text, departments):
    keys = [word_key(w) for w in words(text)]
    return next((dept for dept in sorted(departments, key=len, reverse=True)
                 if next(_department_spans(keys, dept), None) is not None), None)


STAGE_LABELS = {"presale": "Пресейл", "project": "Проект"}
_STAGE_PHRASES = {
    "presale": re.compile(r"\b(?:(?:на\s+)?(?:этап\w*|стади\w*)\s+|на\s+)"
                          r"(?:пресейл(?:а|у|е|ом)?|предпродаж(?:а|и|у|е|ей)?)\b"),
    "project": re.compile(r"\b(?:на\s+этапе\s+проекта|на\s+проектной\s+стадии|на\s+проекте|проектная\s+деятельность)\b"),
}


def stage_in(text):
    value = fold(text).strip(" ?.!")
    for stage, label in STAGE_LABELS.items():
        if value == fold(label) or _STAGE_PHRASES[stage].search(value):
            return stage
    return None


def without_scope(text, scope):
    """Remove explicit scope phrases while keeping subject words such as «проектная заявка»."""
    text = fold(text)
    if scope.stage:
        # A standalone button «Проект» resolves a stage, but «проект» can be the question's object.
        text = _STAGE_PHRASES[scope.stage].sub(" ", text)
    if not scope.department:
        return text
    tokens = words(text)
    keys = [word_key(word) for word in tokens]
    for start, end in _department_spans(keys, scope.department):
        tokens[start:end] = [""] * (end - start)
    return " ".join(tokens)


_PERSONAL_DEPARTMENT = re.compile(
    r"^\s*(?:я\s+(?:(?:теперь|сейчас)\s+)?(?:(?:работаю|переш[её]л|перешла)\s+)?(?:в|из)\s+"
    r"|мо[её] подразделение\s*[:—–-]?\s*)",
    re.IGNORECASE,
)
_POSITION = re.compile(
    r"^\s*(?:моя должность|моя роль(?: в процессе)?)\s*(?:[:—–-]|это)\s*"
    r"(?P<title>[^.!?]+)[.!]?\s*$",
    re.IGNORECASE,
)
_ROLE = re.compile(r"^\s*я\s+(?:работаю\s+|[-—–]\s*)?(?P<title>[^.!?]+)[.!]?\s*$", re.IGNORECASE)
_QUESTION = re.compile(
    r"^\s*(?:кто|сколько|можно|нужн[оы]|должен|как|когда|зачем|почему|како[йея]|где|что)\b"
)
_UNKNOWN = {"не знаю", "не уверен", "не уверена"}


def normalized_reply(text):
    return fold(text).strip(".!? ")


def unknown_reply(text):
    return normalized_reply(text) in _UNKNOWN


def personal_department(text, department):
    return department if department and _PERSONAL_DEPARTMENT.match(text) and "?" not in text else None


def personal_position(text):
    match = _POSITION.fullmatch(text) or _ROLE.fullmatch(text)
    if not match:
        return None
    title = " ".join(match["title"].split())
    tokens = words(title)
    if len(title) > 80 or len(tokens) > 8 or not re.fullmatch(r"[а-яёa-z][\w -]*", title, re.IGNORECASE):
        return None
    if _POSITION.match(text) or any(
        start == 0 and end == len(tokens) for start, end, _ in find_roles(tokens)
    ):
        return title
    return None


def new_question(text):
    return "?" in text or bool(_QUESTION.match(fold(text))) or len(words(text)) >= 6


def scope_only(text, department):
    if not department:
        return False
    return len(words(text)) <= 6 and not re.search(
        r"кто|сколько|можно|долж|нуж|как|когда|зачем|почему|подпис|договор|заявк", fold(text)
    )


def named_department(text, department):
    """A bare option is a personal answer; a phrase such as «а в ИБ?» is a scope change."""
    return (
        department_in(" ".join(words(text)), [department]) == department
        and (len(words(text)) == len(words(department)) or len(words(text)) == 1)
        and not normalized_reply(text).startswith("а ")
        and "?" not in text
    )


def profile_expired(profile, max_age_days):
    return (
        profile.department
        and profile.confirmed_at
        and (
            datetime.now(timezone.utc) - datetime.fromisoformat(profile.confirmed_at)
            > timedelta(days=max_age_days)
        )
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
