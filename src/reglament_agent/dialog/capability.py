"""A verified staffing assessment may explain the method without certifying a specific project."""
import re

from reglament_agent.answer import Answer, Outcome, Statement

from .lexicon import fold


SEARCH_QUERY = "компетенции квалификация навыки"
_WORK = re.compile(r"\b(?:клиент|заказчик|проект|задач|работ|услуг)\w*")
_METHOD = re.compile(r"\bкак (?:понять|определить|проверить|оценить|узнать|выяснить)\b")
_CAN = re.compile(
    r"\b(?:что\s+мы\s+(?:это\s+)?(?:сможем|можем)|(?:сможем|можем)\s+ли\s+мы)"
    r"(?:\s+(?:это\s+)?(?:сделать|выполнить|реализовать|оказать)(?:\s+(?:услугу|работы|задачу|проект))?)?"
    r"\s*(?:[?.!,;]|$)"
)
_PERMISSION = re.compile(r"\b(?:прав[оа]|правомоч\w*|закон\w*|разрешени\w*|лицензи\w*|допуск\w*)\b")
_REGISTER = re.compile(r"\bпо\s+(?:реестр|матриц|справочник)\w*\s+"
                       r"(?:(?:техническ|профессиональн)\w*\s+)?(?:компетенци|квалификаци|навык)\w*")
_ASSESS = re.compile(r"\b(?:определя|оценива|проверя)(?:ет|ют)\s+возможност\w*\s+"
                     r"(?:выполнения|реализации|выполнить|реализовать)\b")
_OWN = re.compile(r"\b(?:внутренн\w*\s+(?:ресурс|сил|команд)\w*|"
                  r"(?:собственн|сво)\w*\s+(?:ресурс|сил|команд)\w*)\b")
_EXCEPTION = re.compile(r"\b(?:не|без|если|кроме|вместо|подрядчик\w*|поставщик\w*|партнер\w*)\b")
_SCOPE = ("Чтобы ответить по конкретным работам, нужно уточнить их состав, возможности команды "
          "и право выполнить эти работы.")


def requested(question):
    text = fold(question)
    method = _METHOD.search(text)
    if not method or not _WORK.search(text) or _PERMISSION.search(text):
        return False
    return bool(_CAN.match(text[method.end():].lstrip(" ,:—-")))


def is_assessment(anchor):
    if re.search(r"\b(?:если|кроме|вместо)\b", fold(anchor.text)):
        return False
    for clause in re.split(r"[,.!?;:]", fold(anchor.text)):
        if _EXCEPTION.search(clause):
            continue
        register = _REGISTER.search(clause)
        assessment = _ASSESS.search(clause)
        own = _OWN.search(clause)
        if register and assessment and own and assessment.end() <= own.start():
            return True
    return False


def partial(rows, *, require_unique=True):
    methods = {row.ref: row for row in rows if is_assessment(row)}
    if not methods or (require_unique and len(methods) != 1):
        return None
    departments = {ref.department for ref in methods}
    where = f"В подразделении «{departments.pop()}»" if len(departments) == 1 else "В найденных регламентах"
    return Answer(
        Outcome.LOW_CONFIDENCE,
        f"{where} описан способ проверить возможности команды.",
        statements=[Statement(row.text, [row.ref]) for row in methods.values()],
        caveat=_SCOPE,
    )
