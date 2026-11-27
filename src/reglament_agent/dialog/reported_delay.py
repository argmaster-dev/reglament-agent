"""Keep an overdue remedy conditional when the user only reports a delay."""
import re
from dataclasses import dataclass

from .lexicon import fold, word_stem, words


_PARTY = r"(?:клиент|заказчик|поставщик|подрядчик|исполнитель|сотрудник|партнер)"
_ASK = re.compile(r"\b(?:что (?:мне |нам )?делать|как (?:мне |нам )?(?:быть|поступить|действовать|реагировать))\b")
_REPORT = re.compile(
    rf"\b(?P<party>{_PARTY})\s+(?:затягива(?:ет|ют)|задержива(?:ет|ют)|медл(?:ит|ят) с)\s+"
    r"(?P<object>[^,;.!?]+?)(?=\s+(?:что|как|но)\b|[,;.!?]|$)"
)
_NOT_DUE = re.compile(
    r"\b(?:срок|дата)\b[^.,;!?]{0,35}?\bне\s+(?:наступ|ист[её]к|выш|прош)\w*"
    r"|\b(?:просрочки|задолженности)\s+(?:пока\s+|еще\s+)?нет\b"
)
_DELAY = r"(?:просрочк\w*|задержк\w*|нарушени\w*\s+(?:установленн\w*\s+)?срок\w*)"
_NOMINAL = re.compile(
    rf"\b(?:в случае|при)\s+{_DELAY}\s+(?P<object>[^,;.!?]+?)\s+"
    rf"(?:со стороны|по вине)\s+(?P<party>{_PARTY})\w*\b"
)
_VERBAL = re.compile(rf"\bесли\s+(?P<party>{_PARTY})\s+(?:просроч\w*|наруш\w*\s+срок\w*)\s+"
                     r"(?P<object>[^,;.!?]+)[,;]")
_PREPOSITIONS = {"за", "по", "на", "в", "к", "у", "от", "для"}
_ACTION = re.compile(
    r"\b(?:инициир|уведомля|направля|отправля|переда|напомина|согласу|регистрир|оформля|"
    r"приостанавлива|составля|сообща|запрашива|обраща)\w*(?:ет|ют|ется|ются)\b"
    r"|\b(?:следует|необходимо|должен|должна|должны)\s+[а-я]{3,}ть(?:ся)?\b"
)


def _subject(text):
    return [word_stem(w) for w in words(text) if w not in _PREPOSITIONS]


def _party(name):
    return "заказчик" if name == "клиент" else name


@dataclass(frozen=True)
class ReportedDelay:
    party: str
    subject: tuple[str, ...]
    object_text: str
    not_yet_due: bool

    value = "о действиях после нарушения срока"
    caveat = "Проверьте, наступил ли срок по обязательству, указанному в цитате. Именно это условие запускает процедуру."

    @property
    def search_query(self):
        return "просрочка " + self.object_text

    @classmethod
    def requested(cls, question):
        question = fold(question)
        match = _REPORT.search(question)
        ask = _ASK.search(question)
        if not match or not ask:
            return None
        not_yet_due = bool(_NOT_DUE.search(question))
        if ask.start() >= match.end():
            # A second event can trigger its own procedure, e.g. a threatened contract deadline.
            between = question[match.end():ask.start()].strip(" ,;?!.")
            if between and not not_yet_due:
                return None
            if question[ask.end():].strip(" ?,!.") not in {"", "дальше", "в этом случае"}:
                return None
        else:
            between = question[ask.end():match.start()].strip(" ,;?!.")
            after = question[match.end():].strip(" ,;?!.")
            if between not in {"", "если", "когда"} or (after and not not_yet_due):
                return None
        subject = _subject(match["object"])
        if not subject:
            return None
        return cls(_party(match["party"]), tuple(subject), match["object"], not_yet_due)

    def supported_by(self, anchor):
        if self.not_yet_due:
            return False
        sentence = fold(anchor.text)
        for pattern in (_NOMINAL, _VERBAL):
            for match in pattern.finditer(sentence):
                source = _subject(match["object"])
                if _party(match["party"]) != self.party or not source or source[0] != self.subject[0]:
                    continue
                consequence = re.split(r"[.!?;]", sentence[match.end():], maxsplit=1)[0]
                if set(self.subject) <= set(source) and _ACTION.search(consequence):
                    return True
        return False
