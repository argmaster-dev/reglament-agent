"""Recognize deadline questions and distinguish time before and after a reference event."""
import re
from typing import Literal

from .lexicon import fold

DeadlineMode = Literal["before", "after", "any"]

_LIMIT = r"срок(?:а|и|ов|ам|ами|ах|ом|е|у)?\b"
_LIMIT_ASK = (
    rf"^\s*{_LIMIT}"
    rf"|\b(?:како[йгвм]\w*|какие|когда|уточните|укажите|назовите|подскажите|сообщите|напомните)"
    rf"(?:,?\s+)(?:[\w-]+\s+){{0,2}}{_LIMIT}"
)
_QUESTION = re.compile(
    _LIMIT_ASK + r"|за сколько|заранее|за како[ей] (?:время|срок)|в течение како|как долго|как быстро"
    r"|до како\w* (?:числа|момента)|до скольки|через сколько|не позднее како|не раньше како"
    r"|сколько\s+(?:\w+\s+)?(?:дн|дне|недел|час|месяц|времени|суток)"
    r"|когда\s+(?:нужно|надо|следует|необходимо|должн|можно|подава|сда)"
)
_OTHER_INTENT = re.compile(
    r"\bкто\b|ответственн\w* за|что (?:будет|произойдет)|как меняется|какие (?:последствия|санкции)"
)
_AFTER = r"\b(?:после|через|со? (?:момента|даты|дня)|начиная с)\b"
_BEFORE = r"\b(?:за\b[^.!?;]{1,64}\bдо|не позднее чем за|до начал\w*|до завершен\w*|до закрыти\w*|до конца)\b"
_UNIT = r"(?:дн\w*|день|недел\w*|час\w*|месяц\w*|минут\w*|сут\w*|квартал\w*|год\w*|лет)"
_ELAPSED_QUESTION = re.compile(
    rf"\bза сколько\s+(?:(?:рабоч|календарн)\w*\s+)?{_UNIT}\s+"
    r"(?!(?:следует|требуется|полагается|надлежит)\b)[а-я]{2,}(?:ают|яют|уют|ют|ут|ат|ят|ет|ит)(?:ся)?\b"
)
_NUMBER = r"(?:\d+|один|одн\w*|два|две|двух|три|трех|четыре|пять|пяти|шесть|семь|восемь|девять|десять|десяти|двадцать|тридцать)"
_DURATION = rf"\b{_NUMBER}\s+(?:(?:рабоч|календарн)\w*\s+)?{_UNIT}\b"
_VALUE = (
    rf"(?:{_DURATION}|\b\d{{1,2}}:\d{{2}}\b|\b(?:немедленно|в тот же день)\b"
    r"|\b(?:следующ\w*|ближайш\w*)\s+(?:рабоч\w*\s+)?(?:день|дня)\b"
    r"|\bдо\s+(?:начал\w*|завершен\w*|закрыти\w*|конца)\s+\w+)"
)


def question_mode(question: str) -> DeadlineMode | None:
    question = fold(question)
    if not _QUESTION.search(question) or _OTHER_INTENT.search(question):
        return None
    if re.search(_AFTER + r"|\bчерез сколько\b", question):
        return "after"
    if _ELAPSED_QUESTION.search(question) and not re.search(r"\b(?:до|перед|заранее)\b", question):
        # “За сколько часов проверяют?” asks how long the action takes. The source
        # provides its reference event, which can be earlier or later than the action.
        return "any"
    if re.search(r"\b(?:заранее|за сколько|за какое время|за какой срок|до)\b", question):
        return "before"
    return "any"


def has_deadline(text: str, mode: DeadlineMode) -> bool:
    text = fold(text)
    if not re.search(_VALUE, text):
        return False
    if mode == "before":
        return bool(re.search(_BEFORE, text))
    if mode == "after":
        return bool(re.search(_AFTER, text)) and not re.search(_BEFORE, text)
    return True


def ranking_pattern(question: str) -> str:
    """Broaden deadline retrieval while keeping the direction of an explicit time relation."""
    match question_mode(question):
        case "before":
            pattern = _BEFORE
        case "after":
            pattern = _AFTER
        case "any":
            pattern = _VALUE
        case _:
            return ""
    # PostgreSQL's equivalent for a word boundary is \y.
    return pattern.replace(r"\b", r"\y")
