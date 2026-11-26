"""Pure rendering helpers: Answer -> markdown strings and action specs. No Chainlit imports."""

from __future__ import annotations

from dataclasses import dataclass

from reglament_agent.answer import (
    Answer,
    Outcome,
    PointView,
    Profile,
    Scope,
    ScopeOrigin,
    SourceRef,
    Statement,
    TicketInfo,
    Variant,
)

OUTCOME_LABELS: dict[Outcome, str] = {
    Outcome.ANSWER: "✅ Ответ",
    Outcome.CLARIFY: "❓ Нужно уточнение",
    Outcome.VARIANTS: "🗂 Варианты по подразделениям",
    Outcome.LOW_CONFIDENCE: "⚠️ Требует подтверждения",
    Outcome.EXPERT_ANSWER: "🎓 Ответ службы качества",
    Outcome.OFF_TOPIC: "🚫 Не по регламентам",
    Outcome.ERROR: "⛔ Ошибка",
}
TICKET_OUTCOMES = {Outcome.NO_BASIS, Outcome.EXTERNAL_DOC, Outcome.CONFLICT}
ORIGIN_TEXT = {
    ScopeOrigin.PROFILE: "из профиля",
    ScopeOrigin.QUESTION: "из вопроса",
    ScopeOrigin.CLARIFICATION: "из уточнения",
}
DELIVERY_TEXT = {"sent": "письмо отправлено", "pending": "письмо ожидает отправки", "error": "ошибка отправки письма"}
DONT_KNOW = "Не знаю"
EMPTY_VARIANT = "в регламенте не указано"
DOWN_REASONS = ["Неверно", "Неполно", "Не то подразделение", "Другое"]
EXAMPLES = [
    "Сколько заявок может быть у пресейл-архитектора?",
    "За сколько подавать заявку на пресейл?",
    "Компенсируют ли такси в региональный выходной?",
    "Какие документы обязательно приложить к карточке проекта в Дирекции ИБ?",
]
WELCOME = (
    "Здравствуйте! Я помогаю найти ответ в регламентах компании: отвечаю коротко и даю ссылки на пункты.\n\n"
    "Если ответ зависит от подразделения, я уточню. Если в регламентах ответа нет, "
    "передам вопрос в службу качества, а ответ придёт в этот диалог.\n\n"
    "Напишите вопрос или выберите пример:"
)


@dataclass(frozen=True)
class ActionSpec:
    name: str
    label: str
    payload: dict


def outcome_label(answer: Answer) -> str:
    if answer.outcome in TICKET_OUTCOMES:
        number = f" №{answer.ticket.number}" if answer.ticket else ""
        return f"✉️ Обращение{number}"
    label = OUTCOME_LABELS[answer.outcome]
    if answer.outcome is Outcome.LOW_CONFIDENCE and answer.ticket:
        label += f" · обращение №{answer.ticket.number}"
    return label


def scope_line(scope: Scope) -> str | None:
    if not scope.department:
        return None
    origin = ORIGIN_TEXT.get(scope.origin)
    where = f" ({origin})" if origin else ""
    return f"Отвечаю для: {scope.department}{where} · Изменить"


def chip(ref: SourceRef) -> str:
    return f"[{ref.label}]"


def assign_chip_names(answer: Answer) -> dict[SourceRef, str]:
    """Unique chip text per distinct source (same label but another sentence gets ' ·2')."""
    refs: list[SourceRef] = []
    for st in _all_statements(answer):
        for r in st.sources:
            if r not in refs:
                refs.append(r)
    names: dict[SourceRef, str] = {}
    used: set[str] = set()
    for r in refs:
        name, n = chip(r), 1
        while name in used:
            n += 1
            name = f"[{r.label} ·{n}]"
        used.add(name)
        names[r] = name
    return names


def _all_statements(answer: Answer) -> list[Statement]:
    out = list(answer.statements)
    for v in answer.variants:
        out.extend(v.statements)
    return out


def statement_line(st: Statement, names: dict[SourceRef, str]) -> str:
    chips = " ".join(names.get(r, chip(r)) for r in st.sources)
    return f"{st.text} {chips}".rstrip()


def statements_md(statements: list[Statement], names: dict[SourceRef, str]) -> str:
    return "\n".join(f"- {statement_line(s, names)}" for s in statements)


def variant_md(v: Variant, names: dict[SourceRef, str]) -> str:
    body = statements_md(v.statements, names) if v.statements else f"_{EMPTY_VARIANT}_"
    return f"**{v.department}**\n{body}"


def ticket_card(t: TicketInfo) -> str:
    delivery = DELIVERY_TEXT.get(t.delivery, t.delivery)
    lines = [f"> ✉ **Обращение № {t.number}** · {delivery[0].upper() + delivery[1:]}", f"> {t.reason}"]
    if t.transferred:
        lines.append(f"> Передано: {', '.join(t.transferred)}")
    lines.append("> Ответ придёт в этот диалог и на ваш адрес.")
    return "\n".join(lines)


def answer_markdown(answer: Answer, names: dict[SourceRef, str] | None = None) -> str:
    names = names if names is not None else assign_chip_names(answer)
    parts: list[str] = []
    if answer.outcome is Outcome.LOW_CONFIDENCE:
        parts.append(f"> ⚠️ {answer.caveat or 'Требует подтверждения службой качества'}")
    parts.append(f"**{outcome_label(answer)}**")
    scope = scope_line(answer.scope)
    if scope:
        parts.append(f"_{scope}_")
    if answer.outcome is Outcome.ERROR:
        parts.append("Не удалось получить ответ, вопрос сохранён. Нажмите «Повторить».")
    elif answer.outcome is Outcome.CLARIFY and answer.clarification:
        parts.append(answer.clarification.question)
    else:
        remaining = answer.statements
        if remaining and answer.summary == remaining[0].text:
            parts.append(statement_line(remaining[0], names))
            remaining = remaining[1:]
        elif answer.summary:
            parts.append(answer.summary)
        if remaining:
            parts.append(statements_md(remaining, names))
        if answer.variants:
            parts.extend(variant_md(v, names) for v in answer.variants)
    if answer.caveat and answer.outcome != Outcome.LOW_CONFIDENCE:
        parts.append(f"> {answer.caveat}")
    if answer.ticket:
        parts.append(ticket_card(answer.ticket))
    if answer.profile_note:
        parts.append(f"_{answer.profile_note}_")
    return "\n\n".join(parts)


def point_markdown(pv: PointView) -> str:
    sents = [f"**▌{s}▐**" if i == pv.highlight else s for i, s in enumerate(pv.sentences)]
    lines = [f"**{pv.doc_title}** ({pv.department})", " › ".join(pv.path), "", " ".join(sents)]
    if pv.column:
        lines += ["", f"Столбец таблицы: {pv.column}"]
    lines += ["", f"Версия {pv.version_no}, загружена {pv.uploaded_at}"]
    if pv.neighbours:
        lines += ["", "Рядом:"] + [f"- {n}: {t}" for n, t in pv.neighbours]
    return "\n".join(lines)


def clarify_actions(answer: Answer) -> list[ActionSpec]:
    if answer.outcome is not Outcome.CLARIFY or not answer.clarification:
        return []
    return [ActionSpec("choose", o, {"option": o}) for o in [*answer.clarification.options, DONT_KNOW]]


def answer_actions(answer: Answer, message_text: str, message_id: str) -> list[ActionSpec]:
    """Buttons under an answer: retry on error, rating, scope change, profile."""
    if answer.outcome is Outcome.ERROR:
        return [ActionSpec("retry", "🔁 Повторить", {"text": message_text, "message_id": message_id})]
    acts: list[ActionSpec] = []
    if answer.outcome is not Outcome.CLARIFY and answer.answer_id:
        acts += [
            ActionSpec("rate_up", "👍", {"answer_id": answer.answer_id}),
            ActionSpec("rate_down", "👎", {"answer_id": answer.answer_id}),
        ]
    if answer.scope.department:
        acts.append(ActionSpec("change_scope", "Изменить подразделение", {"text": message_text}))
    return acts


def reason_actions(answer_id: str) -> list[ActionSpec]:
    return [ActionSpec("rate_reason", r, {"answer_id": answer_id, "reason": r}) for r in DOWN_REASONS]


def forward_action(answer_id: str, reason: str) -> ActionSpec:
    return ActionSpec("rate_forward", "Передать в службу качества", {"answer_id": answer_id, "reason": reason})


def example_actions() -> list[ActionSpec]:
    return [ActionSpec("example", q, {"text": q}) for q in EXAMPLES] + [
        ActionSpec("show_profile", "Что я о вас помню", {})
    ]


def profile_markdown(p: Profile) -> str:
    if not p.department and not p.position:
        return "Пока я о вас ничего не помню. Подразделение запомню, когда вы его уточните."
    lines = ["**Что я о вас помню**"]
    if p.department:
        lines.append(f"- Подразделение: {p.department}")
    if p.position:
        lines.append(f"- Должность: {p.position}")
    if p.confirmed_at:
        lines.append(f"- Подтверждено: {p.confirmed_at}")
    return "\n".join(lines)


def expert_notice() -> str:
    return (
        "Вы вошли как служба качества. Страницы администрирования (журнал, очередь обращений, FAQ) "
        "появятся позже: [/admin](/admin). Пока вы можете пользоваться чатом как сотрудник."
    )


def ref_payload(ref: SourceRef) -> dict:
    return dict(doc_id=ref.doc_id, doc_title=ref.doc_title, department=ref.department, version_no=ref.version_no,
                point=ref.point, sentence=ref.sentence, column=ref.column, faq_id=ref.faq_id)


def ref_from_payload(p: dict) -> SourceRef:
    return SourceRef(**{k: p.get(k) for k in ("doc_id", "doc_title", "department", "version_no", "point", "sentence", "column", "faq_id")})


def source_actions(names: dict[SourceRef, str]) -> list[ActionSpec]:
    """One button per source: opens the point in the side panel."""
    return [ActionSpec("open_source", name, ref_payload(r)) for r, name in names.items()]


ERROR_TEXTS = {
    "timeout": "Ответ не получен за отведённое время. Попробуйте задать вопрос ещё раз.",
    "http": "Сервис не смог обработать запрос. Повторите попытку позже.",
    "unavailable": "Сервис ответов сейчас недоступен. Повторите вопрос чуть позже.",
    "cancelled": "Запрос остановлен.",
    "busy": "Дождитесь ответа на предыдущий вопрос.",
}
