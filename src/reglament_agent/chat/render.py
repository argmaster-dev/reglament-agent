"""Pure rendering helpers: Answer -> markdown strings and action specs. No Chainlit imports."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Callable

from reglament_agent.chat.aspects import Advice, Law
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
CONFIRMED = "подтверждено службой качества"
PRACTICE_TITLE = "Практика применения"
PRACTICE_LIMIT = 3
# «п. 2.2 «Название» (Подразделение): текст»: так маршрут называет близкие пункты в тексте итога.
_NEAR_LINE = re.compile(r"^п\. (\S+) «(.+?)» \((.+?)\): (.*)$")
ADVICE_TITLE = "На что обратить внимание"
ADVICE_LABEL = "Не из регламентов — общий совет. Уточните у юриста или в службе качества."
REFUSAL_LEAD = (
    "В регламентах ответа нет. Я отправил запрос в службу качества по почте, вы в копии. "
    "Ответ придёт на почту и в этот диалог."
)
OFF_TOPIC_HINT = "Этот вопрос не касается регламентов компании — обратитесь к своему руководителю."
NEAR_LIMIT = 3
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
    "Напишите вопрос или выберите пример. [Мои диалоги](/history) · Отзыв о последнем ответе: /feedback"
)


@dataclass(frozen=True)
class ActionSpec:
    name: str
    label: str
    payload: dict


def outcome_label(answer: Answer) -> str:
    if answer.outcome in TICKET_OUTCOMES:
        return "✉️ Передано в службу качества"
    label = OUTCOME_LABELS[answer.outcome]
    if answer.outcome is Outcome.LOW_CONFIDENCE and answer.ticket:
        label += f" · обращение №{answer.ticket.number}"
    return label


def scope_line(scope: Scope) -> str | None:
    labels = [
        s
        for s in (
            scope.department,
            {"project": "реализация", "presale": "пресейл"}.get(scope.stage, scope.stage),
        )
        if s
    ]
    if not labels:
        return None
    origin = ORIGIN_TEXT.get(scope.origin)
    where = f" ({origin})" if origin else ""
    change = " · Изменить" if scope.department else ""
    return f"Отвечаю для: {', '.join(labels)}{where}{change}"


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
    lines.append(f"> [Проверить статус обращения](/tickets/{t.number})")
    lines.append("> Ответ придёт в этот диалог и на ваш адрес электронной почты.")
    return "\n".join(lines)


@dataclass(frozen=True)
class NearPoint:
    point: str
    doc_title: str
    department: str
    text: str

    @property
    def label(self) -> str:
        return f"{self.department} · п. {self.point}"


def split_near(summary: str) -> tuple[str, list[NearPoint]]:
    """Separate «близкие пункты» (lines the route appends to the summary) from the rest of the text."""
    head, near = [], []
    for line in summary.splitlines():
        m = _NEAR_LINE.match(line.strip())
        if m:
            near.append(NearPoint(m[1], m[2], m[3], m[4]))
        else:
            head.append(line)
    return "\n".join(head).strip(), near


def near_points(answer: Answer) -> list[NearPoint]:
    return split_near(answer.summary)[1]


def near_actions(answer: Answer, resolve: Callable[[NearPoint], SourceRef | None]) -> list[ActionSpec]:
    """Buttons that open the near points in the side panel; points that no longer resolve get none."""
    acts = []
    for near in near_points(answer)[:NEAR_LIMIT]:
        ref = resolve(near)
        if ref is not None:
            acts.append(ActionSpec("open_source", f"Близкий пункт · {near.label}", ref_payload(ref)))
    return acts


def faq_ids(answer: Answer) -> list[int]:
    out: list[int] = []
    for st in _all_statements(answer):
        for r in st.sources:
            if r.faq_id is not None and r.faq_id not in out:
                out.append(r.faq_id)
    return out


def ru_date(iso: str) -> str:
    y, m, d = iso[:10].split("-")
    return f"{d}.{m}.{y}"


def confirmed_line(dates: dict[int, str] | None, ids: list[int]) -> str:
    known = sorted({ru_date(dates[i]) for i in ids if dates and i in dates})
    return "🎓 " + CONFIRMED.capitalize() + (f" · {', '.join(known)}" if known else "")


def ticket_note(t: TicketInfo) -> str:
    """Small secondary line of a refusal: the number is a detail, the lead already says what happens."""
    delivery = DELIVERY_TEXT.get(t.delivery, t.delivery)
    return f"_Обращение № {t.number} · {delivery} · [статус](/tickets/{t.number})_"


def evidence_groups(answer: Answer, names: dict[SourceRef, str]) -> list[dict]:
    def items(statements):
        return [
            {"text": s.text, "refs": [{"label": names[ref], "source": ref_payload(ref)} for ref in s.sources]}
            for s in statements
        ]

    groups = [{"label": "", "items": items(answer.statements)}] if answer.statements else []
    groups.extend(
        {"label": v.department, "items": items(v.statements), "empty": EMPTY_VARIANT} for v in answer.variants
    )
    return groups


def answer_markdown(
    answer: Answer,
    names: dict[SourceRef, str] | None = None,
    *,
    separate_evidence=False,
    faq_dates: dict[int, str] | None = None,
    advice: list[Advice] | None = None,
) -> str:
    names = names if names is not None else assign_chip_names(answer)
    summary, near = split_near(answer.summary)
    if answer.outcome is Outcome.OFF_TOPIC and not summary.startswith("Сообщение длиннее"):
        summary = OFF_TOPIC_HINT  # заменяет общий текст маршрута, без повтора
    parts: list[str] = []
    if answer.outcome is Outcome.LOW_CONFIDENCE:
        parts.append(f"> ⚠️ {answer.caveat or 'Требует подтверждения службой качества'}")
    parts.append(f"**{outcome_label(answer)}**")
    scope = scope_line(answer.scope)
    if scope:
        parts.append(f"_{scope}_")
    if answer.outcome in TICKET_OUTCOMES:
        parts.append(REFUSAL_LEAD)
    if answer.outcome is Outcome.ERROR:
        parts.append("Не удалось получить ответ, вопрос сохранён. Нажмите «Повторить».")
    elif answer.outcome is Outcome.CLARIFY and answer.clarification:
        parts.append(answer.clarification.question)
    elif separate_evidence:
        if summary and not any(summary == s.text for s in _all_statements(answer)):
            parts.append(summary)
    else:
        remaining = answer.statements
        if remaining and summary == remaining[0].text:
            parts.append(statement_line(remaining[0], names))
            remaining = remaining[1:]
        elif summary:
            parts.append(summary)
        if remaining:
            parts.append(statements_md(remaining, names))
        if answer.variants:
            parts.extend(variant_md(v, names) for v in answer.variants)
    ids = faq_ids(answer)
    if ids and answer.outcome not in (Outcome.ERROR, Outcome.CLARIFY):
        parts.append(confirmed_line(faq_dates, ids))
    if near:
        parts.append("Близкие пункты: " + " · ".join(f"[{n.label}]" for n in near[:NEAR_LIMIT]))
    if answer.caveat and answer.outcome != Outcome.LOW_CONFIDENCE:
        parts.append(f"> {answer.caveat}")
    if advice:
        parts.append(advice_md(advice))
    if answer.ticket:
        parts.append(ticket_note(answer.ticket) if answer.outcome in TICKET_OUTCOMES else ticket_card(answer.ticket))
    if answer.profile_note:
        parts.append(f"_{answer.profile_note}_")
    return "\n\n".join(parts)


def advice_md(advice: list[Advice]) -> str:
    """Отдельный блок после ответа по регламенту. Тексты каталога экранируются, как любой чужой текст."""
    esc = html.escape
    bullets = [
        f"- {esc(item.aspect.text)} ({'; '.join(esc(law.reference) for law in item.laws)})" for item in advice
    ]
    return f"**{ADVICE_TITLE}**\n\n_{ADVICE_LABEL}_\n\n" + "\n".join(bullets)


def law_actions(advice: list[Advice]) -> list[ActionSpec]:
    """Кнопка на каждую статью: открывает сохранённый текст в боковой панели."""
    seen: dict[str, Law] = {}
    for item in advice:
        for law in item.laws:
            seen.setdefault(law.id, law)
    return [ActionSpec("open_law", f"Статья · {law.reference}", {"law_id": law.id}) for law in seen.values()]


def law_panel(law: Law) -> dict:
    """Что показывает боковая панель статьи: акт, статья, текст, редакция, источник и дата получения."""
    url = law.source_url if law.source_url.startswith(("http://", "https://")) else ""
    return {
        "act": law.act,
        "article": law.reference,
        "title": law.title,
        "text": law.text,
        "revision": f"редакция {law.revision}",
        "source_url": url,
        "retrieved": f"получено {ru_date(law.retrieved)}",
        "note": ADVICE_LABEL,
    }


def point_panel(pv: PointView) -> dict:
    """Keep the precise sentence and table cell separate from surrounding context."""
    stages = {"project": "реализация", "presale": "пресейл"}
    tables = [block.table for block in pv.blocks if block.table]
    entries = []
    for index, sentence in enumerate(pv.sentences):
        selected = index == pv.highlight
        if any(sentence == " | ".join(t.headers) for t in tables):
            continue
        cells = []
        for table in tables:
            row = next(
                (
                    row
                    for row in table.rows
                    if any(cell.strip() for cell in row)
                    and all(
                        f"{header}: {cell}" in sentence
                        for header, cell in zip(table.headers, row)
                        if cell.strip()
                    )
                ),
                None,
            )
            if row is not None:
                cells = [
                    {
                        "header": header,
                        "text": cell,
                        "selected": selected and (not pv.column or header == pv.column),
                    }
                    for header, cell in zip(table.headers, row)
                    if cell.strip()
                ]
                break
        entries.append({"text": sentence, "selected": selected and not pv.column, "cells": cells})
    panel = {
        "title": pv.doc_title,
        "path": pv.path,
        "department": pv.department,
        "stages": [stages.get(stage, stage) for stage in pv.stages],
        "version": pv.version_no,
        "uploaded": pv.uploaded_at[:10],
        "column": pv.column,
        "entries": entries,
        "neighbours": pv.neighbours,
        "practice": practice_block(pv),
    }
    panel["practice_html"] = practice_html(panel)
    return panel


def practice_block(pv: PointView) -> dict:
    """«Практика применения»: up to PRACTICE_LIMIT published expert answers and the count of the rest."""
    shown = pv.practice[:PRACTICE_LIMIT]
    return {
        "title": PRACTICE_TITLE,
        "items": [
            {"question": p.question, "answer": p.answer, "label": CONFIRMED, "date": ru_date(p.confirmed_at)}
            for p in shown
        ],
        "more": len(pv.practice) - len(shown),
    }


def practice_html(panel: dict) -> str:
    """The practice block as HTML. Every text, however it was written by an expert, is escaped."""
    block = panel["practice"]
    if not block["items"]:
        return ""
    esc = html.escape
    items = "".join(
        f"<li><p><strong>{esc(i['question'])}</strong></p><p>{esc(i['answer'])}</p>"
        f"<p><small>{esc(i['label'])} · {esc(i['date'])}</small></p></li>"
        for i in block["items"]
    )
    more = f"<p>и ещё {block['more']}</p>" if block["more"] else ""
    return f'<section aria-label="{esc(block["title"])}"><h3>{esc(block["title"])}</h3><ul>{items}</ul>{more}</section>'


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
    if answer.scope.department and answer.answer_id:
        acts.append(ActionSpec("change_scope", "Изменить подразделение", {"answer_id": answer.answer_id}))
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
    return "Служба качества · [Обращения](/admin/tickets) · [FAQ](/admin/faq) · [Документы](/admin/documents)"


def ref_payload(ref: SourceRef) -> dict:
    return dict(doc_id=ref.doc_id, doc_title=ref.doc_title, department=ref.department, version_no=ref.version_no,
                point=ref.point, sentence=ref.sentence, column=ref.column, faq_id=ref.faq_id)


def ref_from_payload(p: dict) -> SourceRef:
    return SourceRef(**{k: p.get(k) for k in ("doc_id", "doc_title", "department", "version_no", "point", "sentence", "column", "faq_id")})


def source_actions(names: dict[SourceRef, str]) -> list[ActionSpec]:
    """One button per source: opens the point in the side panel."""
    return [ActionSpec("open_source", name, ref_payload(r)) for r, name in names.items()]


ERROR_TEXTS = {
    "timeout": "Ответ не получен за отведённое время. Вопрос сохранён для повтора. Нажмите «Повторить».",
    "http": "Сервис не смог обработать запрос. Повторите попытку позже.",
    "unavailable": "Сервис ответов сейчас недоступен. Вопрос сохранён для повтора. Нажмите «Повторить».",
    "cancelled": "Запрос остановлен.",
    "busy": "Дождитесь ответа на предыдущий вопрос.",
    "user_busy": "Ваш вопрос уже обрабатывается в другом диалоге. Дождитесь ответа и повторите.",
    "stand_busy": "Стенд занят. Подождите немного и повторите вопрос.",
    "user_rate": "Вы отправили много вопросов за минуту. Подождите и повторите вопрос.",
    "stand_rate": "Стенд получает много вопросов. Подождите немного и повторите вопрос.",
}
