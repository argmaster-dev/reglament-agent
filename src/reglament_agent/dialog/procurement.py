"""Locate how a named item is procured; buying software does not establish licence terms."""
import re
from dataclasses import dataclass

from .lexicon import contains_phrase, fold, word_stem, words


_BUY = re.compile(r"\b(?:купить|приобрести|закупить)\s+([^,;.!?]+?)(?=\s+(?:под проект|для проекта|как)\b|[,;.!?]|$)", re.I)
_HOW = re.compile(r"\bкак\b[^.;!?]{0,45}\b(?:оформ|закуп|купить|приобрест|размещ)\w*")
_SPECIAL = re.compile(r"\b(?:без|кроме|ли|если|можно|когда|сколько|право|разрешени\w*)\b")
_EXTERNAL_BUYER = re.compile(r"\b(?:клиент|заказчик|поставщик|подрядчик)(?:у|ам|и)?\s+"
                             r"(?:(?:надо|нужно|хочет|хотят|планиру\w*|собира\w*)\s+)?$")
_SOFTWARE = re.compile(r"\bПО\b|\b(?i:программн\w*\s+обеспечени\w*)\b")
_ASSETS = re.compile(r"\b(?P<operation>закупк\w*|приобретени\w*|покупк\w*|размещени\w*\s+заказ\w*\s+на)\s+"
                     r"(?P<items>[^,;.!?]+?)(?=\s+(?:осуществля|производ|провод|оформля|отвеча|выполня|через)\w*\b|[,;.!?]|$)", re.I)
_METHOD = re.compile(r"\b(?:осуществля|производ|провод|оформля|выполня)\w*\s+(?:через|по|посредством|путем|с привлечением)\b")


@dataclass(frozen=True)
class Procurement:
    item: str
    software: bool
    licence: bool
    value = "о порядке закупки"

    @property
    def search_query(self):
        return "закупка заказ " + self.item

    @property
    def caveat(self):
        if self.licence:
            return "Приведён порядок закупки ПО. Условия и объём прав по конкретной лицензии требуют отдельной проверки."
        return None

    @classmethod
    def requested(cls, question):
        normalized = fold(question)
        action = _BUY.search(question)
        if not action or not _HOW.search(normalized) or _SPECIAL.search(normalized):
            return None
        if _EXTERNAL_BUYER.search(normalized[:action.start()]):
            return None
        item = action[1].strip()
        licence = bool(re.match(r"лицензи\w*\s+на\s+", item, re.I))
        if licence:
            item = re.sub(r"^лицензи\w*\s+на\s+", "", item, flags=re.I)
        software = bool(_SOFTWARE.fullmatch(item))
        if not item or len(words(item)) > 4 or ("лиценз" in normalized and not (licence and software)):
            return None
        return cls(item, software, licence)

    def supported_by(self, anchor):
        for clause in re.split(r"[;.!?]", anchor.text):
            action = _ASSETS.search(clause)
            if not action or re.search(r"\b(?:не|кроме|исключая|вместо)\b", fold(clause)):
                continue
            assets = action["items"]
            if self.software:
                includes_item = bool(_SOFTWARE.search(assets))
            else:
                includes_item = contains_phrase(
                    [word_stem(w) for w in words(assets)], [word_stem(w) for w in words(self.item)]
                )
            if not includes_item:
                continue
            if _METHOD.match(fold(clause[action.end():]).lstrip()):
                return True
            if (action["operation"].lower().startswith("размещени")
                    and re.search(r"\bорганизу(?:ет|ют)\s*$", fold(clause[:action.start()]))):
                return True
        return False
