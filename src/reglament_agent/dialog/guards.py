"""Rules about what the user's text may do: invent content, steer the route, assert a false premise.

The patterns describe request styles and word families, never the content of a regulation.
"""

import math
import re
from typing import Literal

from .checks import _numbers
from .lexicon import find_roles, fold, words

MIN_LENGTH, MAX_LENGTH = 3, 2000

LEGAL_MESSAGE = "Это вопрос законодательства. По нему лучше обратиться к юристам."
OFF_TOPIC_MESSAGE = "Этот вопрос не касается регламентов компании — обратитесь к своему руководителю."

# Ask for a policy even when the subject is money, a particular deal or correspondence.
_PROCEDURE = re.compile(
    r"\b(?:регламент|положени|инструкци|правил|порядок|порядк|методик|формул|услови|обязан|положен|предусмотрен|ограничени)\w*"
    r"|\b(?:как|кто|где|можно ли|нужно ли|имею ли право|каким образом)\b"
    r"|\b(?:применять|применяется|использовать|учитывать)\b"
)
_METRIC = re.compile(r"\b(?:марж|преми|бонус|прибыл|выручк)\w*")
_RECORD = re.compile(
    r"\b(?:меня|мне|мо[йяею]|моих|наша?|нашу|текущ\w*|фактическ\w*|сегодня|сейчас|"
    r"(?:по|этой|эта|той|конкретной) сделк\w*|сделк\w*\s+№|за квартал|за месяц)\b"
)
_EXCHANGE = re.compile(
    r"\b(?:курс\w*\s+(?:\w+\s+)?(?:валют|доллар|евро|юан|рубл|фунт|usd|eur)\w*"
    r"|стоит\s+(?:доллар|евро|юан|фунт)\w*)\b"
)
_TASK = re.compile(
    r"\b(?:сделай(?:те)?|отправь(?:те)?|напиши(?:те)?|разошли(?:те)?|подготовь(?:те)?|"
    r"составь(?:те)?|сформируй(?:те)?)\b.{0,65}"
    r"\b(?:follow[\s-]?up|фолл?оу[\s-]?ап|письм\w*|напоминани\w*|сообщени\w*|рассылк\w*)\b"
)


def operational_request(text):
    """Requests for actual records, market data or external work have no regulatory answer."""
    q = fold(text)
    if _PROCEDURE.search(q):
        return False
    if q.startswith("когда") and not re.search(r"\b(?:мне|нам|мо[яию]|наш[ау])\b", q):
        return False
    return bool(_EXCHANGE.search(q) or _TASK.search(q) or (_METRIC.search(q) and _RECORD.search(q)))


# A legal aspect of an internal task does not make the requested workflow a legal consultation.
_INTERNAL_PROCESS = re.compile(
    r"\b(?:регламент\w*|crm|ткп|у нас)\b"
    r"|\b(?:внутренн\w*|корпоративн\w*)\s+(?:правил|поряд|полити|инструкц|положен|процесс)\w*"
    r"|\bнаш\w*\s+(?:компани|организаци|поряд|правил|положен|инструкц|политик|процедур|отдел|проект)\w*"
    r"|\b(?:кто|кому|куда|как)\b[^.!?]{0,60}\b(?:согласу|согласова|эскалир|переда|завод)\w*"
    r"|\b(?:карточк\w*\s+проект\w*|заявк\w*\s+на\s+\w+|на согласование)\b"
    r"|\b(?:оформ|заполн|пода|согласу|утвержд)\w*\b[^.!?]{0,32}\b(?:командиров|смет|табел|заявк)\w*"
)
_LAW_SOURCE = re.compile(
    r"\b(?:закон(?:[ауеы]|ом|ов|ами|ах)?|законн[аоы]|правомерн[аоы]|законодательств\w*|(?:трудов|гражданск|налогов|семейн|уголовн)\w*\s+кодекс\w*)\b"
    r"|\b(?:(?:по|согласно)\s+(?:тк|гк|нк|ск|ук|коап)|(?:тк|гк|нк|ск|ук|коап)\s+рф|\d+[‐‑–-]фз)\b"
)
_STATUTORY_ISSUE = re.compile(
    r"\b(?:исков\w*\s+давност\w*|подсудност\w*|срок\w*\s+обжаловани\w*)\b"
    r"|\b(?:налогов\w*\s+ставк\w*|ставк\w*\s+(?:налог\w*|ндс|ндфл))\b"
    r"|\b(?:расторг\w*|расторж\w*|отказ\w*|прекра\w*)\b[^.!?]{0,65}\bодносторонн\w*\b"
)
_LEGAL_DEFINITION = re.compile(
    r"\b(?:что (?:такое|считается|признается|относится)|определени\w*)\b"
    r"[^.!?]{0,75}\b(?:персональн\w*\s+данн\w*|коммерческ\w*\s+тайн\w*|"
    r"доверенност\w*|правонарушени\w*|авторск\w*\s+прав\w*)\b"
)


def redirect_for(text: str) -> Literal["legal", "manager"] | None:
    """Identify an explicit request for outside expertise; ambiguous workflow questions continue to search."""
    q = fold(text)
    legal = _LAW_SOURCE.search(q) or _STATUTORY_ISSUE.search(q) or _LEGAL_DEFINITION.search(q)
    if legal and not _INTERNAL_PROCESS.search(q):
        return "legal"
    if operational_request(q):
        return "manager"
    return None


# Verbs of invention and guessing, matched by stem so any inflection is covered.
_INVENT_VERB = re.compile(
    r"\b(?:при|вы)ду?м(?:ай|ать|аешь|аете|ал\w*|ает|ают|ку|ка)\b|\b(?:при|вы)думан\w*|\bсочин\w*|\bизмысл\w*"
    r"|\b(?:с)?фантазир\w*|\bвообраз\w*|\bпредположи\w*|\bприкин\w*|\bприкид\w*|\bнаугад\b|\bнавскидку\b"
    r"|\bна свое[мй] усмотрени\w*|\bот себя\b|\bпредставь\b",
    re.IGNORECASE,
)
_APPROXIMATE = re.compile(
    r"\b(?:при)?мерн\w*\s+(?:срок|значени|цифр|числ|вариант|оценк|размер|сумм)\w*"
    r"|\b(?:примерно|приблизительно|ориентировочно|хотя бы)\s+(?:назови|скажи|дай|укажи)\w*"
    r"|\b(?:назови|скажи|дай|укажи)\w*\s+(?:хотя бы\s+)?(?:при)?(?:мерн|близительн|ентировочн)\w*"
    r"|\bприблизительн\w+\s+(?:срок|значени|цифр|числ|размер|сумм)\w*",
    re.IGNORECASE,
)
_AS_OFFICIAL = re.compile(
    r"\b(?:назов\w*|выда\w*|объяв\w*|подай\w*|предста\w*|сошл\w*|оформ\w*)\b.{0,40}\b"
    r"(?:официальн|действующ|настоящ|реальн)\w*"
    r"|\b(?:как|за)\s+(?:официальн|действующ)\w*"
    r"|\bофициальн\w*\s+(?:назов|выда|числ|цифр)\w*"
    r"|\bкак\s+(?:он|она|оно)\s+должн\w*\s+(?:звуч|выгляд)\w*"
    r"|\bсформулиру\w*\s+(?:за|вместо)\s+\w+",
    re.IGNORECASE,
)

_INSTRUCTION_LEAD = re.compile(
    r"\b(?:игнорир\w*|забуд\w*|отмени\w*|отключи\w*|сними\w*|нарушь\w*|обойди\w*)\b[^.!?:;]{0,50}?"
    r"\b(?:инструкц|правил|предыдущ|прежн|ограничен|запрет|указани|настройк)\w*"
    r"(?:\s+(?:инструкц|правил|предыдущ|прежн|ограничен|запрет|указани|настройк)\w*)*\s*(?:и\s+|,\s*|[:.]\s*)?",
    re.IGNORECASE,
)
_ROLE_LABEL = re.compile(
    r"\b(?:system|assistant|developer|систем\w*|ассистент\w*)\s*(?:prompt|промпт)?\s*:\s*"
    r"(?:(?:ограничени|правил|запрет|инструкци)\w*\s+(?:сняты|отключены|отменены)\s*[.!]?\s*)?",
    re.IGNORECASE,
)
_MODE_PHRASE = re.compile(
    r"\b(?:правила|ограничения|запреты|инструкции)\s+(?:сняты|отключены|отменены)\s*[.!]?"
    r"|\bрежим\s+(?:разработчика|администратора|без ограничений)\b|\bты теперь\s+\w+",
    re.IGNORECASE,
)
_FORMAT = re.compile(
    r"\bзаполни\w*\s+поле\s+\w+\s+значением\s+[«\"'][^»\"']*[»\"']\s*[:,]?"
    r"|\b(?:ответь|ответьте|выведи|верни|дай)\w*\s+в\s+формате\s+[\w-]+\s*"
    r"|\bв\s+формате\s+(?:json|xml|csv|yaml|html|markdown)\b\s*"
    r"|\b(?:json|xml|yaml)\s*[:=]",
    re.IGNORECASE,
)
_NO_REFERENCES = re.compile(
    r"\b(?:только\s+(?:цифр\w*|число|да|нет)\s*[,.]?\s*)?"
    r"(?:без|не\s+(?:указывай|приводи|давай|цитируй|упоминай))\s+"
    r"(?:(?:ссылок|цитат|источников|пунктов|номеров)\w*(?:\s+на\s+\w+)?)"
    r"(?:\s*(?:и|,)\s*(?:без\s+)?(?:ссылок|цитат|источников|пунктов|номеров)\w*)*\s*[:,]?\s*",
    re.IGNORECASE,
)
_ANSWER_VERB = re.compile(
    r"\b(?:просто\s+)?(?:ответь|ответьте|скажи|скажите|подтверди|подтвердите|согласись)\b\s*(?:,?\s*что\b|:)?\s*"
    r"(?:[«\"](?:да|нет)[»\"]\s*)?",
    re.IGNORECASE,
)
_MARKUP = re.compile(r"<script.*?</script>|<[^<>]{1,300}>|\{[^{}]{0,300}\}|\[\[.*?\]\]", re.IGNORECASE | re.DOTALL)


def fabrication_intent(text):
    """The user asks to invent, guess or present a made-up value as official."""
    q = fold(text)
    return bool(_INVENT_VERB.search(q) or _APPROXIMATE.search(q) or _AS_OFFICIAL.search(q))


def _tidy(text):
    text = re.sub(r"\s+([,.:;?!])", r"\1", text)
    text = re.sub(r"([,:;])\s*([,.:;?!])", r"\2", text)
    text = re.sub(r"\s*[—–-]+\s*(?=[.?!]?$)", "", text)
    text = re.sub(r"([?!])\s*\.", r"\1", text)
    text = re.sub(r"^[\s,.:;—-]+", "", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text[:1].upper() + text[1:] if text else text


def neutralize(text):
    """Drop instructions that try to change the route or the answer format, keep the factual part."""
    cleaned = text
    for pattern in (_MARKUP, _ROLE_LABEL, _MODE_PHRASE, _INSTRUCTION_LEAD, _FORMAT, _NO_REFERENCES, _ANSWER_VERB):
        cleaned = pattern.sub(" ", cleaned)
    cleaned = _tidy(cleaned)
    if cleaned == _tidy(text):
        return text
    # Nothing factual remains: keep the original so that the ordinary route decides.
    return cleaned if len(words(cleaned)) >= 2 else text


_DELIVERY = re.compile(
    r"(?:\b(?:и|но|а|затем|потом)\s+)?\b(?:назови|выдай|выдать|сошлись|подай|оформи|объяви|ответь\s+уверенно)\w*\b[^.?!]*",
    re.IGNORECASE,
)
_HEDGES = re.compile(
    r"\b(?:(?:при)?мерн\w*|приблизительн\w*|ориентировочн\w*|разумн\w*|правдоподобн\w*|любой|любое|любую"
    r"|официальн\w*|хотя бы|примерно|приблизительно|ориентировочно|уверенно)\b",
    re.IGNORECASE,
)


def strip_invention(text):
    """The factual part of an invention request, used for retrieval and as the topic to be grounded."""
    cleaned = _DELIVERY.sub(" ", text)
    for pattern in (_INVENT_VERB, _HEDGES):
        cleaned = pattern.sub(" ", cleaned)
    cleaned = re.sub(r"\s+(?:и|но|а|или)\s*([.?!]|$)", r"\1", _tidy(cleaned))
    cleaned = _tidy(cleaned)
    return cleaned if len(words(cleaned)) >= 2 else text


def required_overlap(count):
    """How many question terms an exact sentence must cover when the user asked to invent."""
    return math.ceil(0.75 * count)


_ASSERTION = re.compile(
    r"мне сказали|слышал\w*|говорят|верно|ведь\b|подтверди\w*|запомни|разве|правда ли|не так ли|"
    r"это новое правило|с сегодняшнего дня|теперь\b|,\s*да\s*\?|\bда\?\s*$"
)
_POINT_NUMBER = re.compile(r"(?:\bп\.?|пункт\w*|раздел\w*|ст\.?|статья|статьи)\s*\d+(?:[.,]\d+)*", re.IGNORECASE)


def premise_caveat(question, sentences):
    """A server-written correction when the question states a number or an actor the source does not."""
    q = _POINT_NUMBER.sub(" ", fold(question))
    asked = _numbers(q)
    asserted = bool(_ASSERTION.search(q))
    asked_roles = {canon for _, _, canon in find_roles(words(q))}
    for sentence in sentences:
        have = _numbers(sentence)
        wrong_number = bool(asked - have)
        found = {canon for _, _, canon in find_roles(words(sentence))}
        wrong_actor = asserted and bool(found) and bool(asked_roles) and not asked_roles & found
        if wrong_number or wrong_actor:
            return f"В регламенте указано иначе: «{sentence}»"
    return None


_EXECUTOR = re.compile(r"\bкто\b|\bкем\b|\bкому\b|\bкого\b|\bчь[яеиё]\w*|ответственн|исполнител|\bна ком\b")


def asks_executor(question):
    return bool(_EXECUTOR.search(fold(question)))
