"""Quantities and bounds whose wording matters even for otherwise very similar questions."""

import re

# A word tokenizer alone would drop comparison operators and split decimal values.
_TOKEN = re.compile(r"\d+(?:[.,]\d+)?|[^\W\d_]+|<=|>=|[<>≤≥=%₽$€+±−–-]")
_NUMBER_WORD = re.compile(
    r"нул\w*|ноль|один|одн\w*|два|две|дву\w*|двенадцат\w*|три|трех|трем|тремя|тринадцат\w*|"
    r"четыр\w*|пят\w*|шест\w*|семь\w*|семи\w*|восем\w*|восьм\w*|девят\w*|десят\w*|"
    r"одиннадцат\w*|двадцат\w*|тридцат\w*|сорок\w*|сто|ста|сотн\w*|"
    r"тысяч\w*|миллион\w*|миллиард\w*|перв\w*|втор\w*|трет\w*|четверт\w*"
)
_BOUND = frozenset(
    "до от после перед через сверх свыше более больше менее меньше выше ниже раньше ранее позже позднее "
    "минимум максимум включительно исключая < > <= >= ≤ ≥ =".split()
)
# These words/symbols mark bounds only next to a quantity (compare «отвечает за договор»).
_QUANTITY_PREFIX = frozenset("с со за на по в к + ± − – -".split())
_LIMIT_MODIFIER = re.compile(r"(?:минимальн|максимальн|наименьш|наибольш|верхн|нижн|превыш)\w*")
_QUALIFIER = re.compile(r"(?:рабоч|календарн|банковск|операционн|полн)\w*")
_TIME = re.compile(
    r"день|дн(?:и|я|ей|ем|е|ю|ям|ях|ями)?|суток|сутк\w*|час(?:а|ов|у|ы|е|ом|ам|ах|ами)?|"
    r"минут\w*|секунд\w*|недел\w*|месяц\w*|квартал\w*|год(?:а|у|ы|е|ом|ов|ам|ах|ами)?|лет"
)


def quantity_conditions(text: str) -> tuple[str, ...]:
    """Preserve the order of bounds, values and units, including units outside the time vocabulary."""
    tokens = _TOKEN.findall((text or "").casefold().replace("ё", "е"))
    protected = set()
    for index, token in enumerate(tokens):
        if token in _BOUND or _LIMIT_MODIFIER.fullmatch(token) or _TIME.fullmatch(token) or _QUALIFIER.fullmatch(token):
            protected.add(index)
        if token in {"до", "от", "после", "перед"} and index + 1 < len(tokens):
            protected.add(index + 1)  # the bound can be an event: before approval vs before payment
        if token[0].isdigit() or _NUMBER_WORD.fullmatch(token):
            protected.add(index)
            if index and tokens[index - 1] in _QUANTITY_PREFIX:
                protected.add(index - 1)
            # Never assume two unfamiliar units (e.g. copies and pages) are equivalent.
            if index + 1 < len(tokens):
                protected.add(index + 1)
                if _QUALIFIER.fullmatch(tokens[index + 1]) and index + 2 < len(tokens):
                    protected.add(index + 2)
    return tuple(tokens[i].replace(",", ".") for i in sorted(protected))
