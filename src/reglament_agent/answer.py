"""Answer contract between the dialog route (agent) and the channels (Chainlit chat, later Dion).

The route returns an Answer; a channel only renders it. All texts shown to the user are built
by the server from verified data — the channel never asks the model anything itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Awaitable, Callable, Protocol

from .ingest.model import Block


class Outcome(StrEnum):
    ANSWER = "answer"  # «Ответ»
    CLARIFY = "clarify"  # «Нужно уточнение»
    VARIANTS = "variants"  # «Варианты по подразделениям»
    LOW_CONFIDENCE = "low_confidence"  # «Требует подтверждения» (+ ticket)
    NO_BASIS = "no_basis"  # нет в регламентах (+ ticket)
    EXTERNAL_DOC = "external_doc"  # нужен регламент вне базы (+ ticket)
    CONFLICT = "conflict"  # положения расходятся (+ ticket)
    EXPERT_ANSWER = "expert_answer"  # «Ответ службы качества» (FAQ or reply to a ticket)
    OFF_TOPIC = "off_topic"  # «Не по регламентам»
    ERROR = "error"  # модель недоступна / таймаут — показать «Повторить»


class ScopeOrigin(StrEnum):
    QUESTION = "question"  # «из вопроса»
    PROFILE = "profile"  # «из профиля»
    CLARIFICATION = "clarification"  # «из уточнения»
    NONE = "none"


@dataclass(frozen=True)
class Scope:
    department: str | None
    stage: str | None  # "presale" | "project" | None
    origin: ScopeOrigin = ScopeOrigin.NONE


@dataclass(frozen=True)
class SourceRef:
    """Exact place of a basis: one sentence of a regulation point, or a FAQ record."""

    doc_id: str
    doc_title: str
    department: str
    version_no: int
    point: str  # "16.1"
    sentence: int | None = None  # index in ParsedPoint.sentences
    column: str | None = None  # table column header if the basis is a table cell
    faq_id: int | None = None  # set when the basis is an expert answer (FAQ)

    @property
    def label(self) -> str:
        """Short chip text, e.g. «ТД · п. 16.1»; for FAQ «Служба качества»."""
        if self.faq_id is not None:
            return "Служба качества"
        return f"{self.department} · п. {self.point}"


@dataclass(frozen=True)
class Statement:
    text: str  # one verified statement in plain words
    sources: list[SourceRef]


@dataclass(frozen=True)
class Variant:
    department: str
    statements: list[Statement]  # empty list = «в регламенте не указано»


@dataclass(frozen=True)
class Clarification:
    question: str  # «Срок зависит от подразделения. В каком вы работаете?»
    options: list[str]  # button labels; «Не знаю» is added by the channel
    attempt: int  # 1..3


@dataclass(frozen=True)
class TicketInfo:
    number: str  # «2026-0007»
    reason: str  # human text of the reason
    delivery: str  # "pending" | "sent" | "error"
    transferred: list[str]  # what was passed: «вопрос», «профиль (ТД)», «1 уточнение»


@dataclass
class Answer:
    outcome: Outcome
    summary: str = ""  # 1–3 sentence conclusion shown first
    statements: list[Statement] = field(default_factory=list)
    variants: list[Variant] = field(default_factory=list)
    clarification: Clarification | None = None
    ticket: TicketInfo | None = None
    scope: Scope = field(default_factory=lambda: Scope(None, None))
    caveat: str | None = None  # «Требует подтверждения службой качества»
    profile_note: str | None = None  # «Запомнил: Техническая дирекция»
    answer_id: str | None = None  # id in the request log (ratings, journal link)
    from_cache: bool = False


@dataclass
class UserContext:
    email: str
    role: str  # "employee" | "expert"
    dialog_id: str
    message_id: str | None = None  # stable id of the user message; a retry reuses it (no duplicate ticket)


StatusCallback = Callable[[str], Awaitable[None]]  # «Ищу в 4 регламентах…», «В очереди: 1»


class Answerer(Protocol):
    async def answer(self, user: UserContext, message: str, on_status: StatusCallback) -> Answer: ...

    async def choose(self, user: UserContext, option: str, on_status: StatusCallback) -> Answer:
        """User pressed a clarification button (or «Не знаю»)."""
        ...

    async def rate(self, user: UserContext, answer_id: str, good: bool, reason: str | None, forward: bool = False
    ) -> TicketInfo | None:
        """👍/👎; with forward=True (user pressed «Передать в службу качества») returns a ticket."""
        ...


@dataclass(frozen=True)
class Practice:
    """A published expert answer attached to a regulation point («практика применения»)."""

    faq_id: int
    question: str
    answer: str
    confirmed_at: str  # ISO date of the confirmation or publication


@dataclass(frozen=True)
class PointView:
    """What the source side panel shows."""

    doc_title: str
    department: str
    stages: list[str]
    version_no: int
    uploaded_at: str  # ISO date
    path: list[str]  # ["Раздел 16. Подготовка … договоров с субподрядчиками", "16.1"]
    point: str
    sentences: list[str]
    highlight: int | None  # sentence index to highlight
    column: str | None
    neighbours: list[tuple[str, str]]  # (number, first line) of adjacent subpoints/notes
    blocks: list[Block] = field(default_factory=list)  # original paragraph and table cells
    practice: list[Practice] = field(default_factory=list)  # published FAQ about this point, newest first


class Sources(Protocol):
    async def point(self, ref: SourceRef) -> PointView: ...


@dataclass(frozen=True)
class Profile:
    email: str
    department: str | None
    position: str | None
    confirmed_at: str | None


class Profiles(Protocol):
    async def get(self, email: str) -> Profile: ...

    async def reset(self, email: str) -> None: ...

    async def set_department(self, email: str, department: str) -> Profile: ...
