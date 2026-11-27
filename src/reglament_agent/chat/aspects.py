"""Блок «На что обратить внимание»: советы из каталога службы качества со ссылками на статьи законов.

Совет выбирается детерминированно по разделам процитированных пунктов и признакам в вопросе.
Модель советы не пишет, законы в поиск не попадают, интернет не нужен: тексты статей лежат в `laws.toml`.
Некорректные данные отключают функцию, но не чат.
"""

from __future__ import annotations

import logging
import os
import re
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Mapping

from reglament_agent.answer import Answer, Outcome

log = logging.getLogger(__name__)

ASPECT_LIMIT = 3
ADVICE_OUTCOMES = {Outcome.ANSWER, Outcome.VARIANTS, Outcome.LOW_CONFIDENCE}
_SECTION = re.compile(r"^[A-Za-z0-9_\-]+:[0-9]+$")


class CatalogError(ValueError):
    pass


@dataclass(frozen=True)
class Law:
    id: str
    act: str
    number: str
    article: str
    title: str
    text: str
    revision: str
    source_url: str
    retrieved: str
    short: str = ""

    @property
    def reference(self) -> str:
        """«Гражданский кодекс РФ, ст. 432»."""
        article = re.sub(r"^(статья|ст\.?)\s*", "", self.article.strip(), flags=re.I)
        name = self.short or self.act
        return f"{name}, ст. {article}" if article else name


@dataclass(frozen=True)
class Aspect:
    id: str
    rubric: str
    text: str
    when: tuple[str, ...]
    laws: tuple[str, ...]
    quote: str
    short: str = ""

    @property
    def chat_text(self) -> str:
        """Краткий совет для чата; полный `text` остаётся в каталоге."""
        return self.short or self.text


@dataclass(frozen=True)
class Rubric:
    id: str
    title: str
    sections: frozenset[str]
    aspects: tuple[Aspect, ...]


@dataclass(frozen=True)
class Catalog:
    rubrics: tuple[Rubric, ...]
    laws: Mapping[str, Law]


@dataclass(frozen=True)
class Advice:
    aspect: Aspect
    laws: tuple[Law, ...]


def _norm(text: str) -> str:
    return " ".join(text.split())


def quote_is_grounded(quote: str, texts: list[str]) -> bool:
    """Цитата может склеивать фрагменты через «…»; каждый фрагмент дословно встречается в одном из текстов."""
    fragments = [_norm(f).strip(" .") for f in quote.split("…")]
    fragments = [f for f in fragments if f]
    normed = [_norm(t) for t in texts]
    return bool(fragments) and all(any(f in t for t in normed) for f in fragments)


def _need(entry: dict, key: str, where: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        raise CatalogError(f"{where}: нет поля «{key}»")
    return value


def parse_catalog(aspects_toml: str, laws_toml: str) -> Catalog:
    try:
        raw_aspects, raw_laws = tomllib.loads(aspects_toml), tomllib.loads(laws_toml)
    except tomllib.TOMLDecodeError as exc:
        raise CatalogError(f"файл не читается: {exc}") from exc
    laws: dict[str, Law] = {}
    for entry in raw_laws.get("law", []):
        where = f"закон {entry.get('id')!r}"
        fields = {k: _need(entry, k, where) for k in ("id", "act", "title", "text", "revision", "source_url", "retrieved")}
        law = Law(**fields, number=str(entry.get("number", "")), article=str(entry.get("article", "")),
                  short=str(entry.get("short", "")))
        if law.id in laws:
            raise CatalogError(f"закон {law.id}: повторяется")
        laws[law.id] = law
    rubrics, seen = [], set()
    for r in raw_aspects.get("rubric", []):
        rid = _need(r, "id", "рубрика")
        sections = r.get("sections")
        if not isinstance(sections, list) or not sections:
            raise CatalogError(f"рубрика {rid}: нет списка sections")
        for s in sections:
            if not isinstance(s, str) or not _SECTION.match(s):
                raise CatalogError(f"рубрика {rid}: раздел {s!r} не вида «doc_id:N»")
        aspects = []
        for a in r.get("aspect", []):
            aid = _need(a, "id", f"рубрика {rid}")
            where = f"аспект {aid}"
            if aid in seen:
                raise CatalogError(f"{where}: повторяется")
            seen.add(aid)
            ids = a.get("laws")
            if not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids):
                raise CatalogError(f"{where}: нет списка laws")
            for i in ids:
                if i not in laws:
                    raise CatalogError(f"{where}: закон {i!r} не найден в laws.toml")
            quote = _need(a, "quote", where)
            if not quote_is_grounded(quote, [laws[i].text for i in ids]):
                raise CatalogError(f"{where}: цитаты нет в тексте указанных статей")
            when = a.get("when", [])
            if not isinstance(when, list) or not all(isinstance(w, str) and w for w in when):
                raise CatalogError(f"{where}: when должен быть списком строк")
            aspects.append(Aspect(aid, rid, _need(a, "text", where), tuple(w.lower() for w in when), tuple(ids), quote,
                                  str(a.get("short", "")).strip()))
        rubrics.append(Rubric(rid, str(r.get("title", rid)), frozenset(sections), tuple(aspects)))
    return Catalog(tuple(rubrics), laws)


def default_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "docx" / "aspects"


def load_catalog(directory: Path | None = None, env: Mapping[str, str] | None = None) -> Catalog | None:
    """None, если функция выключена, файлов нет или данные некорректны (причина в журнале)."""
    env = os.environ if env is None else env
    if env.get("ASPECTS_ENABLED", "1").strip().lower() in ("0", "false", "no", "off"):
        return None
    base = directory or Path(env.get("ASPECTS_DIR") or default_dir())
    a, l = base / "aspects.toml", base / "laws.toml"
    if not (a.is_file() and l.is_file()):
        return None
    try:
        return parse_catalog(a.read_text(encoding="utf-8"), l.read_text(encoding="utf-8"))
    except (CatalogError, OSError, UnicodeDecodeError) as exc:
        log.error("каталог «На что обратить внимание» отключён: %s", exc)
        return None


@lru_cache(maxsize=1)
def cached_catalog() -> Catalog | None:
    return load_catalog()


def section_key(doc_id: str, point: str) -> str:
    """«td-project», «7.3.1» -> «td-project:7»."""
    return f"{doc_id}:{point.split('.')[0].strip()}"


def cited_sections(answer: Answer) -> set[str]:
    statements = list(answer.statements) + [s for v in answer.variants for s in v.statements]
    return {
        section_key(r.doc_id, r.point)
        for st in statements
        for r in st.sources
        if r.faq_id is None and r.doc_id and r.point
    }


def select_advice(catalog: Catalog | None, answer: Answer, question: str, limit: int = ASPECT_LIMIT) -> list[Advice]:
    if catalog is None or answer.outcome not in ADVICE_OUTCOMES:
        return []
    sections = cited_sections(answer)
    if not sections:
        return []
    q = question.lower()
    hits, general, seen = [], [], set()
    for rubric in catalog.rubrics:
        if not rubric.sections & sections:
            continue
        for a in rubric.aspects:
            if a.id in seen:
                continue
            seen.add(a.id)
            if not a.when:
                general.append(a)
            elif any(w in q for w in a.when):
                hits.append(a)
    return [Advice(a, tuple(catalog.laws[i] for i in a.laws)) for a in (hits + general)[:limit]]
