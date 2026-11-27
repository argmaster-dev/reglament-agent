"""Rules for relating a regulation sentence to the requested operation.

Question signals (actor, deadline, precondition, external reference) filter
candidate evidence. These patterns describe regulatory language across documents.
The extracted sentence and its precise reference remain the source of the answer.
"""

import re

from reglament_agent.answer import Statement
from reglament_agent.storage.search import QUESTION_WORDS

from .lexicon import find_roles, fold, word_key, words

_STOP = QUESTION_WORDS | {
    "это",
    "техническая",
    "вы",
    "на",
    "будет",
    "от",
    "при",
    "дирекция",
    "до",
    "если",
    "мне",
    "над",
    "работаю",
    "этой",
    "под",
    "из",
    "по",
    "без",
    "про",
    "за",
    "еще",
    "какие",
    "ли",
    "вашего",
    "ваш",
    "для",
    "изо",
}
_ADVANCE = re.compile(r"за сколько|заранее|за какое время|за какой срок")
_INTERVAL = re.compile(r"\bза\b.{0,40}\bдо\b")
# A question about a time limit, however it is phrased (how far ahead, how long, until when, how many days).
_DEADLINE_ASK = re.compile(
    r"срок|за сколько|заранее|за како[ей] (?:время|срок)|в течение како|как долго|как быстро|до како\w* (?:числа|момента)"
    r"|не позднее како|не раньше како|сколько\s+(?:\w+\s+)?(?:дн|дне|недел|час|месяц|времени|суток)|когда\s+(?:нужно|надо|следует|необходимо|должн|можно|подава|сда)"
)
_UNIT = r"(?:дн|недел|час|месяц|минут|суток|квартал)"
_NUMBER = r"(?:\d+|одн\w*|два|две|три|четыре|пять|шесть|семь|восемь|девять|десять|двадцать|тридцать)"
_TIME_VALUE = re.compile(
    rf"{_NUMBER}\s+(?:(?:рабоч|календарн)\w*\s+)?{_UNIT}|в течение|не позднее|не менее чем за|заранее|в срок"
)
_DEADLINE_WORDS = {word_key(w) for w in (
    "срок срока сроки время времени день дня дней недели неделя час часов нужно надо следует необходимо "
    "должен должна можно течение позднее раньше долго быстро сколько какой какого когда числа момента".split())}
_SIGNING_ACTION = re.compile(
    r"\b(?:(?P<nominal>подписани\w*|заключени\w*)"
    r"|(?P<active>подпис(?:ать|ал[аи]?|ыва(?:ет|ют|ть|л[аи]?))|заключ(?:ить|ил[аи]?|а(?:ет|ют|ть|л[аи]?)))"
    r"|(?P<passive>подпис(?:ан[аоы]?|ыва(?:ется|ются))|заключ(?:ен[аоы]?|а(?:ется|ются))))\b"
)
_SIGNING_ACTOR_QUERY = re.compile(r"\b(?:кто|кем|кому|на ком)\b|\bответственн\w* за")
_SIGNING_WORK = re.compile(
    r"\b(?:(?P<initiate>инициир\w*|инициатор\w*)|(?P<organize>организ\w*)"
    r"|(?P<coordinate>координир\w*)|(?P<support>сопровожд\w*)|(?P<prepare>подготов\w*|готови\w*)"
    r"|(?P<approve>согласовыва\w*|согласу\w*|согласовани\w*))\b"
)
_SIGNING_DUTY = re.compile(
    r"\b(?:отвеча(?:ет|ют)|ответственн(?:ый|ая|ые|ым|ыми|ому|ой|ен|а|ы)|обязан\w*|возлагается на"
    r"|выполня(?:ет|ют)|осуществля(?:ет|ют))\b"
)
_SIGNING_PASSIVE_DUTY = re.compile(r"\b(?:выполня|осуществля)(?:ется|ются)\b")
_SIGNING_BACKGROUND = re.compile(r"\b(?:для|к|до|после|при|аналогично|подобно)\s+$")
_SIGNING_DOCUMENT = re.compile(
    r"\b(?:(?P<addendum>дополнительн\w*\s+соглашени\w*|допсоглашени\w*|дс)"
    r"|(?P<contract>договор\w*|контракт\w*)|(?P<agreement>соглашени\w*)"
    r"|(?P<upd>упд)|(?P<act>акт(?:ов|ами|ах|ом|а|у|е|ы)?))\b"
)
_CONTRACTOR = re.compile(r"(?:суб)?подряд(?:чик|н)\w*|внешн\w* исполнител")
_PRIORITY = re.compile(r"приоритет")


def terms(text):
    def concept(word):
        if word.startswith("марж"):
            return "маржа"
        if word.startswith(("сделк", "сделок")):
            return "сделка"
        return word_key(word)

    return {concept(w) for w in words(text) if w not in _STOP and len(w) > 1}


def _signing_work(text, action):
    """An ancillary role can govern a noun for signing, never a finite signing verb."""
    if action.lastgroup != "nominal":
        return None
    for work in _SIGNING_WORK.finditer(text):
        if work.end() <= action.start() and action.start() - work.end() <= 45:
            between = text[work.end():action.start()]
            if not re.search(r"[,.;!?]|\bи\b", between):
                return work.lastgroup
        if action.end() <= work.start() and work.start() - action.end() <= 75:
            between = text[action.end():work.start()]
            if not re.search(r"[,.;!?]|\bи\b", between):
                return work.lastgroup
    return None


def signing_role(question):
    """Classify requests for a signer or a supporting role in the same operation."""
    q = fold(question)
    if not _SIGNING_ACTOR_QUERY.search(q):
        return None
    for action in _SIGNING_ACTION.finditer(q):
        work = _signing_work(q, action)
        if work:
            return work
        if not _SIGNING_BACKGROUND.search(q[:action.start()]):
            return "signer"
    return "signer" if re.search(r"\bподписант\w*\b", q) else None


def asks_signer(question):
    return signing_role(question) == "signer"


def _document_for_action(text, action=None):
    if action is None:
        document = _SIGNING_DOCUMENT.search(text)
    else:
        def distance(document):
            return max(action.start() - document.end(), document.start() - action.end(), 0)

        document = min(_SIGNING_DOCUMENT.finditer(text), key=distance, default=None)
    return document.lastgroup if document else None


def _answers_signing_role(question, sentence, role):
    q = fold(question)
    document = _document_for_action(q, _SIGNING_ACTION.search(q))
    for clause in re.split(r"[.;!?]|,\s*(?=(?:а|но)\s)", sentence):
        for action in _SIGNING_ACTION.finditer(clause):
            if document and _document_for_action(clause, action) != document:
                continue
            work = _signing_work(clause, action)
            if role != "signer":
                if work == role:
                    return True
                continue
            if work or _SIGNING_BACKGROUND.search(clause[:action.start()]):
                continue
            if action.lastgroup == "active" or action.lastgroup == "nominal" and _SIGNING_DUTY.search(clause):
                return True
            if (action.lastgroup == "passive" or _SIGNING_PASSIVE_DUTY.search(clause)) and find_roles(words(clause)):
                return True
    return False


def signer_pointer(q, anchor):
    """Allow a relevant external citation to explain why nobody is named as signer."""
    q, sentence = fold(q), fold(anchor.text)
    document = _document_for_action(q, _SIGNING_ACTION.search(q))
    if not external_reference(anchor) or document and _document_for_action(sentence) != document:
        return False
    if _answers_signing_role(q, sentence, "signer"):
        return False
    subject = terms(q) - terms("подписывает заключает подписание заключение кто")
    return bool(subject & terms(anchor.text)) and bool(_CONTRACTOR.search(sentence) or not _CONTRACTOR.search(q))


def asks_amount(q):
    return bool(re.search(r"како[йяев] .*будет|какая .*будет|сколько .*получ|чему рав", q))


def external_reference(a):
    return bool(
        re.search(
            r"(?:согласно|соответствии с|см\.|смотри|определ\w* в|регулиру\w*).{0,65}"
            r"(?:регламент|положени|инструкц|политик|методик)",
            fold(a.text),
        )
    )


def start_permission(question):
    return bool(
        re.search(r"можно ли|разреш|допустим", question)
        and re.search(r"начать|начинать|старт|приступ", question)
        and re.search(r"работ|исполн|проект", question)
    )


def entry_condition(text):
    return bool(re.search(r"(?:проект|исполн|работ).{0,35}начина.{0,45}услови", text))


_ASK = r"(?:кто|что|как\w*|когда|где|сколько|зачем|почему|куда|откуда|чей|чья|чьё|чьи|кому|кого|чего|чем)"
_PART_SPLIT = re.compile(
    rf"\s+и\s+(?={_ASK}\b)|,?\s+а\s+также\s+|,\s*(?={_ASK}\b)|\s*;\s*"
)


def question_parts(question):
    """Interrogative parts of a compound question; a simple question is one part."""
    q = fold(question)
    parts = [p.strip(" ?!.") for p in _PART_SPLIT.split(q)]
    parts = [p for p in parts if p]
    # «а также» and «;» may join a part that carries no question word of its own: keep it with its neighbour.
    merged = []
    for p in parts:
        if merged and not re.search(rf"\b{_ASK}\b", p):
            merged[-1] += " " + p
        else:
            merged.append(p)
    return merged or [q]


def matches_request(question, anchor):
    parts = question_parts(question)
    return any(_matches_part(p, anchor) for p in parts)


def uncovered_parts(question, statements, rows):
    """Parts of a compound question for which no selected sentence shares any subject term."""
    parts = question_parts(question)
    if len(parts) < 2:
        return []
    by_ref = {a.ref: a for a in rows}
    texts = [fold(by_ref[r].text) if r in by_ref else fold(st.text) for st in statements for r in st.sources]
    missing = []
    for part in parts:
        keys = terms(part)
        if keys and not any(len(keys & terms(t)) >= min(2, len(keys)) for t in texts):
            missing.append(part)
    return missing


def _matches_part(question, anchor):
    q, sentence, context = fold(question), fold(anchor.text), fold(anchor.context)
    if _ADVANCE.search(q):
        return bool(_INTERVAL.search(sentence))
    if start_permission(q):
        if entry_condition(context) or entry_condition(sentence):
            return True
        # A downstream action due after signature does not grant permission to start.
        if re.search(r"с (?:момента|даты|дня) подписания|не позднее.{0,65}до начала", sentence):
            return False
        return bool(
            re.search(r"(запрещ|согласован|разреш|услови|исключени|гарант|допуск|только при)", sentence)
            and re.search(r"нач[аи]|старт|приступ|работ", sentence + " " + context)
        )
    role = signing_role(q)
    if role:
        if role == "signer" and signer_pointer(q, anchor):
            return True
        if _CONTRACTOR.search(q) and not _CONTRACTOR.search(sentence + " " + context):
            return False
        if anchor.ref.point.isdigit() and not find_roles(words(sentence)):
            return False
        return _answers_signing_role(q, sentence, role)
    if re.search(r"кто .*созда", q):
        return "созда" in sentence and ("заявк" not in q or "заявк" in sentence)
    return True


def score(question, anchor):
    query = terms(question)
    direct = terms(anchor.text)
    context = terms(anchor.context)
    value = 2 * len(query & direct) + 0.45 * len(query & context)
    if _ADVANCE.search(fold(question)) and _INTERVAL.search(fold(anchor.text)):
        value += 7
    if anchor.ref.column and ("обязат" in query or "колонк" in query):
        value += 5
    if _PRIORITY.search(fold(question)) and _PRIORITY.search(fold(anchor.text)):
        value += 4
    if start_permission(fold(question)) and (
        entry_condition(fold(anchor.context)) or entry_condition(fold(anchor.text))
    ):
        value += 10
    if asks_amount(fold(question)) and external_reference(anchor):
        value += 8
    return value + 2 / (1 + anchor.rank)


def asks_deadline(q):
    q = fold(q)
    # «Кто контролирует сроки?» asks for a performer; the time limit is only its subject.
    return bool(_DEADLINE_ASK.search(q)) and not re.search(r"\bкто\b", q)


def deadline_subject(question, department=None):
    scoped = {word_key(word) for word in words(department)} if department else set()
    return terms(question) - _DEADLINE_WORDS - scoped


def states_deadline(question, anchor, *, department=None, point_terms=None):
    """A sentence with a time value in a point that concerns the subject the question names."""
    if not _TIME_VALUE.search(fold(anchor.text)):
        return False
    keys = deadline_subject(question, department)
    own = terms(anchor.text + " " + anchor.context)
    # A note or exception of a point rarely repeats the subject of its heading sentence: the point supplies it.
    whole = own | (point_terms or {}).get((anchor.ref.doc_id, anchor.ref.point), set())
    return bool(keys) and len(keys & whole) >= min(2, len(keys))


def applicable_refs(question, rows, *, department=None):
    """Only clarify if several scopes have concrete evidence for the requested operation."""
    q = fold(question)
    scoped_terms = {word_key(word) for word in words(department)} if department else set()
    if asks_amount(q):
        # The amount for an individual transaction also depends on its inputs.
        return set()
    if asks_deadline(q):
        # The wording of a time-limit question must not decide whether several departments are compared.
        point_terms = {}
        for a in rows:
            point_terms.setdefault((a.ref.doc_id, a.ref.point), set()).update(terms(a.text))
        relevant = [
            a for a in rows
            if states_deadline(q, a, department=department, point_terms=point_terms)
            or (_ADVANCE.search(q) and matches_request(q, a))
        ]
    elif _ADVANCE.search(q) or signing_role(q):
        relevant = [a for a in rows if matches_request(q, a)]
    elif re.search(r"без\s+\w+", q):
        keys = terms(q) - scoped_terms
        found = {id(a): keys & terms(a.text + " " + a.context) for a in rows}
        # Two shared common words do not make a sentence about the condition the question names: the words of
        # the «без …» phrase itself must all be there.
        phrase = {k for k in terms(" ".join(re.search(r"без\s+((?:\w+\s*){1,2})", q)[1].split())) if k in keys}
        relevant = [a for a in rows if len(found[id(a)]) >= 2 and phrase <= found[id(a)]]
    else:
        keys = terms(q) - scoped_terms
        relevant = [a for a in rows if keys and keys <= terms(a.text)]
    return {a.ref for a in relevant}


def expand_selected(question, statements, items, rows):
    q = fold(question)
    # Once the model identifies a relevant table, enumerate the requested column in source order.
    if re.search(r"какие|перечисл|список|перечень", q):
        selected = [
            a for a in items.values() if a.ref.column and any(a.ref == s.sources[0] for s in statements)
        ]
        if selected:
            primary = selected[0].ref
            cols = {a.ref.column for a in selected}
            if "обязат" in q:
                cols = {a.ref.column for a in selected if "обязат" in fold(a.ref.column)}
            complete = sorted(
                (
                    a
                    for a in rows
                    if a.ref.doc_id == primary.doc_id
                    and a.ref.point == primary.point
                    and a.ref.column in cols
                ),
                key=lambda a: a.ref.sentence,
            )
            if complete:
                return [Statement(f"{a.ref.column}: {a.text}", [a.ref]) for a in complete]
    return statements


def companion_sentences(statements, rows):
    """Resolve immediate anaphora within the selected point, with its own citation."""
    by_position = {(a.ref.doc_id, a.ref.point, a.ref.sentence): a for a in rows if not a.ref.column}
    collected = []
    for st in statements:
        ref = st.sources[0]
        prefix = (ref.doc_id, ref.point)
        group = [st]
        if not ref.column:
            preceding = by_position.get((*prefix, ref.sentence - 1))
            following = by_position.get((*prefix, ref.sentence + 1))
            if preceding and re.match(r"Если (?:этот|данный) срок", st.text, re.IGNORECASE):
                group.insert(0, Statement(preceding.text, [preceding.ref]))
            if following and re.match(r"Для этого (?:он|она|они) привлека", following.text, re.IGNORECASE):
                group.append(Statement(following.text, [following.ref]))
        collected.extend(s for s in group if s not in collected)
    return collected


def answers_advance_deadline(question):
    return bool(_ADVANCE.search(fold(question)))


def conflicting_priority(question, statements):
    if not _PRIORITY.search(fold(question)):
        return False
    baseline = any(re.search(r"приоритет(?: заявки)? не меняется", fold(s.text)) for s in statements)
    override = any(re.search(r"может.{0,35}(понизить|повысить) приоритет", fold(s.text)) for s in statements)
    return baseline and override
