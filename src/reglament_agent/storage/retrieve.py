"""Candidate sources consumed by the dialog, before any answer is composed."""
from dataclasses import dataclass
from typing import Protocol

from psycopg import Connection

from reglament_agent.answer import SourceRef
from reglament_agent.dialog.lexicon import expand_query

from .search import search
from .sources import Hit


@dataclass(frozen=True)
class FAQHit:
    source: SourceRef  # identity and published revision supplied by the expert service
    answer: str
    basis: SourceRef | None  # None for a published expert answer outside the regulations
    department: str | None = None
    stage: str | None = None

    question: str = ""
    alt_questions: tuple[str, ...] = ()
    similarity: float = 0.0
    attribution: str = ""

    def __post_init__(self):
        if self.source.faq_id is None:
            raise ValueError("A published FAQ candidate requires its FAQ reference")


class PublishedFAQ(Protocol):
    def search_published(self, conn: Connection, question: str, vector: list[float], *,
                         department: str | None, stage: str | None, limit: int) -> list[FAQHit]:
        """Retrieve only published answers with current bases; omit private and pending replies."""
        ...


@dataclass(frozen=True)
class Candidates:
    points: list[Hit]
    faq: list[FAQHit]


def retrieve(conn: Connection, question: str, vector: list[float], *, department: str | None = None,
             stage: str | None = None, limit: int = 10,
             faq: PublishedFAQ | None = None) -> Candidates:
    points = search(conn, question, vector, department=department, stage=stage, limit=limit)
    if faq is None or not question.strip():
        return Candidates(points, [])
    published = faq.search_published(conn, expand_query(question), vector,
                                     department=department, stage=stage, limit=limit)
    scoped = [hit for hit in published if (department is None or hit.department in (None, department))
              and (stage is None or hit.stage in (None, stage))]
    return Candidates(points, scoped[:limit])
