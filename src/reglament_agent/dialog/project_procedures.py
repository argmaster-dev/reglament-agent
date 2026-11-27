"""Check what a project procedure regulates before displaying a model-selected quote."""

import re
from enum import Enum

from .lexicon import fold
from .procurement import Procurement
from .reported_delay import ReportedDelay

_HOW = re.compile(r"\b(?:как|каким документом|в каком порядке|каков порядок)\b")
_EXCEPTION = re.compile(r"\b(?:без|кроме|если|не|ли|когда|сколько)\b")
_PARTY = re.compile(r"\b(?:(?:суб)?подрядчик\w*|(?:(?:суб)?подрядн|внешн)\w*\s+"
                    r"(?:организаци|компани|команд|персонал|исполнител|специалист)\w*)")
_STAFF = (r"(?:(?:команд|бригад)(?:а|ы|е|у|ой|ою|ам|ами|ах)?|персонал(?:а|у|ом|е)?|"
          r"(?:специалист|сотрудник|работник|инженер)(?:а|у|ом|е|ы|и|ов|ам|ами|ах)?)\b")
_MODIFIERS = r"(?:(?:еще|нов\w*|дополнительн\w*|необходим\w*|проектн\w*|согласованн\w*|\d+)\s+){0,3}"
_STAFF_REQUEST = re.compile(r"\b(?:запрос|запраш|выдел|предостав|сформир)\w*\s+" + _MODIFIERS + _STAFF)
_ALLOCATION = re.compile(r"\b(?:выделени\w*|предоставлени\w*|формировани\w*|"
                         r"(?:выдел|предостав)(?:яет|яют|ляет|ляют|ит|ят|ить)|сформир(?:овать|ует|уют))\s+"
                         + _MODIFIERS + _STAFF)
_REQUEST_FOR = re.compile(r"\b(?:запрос|запраш|заявк)\w*\s+(?:(?:у|через)\s+(?:[а-я-]+\s+){1,5}|на\s+)?$")
_ON_REQUEST = re.compile(r"\bпо\s+(?:заявке|запросу|обращению|поручению)\b")
_PROJECT = re.compile(r"\b(?:заказчик|клиент|договор|контракт|проект)\w*")
_FORM = re.compile(r"\b(?:оформ|согласо|закреп|зафиксир|документир)\w*")
_CHANGE = re.compile(r"\b(?:добав|расшир|сокра|исключ|измен|увелич|уменьш|включ)\w*\s+" + _MODIFIERS +
                     r"(?:(?:объем|содержани|функционал|модул|компонент|поставк|услови)\w*|"
                     r"(?:работ|услуг)(?:а|ы|и|е|у|ой|ами|ах)?)\b")
_CHANGE_SUBJECT = re.compile(r"\bизменени\w*\s+(?:(?:услови|предмет)\w*\s+(?:договор|контракт|проект)(?:а|ов)?\b|"
                             r"(?:объем|содержани|состав)\w*\s+(?:работ\w*|проект(?:а|ов)?|поставк\w*|услуг\w*)\b)")
_FORM_OF_CHANGE = re.compile(r"\b(?:оформл|оформит|фиксир|закрепля|закрепит|утвержда)\w*\s+"
                             r"(?:в\s+)?(?:(?:дополнительн|письменн|отдельн)\w*\s+)?"
                             r"(?:соглашени|заявк|протокол|акт|приказ|документ)\w*")
_CLAUSES = re.compile(r"[;!?]|\.\s+(?=[А-ЯЁ])|,\s*(?:а|но|затем)\s+")


def _change_rule(clause):
    subject = _CHANGE_SUBJECT.search(clause)
    instrument = _FORM_OF_CHANGE.search(clause)
    if not subject or not instrument or subject.end() > instrument.start():
        return False
    # A parenthesis may list dimensions of the change. Another noun or predicate in
    # this position can make the document apply to a different operation entirely.
    between = re.sub(r"\([^()]*\)", " ", clause[subject.end():instrument.start()])
    return not (set(re.findall(r"[а-я]+", between)) - {
        "должен", "должна", "должно", "должны", "быть", "необходимо", "следует", "обязательно", "не",
    })


def _staffing_rule(clause):
    allocation = _ALLOCATION.search(clause)
    if not allocation:
        return False
    # A request for the allocation itself, or an allocation explicitly made on request.
    return bool(_REQUEST_FOR.search(clause[:allocation.start()]) or _ON_REQUEST.search(clause))


class ProjectProcedure(Enum):
    CHANGE = "об изменении согласованного объёма работ"
    STAFF = "о запросе команды на проект"
    EXTERNAL_STAFF = "о запросе команды подрядчика"

    def supported_by(self, anchor):
        external_party = bool(_PARTY.search(fold(" ".join((anchor.text, anchor.context, anchor.section_title)))))
        if external_party != (self is ProjectProcedure.EXTERNAL_STAFF):
            return False
        for raw in _CLAUSES.split(anchor.text):
            clause = fold(raw)
            if self is ProjectProcedure.CHANGE:
                if _change_rule(clause):
                    return True
            elif _staffing_rule(clause):
                return True
        return False


def focused(question):
    return ReportedDelay.requested(question) or Procurement.requested(question)


def requested(question):
    if procedure := focused(question):
        return procedure
    q = fold(question)
    if not _HOW.search(q) or _EXCEPTION.search(q):
        return None
    if _STAFF_REQUEST.search(q):
        return ProjectProcedure.EXTERNAL_STAFF if _PARTY.search(q) else ProjectProcedure.STAFF
    if _FORM.search(q) and _PROJECT.search(q) and _CHANGE.search(q) and not _PARTY.search(q):
        return ProjectProcedure.CHANGE
    return None
