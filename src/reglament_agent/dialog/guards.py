"""Rules about what the user's text may do: invent content, steer the route, assert a false premise.

The patterns describe request styles and word families, never the content of a regulation.
"""

import math
import re

from .checks import _numbers
from .lexicon import find_roles, fold, glossary, words

MIN_LENGTH, MAX_LENGTH = 3, 2000

OFF_TOPIC_MESSAGE = "Этот вопрос не касается регламентов компании — обратитесь к своему руководителю."

# Ask for a policy even when the subject is money, a particular deal or correspondence.
_PROCEDURE = re.compile(
    r"\b(?:регламент|положени|инструкци|правил|порядок|порядк|методик|формул|услови|обязан|положен|предусмотрен|ограничени)\w*"
    r"|\b(?:как|кто|где|когда|можно ли|нужно ли|имею ли право|каким образом)\b"
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
    r"\b(?:follow[\s-]?up|фоллоу[\s-]?ап|письм\w*|напоминани\w*|сообщени\w*|рассылк\w*)\b"
)

# Classify the intent (choice for a private recipient or purpose), without a catalog of products.
_CHOICE = re.compile(
    r"\b(?:подобрать|подбери(?:те)?|выбрать|выбери(?:те)?|посоветуй(?:те)?|порекомендуй(?:те)?)\b"
)
_PRIVATE_PURPOSE = re.compile(
    r"\b(?:для себя(?: лично)?|для личного пользования)\b"
    r"|\b(?:для|на|в|к)\s+(?:(?:семейн|личн|домашн)\w*\s+\w+|(?:отдых|поход|пикник|рыбалк)\w*)\b"
    r"|\b(?:для(?:\s+(?:сво\w*|домашн\w*|взросл\w*))?|мо(?:ему|ей)|сво(?:ему|ей))\s+"
    r"(?:семь\w*|ребенк\w*|дет(?:ей|ям)|питомц\w*|кот(?:а|у|ят|енк[ау])|кошк\w*|собак\w*|щенк\w*)\b"
)
_WORK_RULE = re.compile(
    r"\b(?:регламент|инструкци|положени|поряд|процедур|смет|закуп|договор|документ|заказчик|сделк"
    r"|компани|подразделени|служебн|предприяти|финансир|согласовани|сотрудник|клиент)\w*\b"
    r"|\bправил(?:о|а|у|ом|е|ам|ами|ах)?\b|\bпроект(?:а|у|е|ом|ы|ов|ам|ами|ах|н\w*)?\b"
)
# These are work subjects even when the reason for arranging them is personal.
_EMPLOYMENT_SUBJECT = re.compile(r"\b(?:отпуск|отгул|больничн|командировк|дежурств|график)\w*\b")
_ADVICE_FILLERS = {"мне", "помоги", "помогите", "пожалуйста", "пожалуй",
                  "прошу", "как", "что", "это", "бы", "ты", "вы", "что-нибудь", "нибудь", "который", "нужно"}
# A cooking or household action needs a personal setting. Preparing a work deliverable
# remotely is legitimate, as is asking how the employer purchases or serves food.
_HOME_SETTING = re.compile(r"\b(?:дома|в домашних условиях|на (?:своей|домашней) кухне)\b")
_KITCHEN_SETTING = re.compile(r"\b(?:завтрак|обед|ужин)\w*\b|\b(?:в духовке|на сковороде|на пару)\b")
_HOUSEHOLD_HOW = re.compile(
    r"\bкак\s+(?:(?:дома|можно|лучше|правильно|самому|самой|быстро)\s+){0,3}"
    r"(?:приготовить|готовить|сварить|испечь|запечь|пожарить|замесить|постирать|погладить|пропылесосить)\b"
)
_RECIPE_ADVICE = re.compile(
    r"\b(?:посоветуй(?:те)?|порекомендуй(?:те)?|подскажи(?:те)?|дай(?:те)?|предложи(?:те)?|"
    r"напиши(?:те)?|покажи(?:те)?|како[йе])\s+(?:(?:мне|нам|простой|подходящий|новый)\s+){0,2}рецепт(?:ы)?\b"
)
_WORK_PREPARATION = re.compile(
    r"\b(?:отчет|заявк|презентаци|реестр|бланк|файл|кейтеринг|корпоративн|рабоч|офисн)\w*\b"
    r"|\b(?:коммерческ\w*\s+предложени\w*|из дома по работ\w*|поставщик\w*)\b"
)
_REFERENCE_WORDS = {"его", "ее", "это", "этот", "эту", "этого", "эти", "их", "него", "нее", "такой", "такую",
                    "этим", "этом", "все", "данный", "дома", "для", "нас", "нам", "себя", "потом", "еще", "сейчас"}
_FX_APPLICATION = re.compile(
    r"\b(?:закладывать|фиксировать|подставлять|указать|указывать|применить|применять|использовать|учитывать)\b"
    r"|\b(?:для|при)\s+(?:(?:предварительн|проектн)\w*\s+)?(?:расчет|оформлени|составлени|заполнени)\w*"
    r"\s+(?:смет|бюджет|стоимост|цен|предложени|договор|счет)\w*\b"
)


def _personal_choice(q):
    choice = _CHOICE.search(q)
    if not choice or _WORK_RULE.search(q):
        return False
    purpose = _PRIVATE_PURPOSE.search(q)
    if not purpose or find_roles(words(q)):
        return False
    # «Помоги выбрать для себя» still needs clarification. Require a named subject of the choice.
    subject = _CHOICE.sub(" ", q[:purpose.start()] + " " + q[purpose.end():])
    return not _EMPLOYMENT_SUBJECT.search(subject) and any(
        len(w) > 2 and w not in _ADVICE_FILLERS for w in words(subject)
    )


def _household_instruction(q):
    advice = _RECIPE_ADVICE.search(q)
    how = _HOUSEHOLD_HOW.search(q)
    if not advice and not (how and (_HOME_SETTING.search(q) or _KITCHEN_SETTING.search(q))):
        return False
    if _WORK_RULE.search(q) or _WORK_PREPARATION.search(q) or _EMPLOYMENT_SUBJECT.search(q):
        return False
    tokens = words(q)
    # «по» can be an ordinary preposition. Other glossary abbreviations identify work subjects such as КП.
    if find_roles(tokens) or any(token != "по" and token in tokens for token in map(fold, glossary())):
        return False
    # A pronoun alone can continue a real work question. Require an independently named subject.
    subject = _HOME_SETTING.sub(" ", q[(advice or how).end():])
    return any(len(w) > 2 and w not in _ADVICE_FILLERS and w not in _REFERENCE_WORDS for w in words(subject))


def operational_request(text):
    """Requests for actual records, market data or external work have no regulatory answer."""
    q = fold(text)
    if _personal_choice(q) or _household_instruction(q):
        return True
    if _PROCEDURE.search(q):
        return False
    if _EXCHANGE.search(q):
        return not _FX_APPLICATION.search(q)
    return bool(_TASK.search(q) or (_METRIC.search(q) and _RECORD.search(q)))


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
