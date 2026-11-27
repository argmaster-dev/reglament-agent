"""Keep a verified instruction tied to the operation and triggering event the employee describes."""

import re
from collections import defaultdict

from .lexicon import find_roles, fold, words
from .relevance import terms

_EQUIVALENT = re.compile(r"\b(?:эквивалент\w*|аналог(?:а|и|ов|ом|у|ами)?|равноценн\w*\s+замен\w*)\b")
_SUPPLY_EQUIVALENT = re.compile(
    r"\b(?:поставить|закуп\w*|закупить|приобрести|заменить|поставк\w*|использовать|применить)\b[^,.?!;]{0,55}$"
)
_PUBLIC = re.compile(r"\b(?:гос(?:ударственн\w*\s+)?(?:заказ\w*|закуп\w*)|государственн\w*\s+контракт\w*)")
_NORM = re.compile(r"\b(?:соглас\w*|разреш\w*|допуска\w*|запрещ\w*|можно|нельзя|обоснова\w*|требу\w*)\b")
_CONDITION = re.compile(r"\b(?:в случае(?:,? если)?|если|при|когда)\s+(.*?)(?:[,;:.]|\b(?:необходим\w*|"
                        r"следует|требуется|можно|нельзя|допуска\w*|запрещ\w*|разреш\w*|согласовыва\w*)\b|$)")
_AVAILABILITY = terms("наличие доступность есть доступен аналог эквивалент "
                      "оборудование изделие деталь товар модель продукция позиция программное обеспечение ПО")
_PUBLIC_TERMS = terms("государственная государственным госзакупка закупка заказчик контракт")
_ESCALATE = re.compile(r"\bэскал\w*")
_STATUS = re.compile(r"\b(?:статус|состоян|здоровь|зон[аыеу])\w*")
_CHANGE = re.compile(r"\b(?:покрасн\w*|ухудш\w*|выпал\w*|переш\w*|переход\w*|перевод\w*|изменени\w*)\b")
_BAD_STATE = re.compile(r"\b(?:покрасн\w*|красн\w*|желт\w*|проблемн\w*|под риском|из зеленой зоны)\b")
_NEGATED_CHANGE = re.compile(r"\bне\s+(?:покрасн|ухудш|выпал|переш|перев|измен|красн|проблемн)\w*")
_STATE_SUBJECT = re.compile(r"\b(проект|заказ|сервис|инцидент|заявк|сделк|обращени)"
                            r"(?:а|у|ом|е|ы|и|ов|ам|ами|ах|я|ю|ем|й|ям|ями|ях|ой)?\b")
_NEXT_LEVEL = re.compile(r"(?:при|в случае) отсутстви[ия]\s+(?:решени|ответ|реакци)\w*"
                         r"|после (?:\w+\s+){0,2}(?:коммуникаци|уведомлени|информировани|обращени)\w*")
_TRIP = re.compile(r"\b(?:выезд\w*|выезж\w*|выед\w*|поездк\w*|командировк\w*|посещени\w*)\b")
_BEFORE_TRIP = re.compile(r"\b(?:до|перед|накануне|для|при подготовке к)\s+(?:предстоящ\w*\s+)?"
                          r"(?:выезд\w*|посещени\w*|поездк\w*|командировк\w*|прибыти\w*)\b")
_AFTER_TRIP = re.compile(r"\b(?:после (?:выезда|поездки|прибытия|командировки)|по прибытии|по возвращении)\b")
_PREPARE = re.compile(r"\b(?:что\b[^,;?!]{0,50}\b(?:подготов|оформ|собра|провери)|как\s+подготовиться)")
_PREP_ACTION = re.compile(r"\b(?:подготов|оформ|провер|получ|запрос|запраш|соглас|состав|уточн|обеспеч|собир)\w*")

_SITE = re.compile(r"\b(?:объект|площадк|территори)\w*\b")
_SITE_PARTY = re.compile(r"\b(?:объект|площадк|территори)\w*\s+(?:к\s+)?(заказчик|клиент|поставщик|подрядчик|производител)\w*\b")
_START_WORK = re.compile(r"\bподготовк\w*\s+к\s+(?:начал|выполнени|проведени)\w*\s+(?:\w+\s+){0,2}работ\w*\b")
_SITE_WORK = re.compile(r"\bработ\w*\s+на\s+(?:объект|площадк|территори)\w*\b")
_OTHER_PURPOSE = re.compile(r"\b(?:совещани|повестк|отчет|возвращени|возврат|протокол|презентаци)\w*\b"
                            r"|\b(?:на|к|для)\s+(?:\w+\s+)?встреч\w*\b")
_CANCELLED = re.compile(r"\bне (?:выезж|выед)\w*|\b(?:поездк|выезд)\w* отмен\w*")
_ACTION_ITEM = re.compile(r"(?:согласует|согласовывает|получает|проверяет|инициирует|оформляет|заказывает|готовит|подготавливает|организует|запрашивает|уточняет|составляет|обеспечивает)\b")


def _site_party(text):
    match = _SITE_PARTY.search(text)
    if not match:
        return None
    return "клиент" if match[1] in ("клиент", "заказчик") else match[1]


def site_work_steps(question, rows):
    """Find a complete actor's checklist for starting work at the site, if one point establishes it."""
    q = fold(question)
    if not (_TRIP.search(q) and _PREPARE.search(q) and _SITE.search(q)) or _OTHER_PURPOSE.search(q) or _CANCELLED.search(q):
        return []
    actors = {role for _, _, role in find_roles(words(_SITE_PARTY.sub("", q)))}
    if len(actors) != 1:
        return []
    actor = next(iter(actors))
    groups = defaultdict(list)
    for a in rows:
        if a.ref.column is None and _START_WORK.search(fold(a.section_title)) and a.context.endswith(":"):
            lead = words(a.context)
            if find_roles(lead) == [(0, len(lead), actor)]:
                groups[a.ref.doc_id, a.ref.version_no, a.ref.point].append(a)
    destination = _site_party(q)
    choices = []
    for group in groups.values():
        checklist = []
        for a in sorted(group, key=lambda item: item.ref.sentence):
            # A colon-headed list continues through semicolons and ends with a full stop.
            text = fold(a.text)
            if (a.ref.sentence != len(checklist) + 1 or not _ACTION_ITEM.match(text)
                    or _AFTER_TRIP.search(text) or _OTHER_PURPOSE.search(text)):
                break
            checklist.append(a)
            if a.text.endswith("."):
                if any(_SITE_WORK.search(fold(item.text)) and _site_party(fold(item.text)) == destination for item in checklist):
                    choices.append(checklist)
                break
            if not a.text.endswith(";"):
                break
    if len(choices) != 1:
        return []
    chosen = choices[0]
    if any(a not in chosen and _BEFORE_TRIP.search(fold(a.text)) and _PREP_ACTION.search(fold(a.text)) for a in rows):
        return []  # A direct pre-departure rule must remain available for ordinary comparison.
    return chosen


def _applicable_condition(text, question):
    question_terms = terms(question)
    negated = {k for token in re.findall(r"\bне\s+([\w-]+)", question) for k in terms(token)}
    if _PUBLIC.search(question):
        question_terms |= _PUBLIC_TERMS
    for match in _CONDITION.finditer(text.removeprefix("при этом ")):
        trigger = match[1]
        allowed = question_terms
        if _EQUIVALENT.search(trigger) and re.search(r"\b(?:наличи|доступ|есть)", trigger):
            allowed = allowed | _AVAILABILITY
        required = terms(trigger)
        negated_trigger = {k for token in re.findall(r"\bне\s+([\w-]+)", trigger) for k in terms(token)}
        if not required <= allowed or (negated ^ negated_trigger) & required:
            return False
    return True


def _substitution(question, group):
    primary = [a for a in group if _EQUIVALENT.search(fold(a.text)) and _NORM.search(fold(a.text))
               and _applicable_condition(fold(a.text), question)]
    if not primary:
        return []
    if not _PUBLIC.search(question):
        return [a for a in primary if not _PUBLIC.search(fold(a.text))]
    specific = [a for a in group if _PUBLIC.search(fold(a.text))
                and re.search(r"замен|эквивалент|аналог", fold(a.text)) and _NORM.search(fold(a.text))
                and _applicable_condition(fold(a.text), question)]
    return [*primary, *specific] if specific else []


def _escalation(question, group):
    subject = {m[1] for clause in re.split(r"[,;:.?!]", question) if _BAD_STATE.search(clause)
               for m in _STATE_SUBJECT.finditer(clause)}
    if len(subject) != 1:
        return []
    direct = []
    for a in group:
        text = fold(a.text)
        event = re.split(r"[,;:]", text, maxsplit=1)[0]
        if (_ESCALATE.search(text) and _BAD_STATE.search(event) and _CHANGE.search(event)
                and not _NEGATED_CHANGE.search(event)
                and subject == {m[1] for m in _STATE_SUBJECT.finditer(event)}):
            direct.append(a)
    if not direct:
        return []
    first = min(a.ref.sentence for a in direct)
    # Only the next escalation or the message about this status is a continuation of the trigger.
    return [a for a in group if a in direct or (a.ref.sentence > first and _STATUS.search(fold(a.text)) and (
        re.search(r"\bв (?:письме|сообщении|уведомлении)\b", fold(a.text))
        or (_NEXT_LEVEL.search(fold(a.text)) and _ESCALATE.search(fold(a.text)))))]


def _preparation(_question, group):
    return [a for a in group if _PREP_ACTION.search(fold(a.text))
            and (_BEFORE_TRIP.search(fold(a.text))
                 or (not _AFTER_TRIP.search(fold(a.text)) and
                     re.search(r"\bподготовк\w*\s+(?:к\s+)?(?:выезд|посещени|поездк)", fold(a.section_title))))]


def applicable_refs(question, verified):
    """Only a selected source can establish a rule or add a qualifier from that same provision."""
    q = fold(question)
    item = _EQUIVALENT.search(q)
    if item and _SUPPLY_EQUIVALENT.search(q[:item.start()]):
        select = _substitution
    elif _ESCALATE.search(q) and _BAD_STATE.search(q):
        if _NEGATED_CHANGE.search(q):
            return frozenset()
        select = _escalation
    elif _TRIP.search(q) and _PREPARE.search(q):
        if _CANCELLED.search(q):
            return frozenset()
        select = _preparation
    else:
        return None
    groups = defaultdict(list)
    for a in verified:
        groups[(a.ref.doc_id, a.ref.version_no, a.ref.point)].append(a)
    return frozenset(a.ref for group in groups.values() for a in select(q, group))
