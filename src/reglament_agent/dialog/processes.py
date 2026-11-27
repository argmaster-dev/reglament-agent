"""Navigation through actual sections of published regulations."""

import re

from .context import without_scope
from .lexicon import fold
from .relevance import terms


# These constructions leave the intended operation open. A specific object or condition
# still takes precedence over the invitation to choose a process.
_OPEN = re.compile(
    r"\b(?:что\s+(?:мне\s+)?(?:делать|сделать)|как\s+(?:мне\s+)?(?:быть|поступить|разобраться)|"
    r"(?P<start>с\s+чего\s+(?:мне\s+)?начать|как\s+(?:мне\s+)?(?:запустить|начать|стартовать))|"
    r"(?:помогите|помоги|нужна\s+помощь|хочу\s+разобраться)\s+(?:мне\s+)?(?:с|в)\b|"
    r"(?:подскажите|подскажи|вопрос)\s+(?:по|насчет)\b|"
    r"кто\s+(?:за\s+(?:это|такое)\s+(?:отвечает|ответственен)|(?:отвечает|ответственен)\s+за\s+это))\b"
)
_START = re.compile(r"\b(?:старт|инициац|запуск|запуст|открыти|заведени|начал)\w*")
_CONDITION = re.compile(r"\b(?:если|после|перед|когда|без|только|срок|сколько)\b")


_DOCUMENT = r"(?:регламент|документ|инструкци|положени|правил)\w*"
_DOCUMENT_MODIFIERS = r"(?:(?:внутренн|действующ|локальн|корпоративн)\w*\s+){0,2}"
_FIND_DOCUMENT = re.compile(
    rf"\b(?:в|по)\s+како(?:м|й|му)\s+{_DOCUMENT_MODIFIERS}(?:регламент|инструкци|положени)\w*\b"
    rf"|\b(?:в\s+како(?:м|й)|каким|какой|какая|какое|какими)\s+{_DOCUMENT_MODIFIERS}{_DOCUMENT}\b"
    rf"[^.!?]{{0,75}}\b(?:опис|указ|устанавлив|установлен|регулиру|определя|содерж|закреп|пропис)\w*\b"
    rf"|\b(?:(?:где|как)\s+(?:можно\s+)?(?:найти|открыть|посмотреть|прочитать)|"
    rf"найди(?:те)?|покажи(?:те)?|пришли(?:те)?|назови(?:те)?)\s+(?:мне\s+)?{_DOCUMENT_MODIFIERS}{_DOCUMENT}\b"
    rf"|\b(?:дайте|дай|пришлите|пришли|покажи(?:те)?|нужна)\s+(?:мне\s+)?ссылк[уа]\s+на\s+{_DOCUMENT_MODIFIERS}{_DOCUMENT}\b"
)


def document_summary(question, statements):
    """Name the regulations behind a verified answer when the user asks where to find them."""
    if not _FIND_DOCUMENT.search(fold(question)):
        return None
    titles = dict.fromkeys(ref.doc_title for st in statements for ref in st.sources if ref.faq_id is None)
    if titles:
        return "См. " + ", ".join(f"«{title}»" for title in titles) + "."
    return None


def broad_subject(question, scope=None):
    text = fold(question)
    opening = _OPEN.search(text)
    if not opening or _CONDITION.search(text):
        return None
    remainder = text[:opening.start()] + " " + text[opening.end():]
    subject = terms(without_scope(remainder, scope) if scope else remainder)
    if 1 <= len(subject) <= 2:
        return subject, bool(opening['start'])
    return None


def options(question, points, scope=None):
    """Offer different named processes with direct subject overlap, keeping real point identities."""
    request = broad_subject(question, scope)
    if not request:
        return []
    subject, start = request
    ranked = []
    for rank, point in enumerate(points):
        title = point.section_title.strip()
        if not title or '.' not in point.number or not point.sentences:
            continue
        heading = terms(title)
        body = terms(point.text)
        if not subject <= heading | body:
            continue
        if start and not _START.search(fold(title + ' ' + point.sentences[0])):
            continue
        ranked.append((start and not _START.search(fold(title)),
                       -len(subject & heading), -len(subject & body), rank, title, point))
    choices = {}
    for *_, title, point in sorted(ranked):
        choices.setdefault(fold(title), (title, point.source_ref()))
        if len(choices) == 4:
            break
    return list(choices.values()) if len(choices) >= 2 else []


def section(ref):
    return ref.doc_id, ref.version_no, ref.point.split('.', 1)[0]


def related(statements, points):
    """Return other points in the same version and section as verified statements."""
    bases = {ref for statement in statements for ref in statement.sources if ref.faq_id is None}
    sections = {section(ref) for ref in bases}
    seen = {(section(ref), ref.point) for ref in bases}
    result = []
    for point in points:
        if '.' not in point.number:
            continue
        ref = point.source_ref()
        key = section(ref), ref.point
        if key[0] in sections and key not in seen:
            seen.add(key)
            result.append(ref)
            if len(result) == 3:
                break
    return result
