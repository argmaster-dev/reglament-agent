"""Evidence for employee entitlements in a question that also invokes the law."""
import re
from dataclasses import dataclass

from . import guards
from .lexicon import fold

# The words naming a benefit are distinct from the operation an employee asks about.
_LEAVE = re.compile(r"\b(?:отпуск(?:а|е|у|ом|ов|ам|ах|ами|и)?|отпускн\w*)\b")
_TRIP = re.compile(r'\bкомандировк\w*\b')
_SUBJECTS = (
    _LEAVE,
    re.compile(r'\b(?:сверхуроч\w*|переработк\w*)\b'),
    _TRIP,
    re.compile(r'\b(?:выходн\w*|праздничн\w*|нерабочи\w*)\b'),
    re.compile(r'\b(?:больничн\w*|нетрудоспособност\w*)\b'),
)
_PAYMENT = re.compile(r'\b(?:оплат\w*|оплач\w*|платят|выплат\w*|доплат\w*)\b')
_PAYMENT_RULE = re.compile(
    r'\b(?:оплачива(?:ется|ются|ют|ет|ть)|выплачива(?:ется|ются|ют|ет|ть)|'
    r'начисля(?:ется|ются|ют|ет)|доплачива(?:ется|ют|ет)|(?:долж\w*|следует|необходимо)\s+оплатить)\b'
    r'|\b(?:оплата|доплата|выплата)\b[^.;!?]{0,65}\b(?:производится|положена|рассчитывается|составляет|по табелю)\b'
)
# Match the thing being paid for next to the predicate. A time modifier such as
# "during overtime" can equally apply to paying an invoice or reimbursing a hotel.
_WORK_TIME = (
    r'(?:сверхурочн(?:ая работа|ые часы|ые|ой труд)|переработк(?:а|и)|'
    r'работа|труд|отработанн(?:ое время|ые часы)|выходн(?:ой|ые)|праздничн(?:ый день|ые дни)|'
    r'отпуск(?:ные)?|больничный|пособие)'
)
_WORK_QUALIFIERS = (
    r'(?:сотрудник(?:а|у|ов|ам)?|работник(?:а|у|ов|ам)?|в|на|во|и|за|дни|день|'
    r'выходн\w*|праздничн\w*|нерабоч\w*|командировк\w*|проекте|также|отдельно|не)'
)
_WORK_PAY = re.compile(
    rf'\b{_WORK_TIME}\b(?:\s+{_WORK_QUALIFIERS}){{0,8}}\s+(?:{_PAYMENT_RULE.pattern})'
    r'|\b(?:оплата|доплата|выплата)\s+(?:за\s+)?'
    r'(?:сверхурочн\w*|переработк\w*|работ[ыу]\s+в\s+(?:выходн|праздничн)\w*|'
    r'отпускн\w*|больничн\w*)\b'
    r'|\b(?:работодатель|организация|компания)\s+оплачивает\s+'
    r'(?:сверхурочн\w*|переработк\w*|работу\s+в\s+(?:выходн|праздничн)\w*)\b'
    r'|\b(?:платят|доплачивают|выплачивают|положена\s+доплата)\s+'
    r'(?:сотрудник(?:у|ам)|работник(?:у|ам))\s+за\s+'
    r'(?:сверхурочн\w*|переработк\w*|работу\s+в\s+(?:выходн|праздничн)\w*)\b'
)
_TRANSFER = re.compile(r'\b(?:перенос\w*|перенес\w*|перенош\w*)\b')
_DURATION = re.compile(r'\b(?:продолжительност\w*|длительност\w*|сколько\s+(?:календарных\s+|рабочих\s+)?дней)\b')
_EXPENSE = re.compile(r'\b(?:расход|затрат|проживани|проезд|гостиниц|билет|питани|материал|оборудовани|услуг)\w*')
_LEAVE_NAME = r'отпуск(?:е|а|у|ом|и|ов|ами|ах)?\b'
_LEAVE_ADJ = r'(?:(?:ежегодн|оплачиваем|очередн|дополнительн|основн|неиспользованн)\w*\s+){0,3}'
_TRANSFER_ACT = r'перен(?:ос|ес|ош)\w*'
_LEAVE_TRANSFER = re.compile(
    rf'\b{_TRANSFER_ACT}\s+{_LEAVE_ADJ}{_LEAVE_NAME}|\b{_LEAVE_NAME}\s+'
    rf'(?:(?:по заявлению|с согласия) (?:сотрудника|работника|руководителя)\s+)?'
    rf'(?:(?:можно|допускается|разрешено|может|быть|не|нельзя|должен)\s+)*{_TRANSFER_ACT}'
)
_LEAVE_DURATION = re.compile(
    rf'\b(?:продолжительность|длительность|срок)\s+{_LEAVE_ADJ}{_LEAVE_NAME}|\b{_LEAVE_NAME}\s+'
    r'(?:(?:работнику|сотруднику|ежегодно)\s+)?(?:длится|составляет|предоставляется\s+на)\b'
)
_DURATION_RULE = re.compile(r'\b(?:продолжительност\w*|длительност\w*|длит(?:ся|ься)|составля\w*|предоставля\w*\s+на)\b')
_ACTIONS = ((_PAYMENT, _PAYMENT_RULE), (_TRANSFER, _TRANSFER), (_DURATION, _DURATION_RULE))
_ANAPHOR = re.compile(r'\b(?:его|ее|этого|этой|такого|такой)\b')
_CLAUSE = re.compile(r'[.;!?]|,\s+(?:а|но|затем)\s+')


@dataclass(frozen=True)
class Entitlement:
    subjects: tuple[re.Pattern, ...]
    operations: tuple[re.Pattern, ...]

    def supported_by(self, text):
        for clause in _CLAUSE.split(fold(text)):
            if not all(term.search(clause) for term in (*self.subjects, *self.operations)):
                continue
            # Paying travel expenditure establishes no entitlement to pay for time worked.
            if _PAYMENT_RULE in self.operations:
                # A trip can be the setting for work on a holiday. The entitlement
                # specified by the employee must still be the one that is paid.
                entitlements = tuple(term for term in self.subjects if term is not _TRIP)
                if not any(all(term.search(m.group()) for term in entitlements)
                           for m in _WORK_PAY.finditer(clause)):
                    continue
            if _TRANSFER in self.operations and not _LEAVE_TRANSFER.search(clause):
                continue
            if _DURATION_RULE in self.operations and not _LEAVE_DURATION.search(clause):
                continue
            return True
        return False


def requested_parts(question, parts):
    """A pronoun can carry a benefit across coordinated questions, such as a leave and its duration."""
    if not guards.asks_about_law(fold(question)):
        return {}
    result = {}
    previous = ()
    for part in parts or [question]:
        text = fold(part)
        subjects = tuple(term for term in _SUBJECTS if term.search(text))
        if not subjects and _ANAPHOR.search(text):
            subjects = previous
        operations = tuple(evidence for query, evidence in _ACTIONS if query.search(text))
        if _LEAVE not in subjects:
            operations = tuple(op for op in operations if op not in (_TRANSFER, _DURATION_RULE))
        if _PAYMENT_RULE in operations and _EXPENSE.search(text):
            # Expense reimbursement is checked independently of compensation for working time.
            operations = tuple(op for op in operations if op != _PAYMENT_RULE)
        if subjects and operations:
            result[part] = Entitlement(subjects, operations)
        previous = subjects
    return result
