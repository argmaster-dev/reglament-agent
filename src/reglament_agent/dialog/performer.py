"""Check which clause actually assigns a named procedure to a performer."""

import re
from dataclasses import dataclass

from .lexicon import ROLE_ABBREVIATIONS, fold, word_key, words
from .relevance import terms

_ACTION = re.compile(r"\b(провод(?:ит|ят)|организу(?:ет|ют)|выполня(?:ет|ют)|осуществля(?:ет|ют))\b")
_DUTY = re.compile(r"\bотвеча(?:ет|ют)\b")
_CLAUSES = re.compile(r"[,;:.!?]|\s+(?:а|но|и)\s+(?=\w+(?:ет|ит|ят|ют|ут|ат)\b)")
# Check an explicit nominative subject. A dative addressee («начальнику отдела») cannot own the action.
_ROLE = re.compile(
    r"^(?:(?:менеджер|координатор|оператор|куратор|диспетчер|администратор|инженер|эксперт|"
    r"бухгалтер|директор|архитектор|аудитор|инспектор|специалист|логист|руководитель|"
    r"начальник|ответственный|исполнитель|секретарь|сотрудник|представитель|отдел|департамент)"
    r"(?:ы|и)?|руководители|исполнители|служба|группа|комиссия|управление|подразделение|бюро|команда)$"
)
# Productive nominative job-title endings apply in the explicit duty construction
# («за работу отвечает …»). Oblique forms such as «мастеру» cannot pass this check.
_TITLE_NOMINATIVE = re.compile(r"^[а-я-]{2,}(?:тель|ник|щик|чик|ист|тор|ант|ент|вед|олог|ург|арь|ер|ор)$")
_ROLE_CODES = {fold(r) for r in ROLE_ABBREVIATIONS} | {"tps"}
_SKIP_MODIFIER = re.compile(r"^\w+(?:ый|ий|ой|ая|ые|ие)$")
_CUSTOMER = {word_key("клиент"), word_key("заказчик")}


def _scope_keys(text):
    return {"клиент" if k in _CUSTOMER else k for k in terms(text)}


def _owner(text, *, explicit_duty=False):
    toks = words(text)
    while len(toks) > 1 and _SKIP_MODIFIER.fullmatch(toks[0]) and not _ROLE.fullmatch(toks[0]):
        toks = toks[1:]
    return bool(toks and (toks[0] in _ROLE_CODES or _ROLE.fullmatch(toks[0])
                          or (explicit_duty and _TITLE_NOMINATIVE.fullmatch(toks[0]))))


@dataclass(frozen=True)
class Procedure:
    verb: str
    subject: frozenset[str]

    def supported_by(self, text):
        for clause in _CLAUSES.split(fold(text)):
            for match in _DUTY.finditer(clause):
                left, right = clause[:match.start()].strip(), clause[match.end():].strip()
                if left.endswith(" не"):
                    continue
                if left.startswith("за ") and self.subject <= _scope_keys(left) and _owner(right, explicit_duty=True):
                    return True
                if right.startswith("за ") and self.subject <= _scope_keys(right) and _owner(left, explicit_duty=True):
                    return True
            for match in _ACTION.finditer(clause):
                if word_key(match[0]) != self.verb:
                    continue
                left, right = clause[:match.start()].strip(), clause[match.end():].strip()
                if left.endswith(" не"):
                    continue
                if ((_owner(left) and self.subject <= _scope_keys(right))
                        or (_owner(right) and self.subject <= _scope_keys(left))):
                    return True
        return False


def requested(question, departments):
    question = fold(question)
    who = re.search(r"\bкто\b", question)
    action = _ACTION.search(question, who.end()) if who else None
    if not action:
        return None
    phrase = question[action.end():]
    # Circumstances and compound requests can require more than one sentence and their own scope checks.
    if re.search(r"\b(?:при|если|без|до|после|когда|чтобы)\b|[,;]", phrase):
        return None
    subject = _scope_keys(phrase) - _scope_keys(" ".join(departments))
    return Procedure(word_key(action[0]), frozenset(subject)) if subject else None
