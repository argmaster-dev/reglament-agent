"""Resolve explicit and conversational scope before retrieval."""

import re
from datetime import datetime, timedelta, timezone

from .lexicon import find_roles, fold, glossary, word_key, words


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
