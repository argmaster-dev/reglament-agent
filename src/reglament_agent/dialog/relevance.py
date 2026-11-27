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
_SIGN = re.compile(r"подпис|заключ")
_SIGN_ROLE = re.compile(r"подпис(?:ание|ывает|ать|ыва)|заключ(?:ение|ает|ить)")
_CONTRACTOR = re.compile(r"(?:суб)?подрядчик|внешн\w* исполнител")
_PRIORITY = re.compile(r"приоритет")


def terms(text):
    def concept(word):
        if word.startswith("марж"):
            return "маржа"
        if word.startswith(("сделк", "сделок")):
            return "сделка"
        return word_key(word)

    return {concept(w) for w in words(text) if w not in _STOP and len(w) > 1}


def asks_signer(q):
    return bool(re.search(r"\bкто\b|ответственн\w* за", q) and _SIGN.search(q))


def signer_pointer(q, anchor):
    """A sentence that sends the subject of a signer question to another document names no signer itself,
    but it is the point the answer must cite (with the remark that the signer is not named there)."""
    if not external_reference(anchor) or _SIGN_ROLE.search(fold(anchor.text)):
        return False
    subject = terms(q) - terms("подписывает заключает подписание заключение кто")
    return bool(subject & terms(anchor.text)) and bool(_CONTRACTOR.search(fold(anchor.text)) or not _CONTRACTOR.search(q))


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


_QUALIFIER_PREPS = ("на", "от", "для")
_NOUN_QUALIFIER = re.compile(r"\b(\w{3,})\s+(на|от|для)\s+(\w+)")


def _head(word):
    # «поставщик» and «субпоставщик» name the same kind of counterparty.
    return word_key(word[3:] if word.startswith("суб") and len(word) > 7 else word)


def request_qualifiers(question):
    """(noun key, preposition, head key) for each «noun на/от/для X» phrase of an executor question."""
    q = fold(question)
    if not re.search(r"\bкто\b", q):
        return []
    return [
        (word_key(noun), prep, _head(head))
        for noun, prep, head in _NOUN_QUALIFIER.findall(q)
        if noun not in _STOP and noun not in _QUALIFIER_PREPS
    ]


def _sentence_qualifiers(sentence, noun):
    toks = words(sentence)
    found = [
        (toks[i + 1], _head(toks[i + 2]))
        for i in range(len(toks) - 2)
        if word_key(toks[i]) == noun and toks[i + 1] in _QUALIFIER_PREPS
    ]
    present = any(word_key(t) == noun for t in toks)
    return present, found


def _conflicts(question, text):
    for noun, prep, head in request_qualifiers(question):
        present, found = _sentence_qualifiers(text, noun)
        # An unqualified mention carries no contradicting object; a differing preposition or head does.
        if present and found and (prep, head) not in found:
            return (noun, prep, head)
    return None


def qualifier_conflict(question, anchor):
    """The sentence names the requested noun for another object or direction than the question asks."""
    return _conflicts(question, anchor.text) is not None


def near_basis(question, rows, limit=3):
    """Sentences about the same noun with another object or direction, named openly instead of substituted."""
    lines, seen = [], set()
    for a in rows:
        if _conflicts(question, a.text) and (a.ref.doc_id, a.ref.point) not in seen:
            seen.add((a.ref.doc_id, a.ref.point))
            lines.append(f"п. {a.ref.point} «{a.ref.doc_title}» ({a.ref.department}): {a.text}")
        if len(lines) >= limit:
            break
    return "\n".join(lines)


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
    if qualifier_conflict(question, anchor):
        return False
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
    if asks_signer(q):
        if signer_pointer(q, anchor):
            return True
        if not _SIGN_ROLE.search(sentence):
            return False
        if re.search(r"договор|контракт|соглашен", q):
            # Bind the operation to the requested artifact: signing a delivery note is a different act.
            contract = r"(?:договор|контракт|соглашени)"
            direct = r"(?:подпис|заключ)\w*\s+(?:(?:рамочн|агентск|нов|проектн)\w*\s+)?" + contract
            inverse = contract + r"\w*[^.;]{0,42}(?:подписывает|подписан|заключает)"
            if not re.search(direct + "|" + inverse, sentence):
                return False
        if anchor.ref.point.isdigit() and not find_roles(words(sentence)):
            return False
        if _CONTRACTOR.search(q):
            return bool(_CONTRACTOR.search(sentence + " " + context))
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


def applicable_refs(question, rows, *, department=None):
    """Only clarify if several scopes have concrete evidence for the requested operation."""
    q = fold(question)
    scoped_terms = {word_key(word) for word in words(department)} if department else set()
    if asks_amount(q):
        # The amount for an individual transaction also depends on its inputs.
        return set()
    if _ADVANCE.search(q) or re.search(r"кто .*подпис|кто .*заключ", q):
        relevant = [a for a in rows if matches_request(q, a)]
    elif re.search(r"без\s+\w+", q):
        keys = terms(q) - scoped_terms
        relevant = [a for a in rows if len(keys & terms(a.text + " " + a.context)) >= 2]
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
