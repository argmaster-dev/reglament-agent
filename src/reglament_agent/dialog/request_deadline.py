"""Bind a request's lead time to the action and reference event in the same rule."""
import re
from dataclasses import dataclass

from .lexicon import fold, word_key, words

_CREATE = r"(?:завести|заводить|создать|создавать|оформить|оформлять)"
_ASK = re.compile(
    rf"\b{_CREATE}\s+(?:нов(?:ую|ый)\s+)?(?:заявку|запрос)\s+на\s+"
    r"(.+?)(?=\s+(?:до|перед)\b|[,.!?;]|$)"
)
_PARTICIPATE = re.compile(r"\b(?:идем|иду|участвуем|участвую|готовимся|готовлюсь)\s+(?:на|в|к)\s+([^.,;!?]+)")
_REFERENCE = re.compile(rf"\b(?:до|перед)\s+(.+?)(?=\s+(?:надо|нужно|следует|необходимо|{_CREATE})\b|[,;.!?]|$)")
_REQUEST = r"(?:заявк\w*|запрос\w*)"
_PASSIVE = (r"(?:инициир(?:ован[аоы]?|уется|уются)|созда(?:н[аоы]?|ется|ются)"
            r"|завод(?:ится|ятся)|оформ(?:лен[аоы]?|ляется|ляются))")
_ACTIVE = r"(?:инициир(?:ует|уют)|созда(?:ет|ют)|завод(?:ит|ят)|оформля(?:ет|ют))"
_OPEN = re.compile(
    rf"\b(?P<before>\w+)(?:\s+на\s+(?P<purpose>[\w-]+(?:\s+[\w-]+){{0,3}}?))?"
    rf"(?:\s+(?:долж\w+|следует|необходимо|быть)){{0,3}}\s+{_PASSIVE}\b"
)
_ACTIVE_OPEN = re.compile(rf"\b{_ACTIVE}\s+(?P<after>\w+)(?:\s+на\s+(?P<purpose>[\w-]+(?:\s+[\w-]+){{0,3}}))?")
_REQUEST_OPEN = re.compile(rf"\b{_ACTIVE}\s+{_REQUEST}\b|\b{_REQUEST}\s+(?:долж\w*\s+быть\s+)?{_PASSIVE}\b")
_NAMED_REQUEST = re.compile(rf"\b{_REQUEST}\s+на\s+(\w+)")
# A generic “the request” can refer to the named deliverable when the same document
# explicitly places that deliverable in the request. A shared noun in another point is insufficient.
_DELIVERED = re.compile(rf"\b(\w+)\s+(?:выдается|выдают|вкладывается|вкладывают|загружается|загружают)\s+в\s+{_REQUEST}\b")
_ADVANCE = re.compile(r"\bза\b[^,;.!?]{1,70}?\bдо\s+([^,;.!?]+)")


def created_request(text):
    text = fold(text)
    return any(not re.search(r"\bне\s*$", text[:match.start()]) for match in _REQUEST_OPEN.finditer(text))


def request_purposes(text):
    return {word_key(name) for name in _NAMED_REQUEST.findall(fold(text))}


def request_deliverables(text):
    return {word_key(name) for name in _DELIVERED.findall(fold(text))}


@dataclass(frozen=True)
class RequestLeadTime:
    purpose: tuple[str, ...]
    event: frozenset[str]

    @classmethod
    def from_question(cls, question):
        question = fold(question)
        asked = _ASK.search(question)
        if not asked:
            return None
        event = _PARTICIPATE.search(question[:asked.start()]) or _REFERENCE.search(question)
        return cls(tuple(word_key(word) for word in words(asked[1])),
                   frozenset(word_key(word) for word in words(event[1])) if event else frozenset())

    def basis(self, sentence, *, created, purposes, delivered):
        # Cut off the target event before locating the verb and its subject/object.
        # Opening an application in a background clause does not time the application.
        sentence = fold(sentence)
        limit = _ADVANCE.search(sentence)
        if not limit or not self.event <= {word_key(word) for word in words(limit[1])}:
            return None
        timed = re.split(r"[,;.!?]", sentence[:limit.start()])[-1]
        timed = re.sub(r"\s+не (?:позднее|менее|раньше) чем\s*$", "", timed)
        for pattern, name in ((_ACTIVE_OPEN, "after"), (_OPEN, "before")):
            for bound in pattern.finditer(timed):
                if re.search(r"\bне\s*$", timed[:bound.start()]):
                    continue
                entity = bound[name]
                qualifier = bound["purpose"]
                if re.fullmatch(_REQUEST, entity):
                    if qualifier:
                        if self.purpose == tuple(word_key(word) for word in words(qualifier)):
                            return "direct"
                    elif set(self.purpose) == purposes and len(self.purpose) == 1:
                        return "direct"
                    elif not purposes and len(self.purpose) == 1 and delivered == {self.purpose[0]}:
                        return "generic"
                elif self.purpose == (word_key(entity),) and created:
                    return "direct"
        return None
