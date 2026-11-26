"""Stub services so the chat is fully demoable without the real dialog route (demo texts)."""

from __future__ import annotations

import asyncio
import itertools

from reglament_agent.answer import (
    Answer,
    Clarification,
    Outcome,
    PointView,
    Profile,
    Scope,
    ScopeOrigin,
    SourceRef,
    Statement,
    StatusCallback,
    TicketInfo,
    UserContext,
    Variant,
)

TD = "Техническая дирекция"
IB = "Дирекция ИБ"
DB = "ДБРЦТ"
PREVIEW_DEPARTMENTS = (DB, IB, TD)

REF_TD_1_5 = SourceRef("stub-td", "Регламент пресейла", "ТД", 3, "1.5", sentence=1)
REF_TD_16_1 = SourceRef("stub-td-sub", "Регламент работы с субподрядчиками", "ТД", 2, "16.1", sentence=0)
REF_TD_1_7 = SourceRef("stub-td", "Регламент пресейла", "ТД", 3, "1.7", sentence=0)
REF_IB_4_2 = SourceRef("stub-ib", "Регламент Дирекции ИБ", "Дирекция ИБ", 1, "4.2", sentence=0, column="Документы")
REF_FAQ = SourceRef("faq", "FAQ службы качества", "Служба качества", 1, "", faq_id=1)

_POINTS = {
    ("stub-td", "1.5"): PointView(
        "Регламент пресейла", "ТД", ["presale"], 3, "2026-09-01", ["Регламент пресейла", "1.5"], "1.5",
        ["Заявка на пресейл оформляется в карточке проекта.", "Её подают за 2 недели до подачи конкурса."], 1, None,
        [("1.4", "Для чего нужна заявка"), ("1.6", "Кто согласует заявку")],
    ),
    ("stub-td-sub", "16.1"): PointView(
        "Регламент работы с субподрядчиками", "ТД", ["project"], 2, "2026-08-12", ["Раздел 16. Договоры с субподрядчиками", "16.1"], "16.1",
        ["Заявку создаёт РП, за подготовку, согласование и подписание договора отвечает Логист."], 0, None,
        [("16.2", "Сроки согласования договора")],
    ),
    ("stub-td", "1.7"): PointView(
        "Регламент пресейла", "ТД", ["presale"], 3, "2026-09-01", ["Регламент пресейла", "1.7"], "1.7",
        ["Число одновременных заявок на одного пресейл-архитектора ограничено (демо-текст)."], 0, None, [],
    ),
    ("stub-ib", "4.2"): PointView(
        "Регламент Дирекции ИБ", "Дирекция ИБ", ["project"], 1, "2026-07-20", ["Регламент Дирекции ИБ", "4.2"], "4.2",
        ["К карточке проекта прикладывают перечень документов по таблице."], 0, "Документы", [("4.3", "Сроки")],
    ),
}


class StubSources:
    async def point(self, ref: SourceRef) -> PointView:
        pv = _POINTS.get((ref.doc_id, ref.point))
        if pv:
            return pv
        return PointView(ref.doc_title, ref.department, [], ref.version_no, "2026-01-01", [ref.doc_title, ref.point],
                         ref.point, ["Текст пункта (демо)."], ref.sentence, ref.column, [])


class StubProfiles:
    def __init__(self) -> None:
        self._data: dict[str, Profile] = {}

    async def get(self, email: str) -> Profile:
        return self._data.get(email) or Profile(email, None, None, None)

    async def reset(self, email: str) -> None:
        self._data.pop(email, None)

    async def set_department(self, email: str, department: str) -> Profile:
        p = Profile(email, department, (self._data.get(email) or Profile(email, None, None, None)).position, "2026-11-26")
        self._data[email] = p
        return p


class StubAnswerer:
    """Canned answers by keyword: пресейл, субподряд, такси, документы, конфликт, эксперт, плов, ошибка."""

    def __init__(self, profiles: StubProfiles | None = None) -> None:
        self.profiles = profiles or StubProfiles()
        self._pending: dict[str, str] = {}
        self._tickets: dict[str, TicketInfo] = {}  # by message id: a retry never duplicates a ticket
        self._seq = itertools.count(1)
        self._fail_once: set[str] = set()

    def _ticket(self, key: str, reason: str, transferred: list[str]) -> TicketInfo:
        if key not in self._tickets:
            self._tickets[key] = TicketInfo(f"2026-{next(self._seq):04d}", reason, "sent", transferred)
        return self._tickets[key]

    async def answer(self, user: UserContext, message: str, on_status: StatusCallback) -> Answer:
        await on_status("В очереди: 1")
        await asyncio.sleep(0.3)
        await on_status("Ищу в 4 регламентах…")
        await asyncio.sleep(0.5)
        await on_status("Проверяю основания…")
        await asyncio.sleep(0.3)
        text = message.lower()
        key = user.message_id or f"{user.dialog_id}:{message}"
        aid = f"stub-{key}"
        if "ошибка" in text and key not in self._fail_once:
            self._fail_once.add(key)  # fails once; «Повторить» with the same message id succeeds
            return Answer(Outcome.ERROR)
        if "плов" in text:
            return Answer(Outcome.OFF_TOPIC, summary="Я отвечаю только на вопросы по регламентам компании.")
        if "субподряд" in text:
            return Answer(
                Outcome.ANSWER, "Заявку на договор с субподрядчиком создаёт руководитель проекта.",
                [Statement("Заявку создаёт РП, за подготовку, согласование и подписание договора отвечает Логист.", [REF_TD_16_1])],
                scope=Scope(TD, "project", ScopeOrigin.QUESTION), answer_id=aid,
            )
        if "сколько заявок" in text:
            return Answer(
                Outcome.VARIANTS, "Ответ зависит от подразделения.", answer_id=aid,
                variants=[
                    Variant(TD, [Statement("Число заявок на одного пресейл-архитектора ограничено (демо-текст).", [REF_TD_1_7])]),
                    Variant(IB, []),
                    Variant(DB, []),
                ],
            )
        if "пресейл" in text:
            prof = await self.profiles.get(user.email)
            if prof.department == TD:
                return self._presale_answer(aid, ScopeOrigin.PROFILE)
            self._pending[user.dialog_id] = aid
            return Answer(
                Outcome.CLARIFY,
                clarification=Clarification("Срок зависит от подразделения. В каком вы работаете?", [DB, IB, TD], 1),
                answer_id=aid,
            )
        if "такси" in text:
            t = self._ticket(key, "В регламентах нет положения о компенсации такси.", ["вопрос"])
            return Answer(Outcome.NO_BASIS, "В регламентах я не нашёл ответа на этот вопрос.", ticket=t, answer_id=aid)
        if "документ" in text or "карточк" in text:
            t = self._ticket(key, "Ответ найден, но требует подтверждения.", ["вопрос", "профиль"])
            return Answer(
                Outcome.LOW_CONFIDENCE, "Похоже, нужен перечень документов по таблице п. 4.2.",
                [Statement("К карточке проекта прикладывают документы из перечня.", [REF_IB_4_2])],
                scope=Scope(IB, "project", ScopeOrigin.QUESTION), ticket=t, answer_id=aid,
                caveat="Требует подтверждения службой качества",
            )
        if "конфликт" in text:
            t = self._ticket(key, "Два регламента отвечают по-разному.", ["вопрос"])
            return Answer(Outcome.CONFLICT, "Положения расходятся, вопрос передан эксперту.", ticket=t, answer_id=aid)
        if "эксперт" in text:
            return Answer(
                Outcome.EXPERT_ANSWER, "Служба качества подтвердила: заявку можно подать повторно.",
                [Statement("Повторная подача допускается.", [REF_FAQ])], answer_id=aid,
            )
        t = self._ticket(key, "Ответ в регламентах не найден.", ["вопрос"])
        return Answer(Outcome.NO_BASIS, "В регламентах я не нашёл ответа на этот вопрос.", ticket=t, answer_id=aid)

    def _presale_answer(self, aid: str, origin: ScopeOrigin, note: str | None = None) -> Answer:
        return Answer(
            Outcome.ANSWER, "Заявку на пресейл подают заранее, за 2 недели до подачи конкурса.",
            [Statement("За 2 недели до подачи конкурса.", [REF_TD_1_5])],
            scope=Scope(TD, "presale", origin), profile_note=note, answer_id=aid,
        )

    async def choose(self, user: UserContext, option: str, on_status: StatusCallback) -> Answer:
        await on_status("Ищу в 4 регламентах…")
        aid = self._pending.pop(user.dialog_id, "stub-choose")
        if option == "Не знаю":
            t = self._ticket(aid, "Не удалось определить подразделение.", ["вопрос", "1 уточнение"])
            return Answer(Outcome.NO_BASIS, "Без подразделения я не смогу ответить точно.", ticket=t, answer_id=aid)
        if option != TD:
            return Answer(Outcome.NO_BASIS, ticket=self._ticket(aid, f"Для «{option}» в регламентах нет срока.", ["вопрос", "1 уточнение"]),
                          summary="Для вашего подразделения срок в регламентах не указан.", answer_id=aid)
        await self.profiles.set_department(user.email, option)
        return self._presale_answer(aid, ScopeOrigin.CLARIFICATION, f"Запомнил: {option}")

    async def rate(self, user: UserContext, answer_id: str, good: bool, reason: str | None, forward: bool = False) -> TicketInfo | None:
        if good or not forward:
            return None
        return self._ticket(f"rate:{answer_id}", f"Пользователь счёл ответ неверным: {reason or 'без причины'}.", ["вопрос", "ответ"])
