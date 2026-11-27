"""К4: каждый отказ полезен; ответ из FAQ помечен «подтверждено службой качества» с датой."""

from pathlib import Path

import pytest

from reglament_agent.answer import Answer, Outcome, Scope, ScopeOrigin, SourceRef, Statement, TicketInfo
from reglament_agent.chat import render as R
from reglament_agent.storage.sources import faq_confirmations, resolve_point

from .test_practice import add_doc, add_faq

TICKET = TicketInfo("2026-0042", "в регламентах нет ответа", "sent", ["вопрос"])
NEAR = "п. 2.2 «Правила закупок» (ОЗ): Закупку до лимита оформляет заказчик."
SUMMARY = f"В регламентах ответа нет.\nТочного пункта нет. Есть пункты о том же предмете:\n{NEAR}"
COVERED = Statement("Заявку на закупку подаёт заказчик.", [SourceRef("zakup", "Правила закупок", "ОЗ", 1, "2.1")])
REFUSALS = [Outcome.NO_BASIS, Outcome.CONFLICT, Outcome.EXTERNAL_DOC]  # EXTERNAL_DOC = вне регламентов


def resolver(near):
    return SourceRef("zakup", "Правила закупок", "ОЗ", 1, near.point)


@pytest.mark.parametrize("outcome", REFUSALS)
def test_lead_sentence_is_first_and_said_once(outcome):
    summary = "В доступных регламентах не нашёл ответа на этот вопрос.\nНа вторую часть ответа нет."
    a = Answer(outcome, summary, [COVERED], ticket=TICKET, scope=Scope("ОЗ", "project", ScopeOrigin.PROFILE))
    md = R.answer_markdown(a)
    assert md.startswith(R.REFUSAL_LEAD)  # ни заголовка, ни подразделения до фразы
    low = md.lower()
    assert low.count("ответа нет") == 1 and "не нашёл" not in low  # пересказ из итога маршрута убран
    assert "Заявку на закупку подаёт заказчик." in md  # покрытая часть остаётся
    assert md.index("Отвечаю для") > md.index(R.REFUSAL_LEAD)  # подразделение, если нужно, после
    assert "**" not in md.split(R.REFUSAL_LEAD)[0]
    assert "по почте" in md and "в копии" in md and "в этот диалог" in md


def test_route_summary_with_real_content_is_kept():
    a = Answer(Outcome.CONFLICT, "Нашёл общее правило и условие, которое требует отдельной проверки.", ticket=TICKET)
    assert "требует отдельной проверки" in R.answer_markdown(a)


@pytest.mark.parametrize("outcome", REFUSALS)
def test_ticket_number_is_a_small_secondary_line_in_the_element(outcome):
    a = Answer(outcome, "Нет.", ticket=TICKET)
    md = R.answer_markdown(a)
    assert "2026-0042" not in md  # не в тексте ответа и не заголовок
    note = R.refusal_element(a, resolver)
    assert note["ticket"]["text"] == "Обращение № 2026-0042" and note["ticket"]["href"] == "/tickets/2026-0042"
    jsx = (Path(__file__).parents[2] / "public/elements/RefusalNote.jsx").read_text()
    assert "<small" in jsx and "fontSize" in jsx  # мелкий кегль


@pytest.mark.parametrize("delivery", ["pending", "sent", "error"])
def test_refusal_never_shows_a_stale_delivery_state(delivery):
    ticket = TicketInfo("2026-0042", "r", delivery, ["вопрос"])
    a = Answer(Outcome.NO_BASIS, "Нет.", ticket=ticket)
    visible = R.answer_markdown(a) + str(R.refusal_element(a, resolver)).lower()
    assert all(w not in visible.lower() for w in ("ожида", "отправлено", "ошибка отправки", "pending"))
    assert "по почте" in visible  # фраза-лид от состояния доставки не зависит


def test_near_chips_stay_on_one_compact_row():
    jsx = (Path(__file__).parents[2] / "public/elements/RefusalNote.jsx").read_text()
    for needle in ('flexWrap: "nowrap"', 'overflowX: "auto"', 'whiteSpace: "nowrap"', 'textOverflow: "ellipsis"'):
        assert needle in jsx


@pytest.mark.parametrize("outcome", REFUSALS)
def test_near_points_are_clickable_chips_without_duplicate_buttons(outcome):
    a = Answer(outcome, SUMMARY, ticket=TICKET)
    md = R.answer_markdown(a)
    assert "Близкие пункты" not in md and "Закупку до лимита" not in md  # не плоским текстом
    note = R.refusal_element(a, resolver)
    (chip,) = note["near"]
    assert chip["label"] == "ОЗ · п. 2.2"
    assert R.ref_from_payload(chip["source"]) == SourceRef("zakup", "Правила закупок", "ОЗ", 1, "2.2")
    assert not hasattr(R, "near_actions")  # отдельного ряда кнопок нет
    assert R.answer_actions(a, "q", "m1") == [] or all(x.name != "open_source" for x in R.answer_actions(a, "q", "m1"))


def test_structured_nearby_sources_win_over_summary_parsing():
    refs = [SourceRef("zakup", "Правила закупок", "ОЗ", 1, p) for p in ("2.1", "2.1", "2.2", "2.3", "2.4")]
    a = Answer(Outcome.NO_BASIS, SUMMARY, ticket=TICKET)
    a.nearby_sources = refs  # поле приходит из маршрута; в answer.py его добавляет другая ветка
    chips = R.refusal_element(a, lambda n: None)["near"]
    assert [c["label"] for c in chips] == ["ОЗ · п. 2.1", "ОЗ · п. 2.2", "ОЗ · п. 2.3"]


def test_unresolvable_near_point_gets_no_chip():
    assert R.refusal_element(Answer(Outcome.NO_BASIS, SUMMARY), lambda near: None) is None


def test_near_points_are_limited_to_three():
    lines = "\n".join(f"п. 2.{i} «Правила закупок» (ОЗ): Текст {i}." for i in range(1, 6))
    a = Answer(Outcome.NO_BASIS, f"Нет.\n{lines}", ticket=TICKET)
    assert len(R.refusal_element(a, resolver)["near"]) == 3


def test_off_topic_sends_to_manager_without_ticket():
    from reglament_agent.dialog.route import _ESCALATE
    assert Outcome.OFF_TOPIC not in _ESCALATE  # маршрут не заводит обращение и письмо
    a = Answer(Outcome.OFF_TOPIC, "Я помогаю с вопросами по внутренним регламентам.")
    md = R.answer_markdown(a)
    assert "обратитесь к своему руководителю" in md and "не касается регламентов" in md
    assert "Обращение" not in md and "службу качества" not in md and "почт" not in md
    assert md.count("руководителю") == 1 and "Я помогаю" not in md
    assert R.refusal_element(a, resolver) is None


def test_refusal_without_near_points_has_no_empty_chips():
    note = R.refusal_element(Answer(Outcome.NO_BASIS, "Нет.", ticket=TICKET), resolver)
    assert note["near"] == []


def test_covered_part_is_answered_next_to_the_refusal():
    a = Answer(Outcome.NO_BASIS, "На вторую часть ответа нет.", [COVERED], ticket=TICKET)
    md = R.answer_markdown(a)
    assert "Заявку на закупку подаёт заказчик." in md


def test_faq_answer_is_labelled_with_date():
    faq = SourceRef("zakup", "FAQ службы качества", "ОЗ", 1, "", faq_id=7)
    a = Answer(Outcome.EXPERT_ANSWER, "Ответ", [Statement("Можно.", [faq])])
    md = R.answer_markdown(a, faq_dates={7: "2026-11-25"})
    assert "подтверждено службой качества" in md.lower() and "25.11.2026" in md
    assert "подтверждено службой качества" not in R.answer_markdown(Answer(Outcome.ANSWER, "x", [COVERED])).lower()


def test_confirmations_lookup_and_point_resolution_use_published_data_only(isolated):
    conn = isolated[0]
    vid = add_doc(conn)
    live = add_faq(conn, vid, "Вопрос", "Ответ.")
    gone = add_faq(conn, vid, "Снятый", "Ответ.", status="deleted")
    assert faq_confirmations(conn, [live, gone]) == {live: "2026-11-25"}
    near = R.near_points(Answer(Outcome.NO_BASIS, SUMMARY))[0]
    ref = resolve_point(conn, near.doc_title, near.department, near.point)
    assert (ref.doc_id, ref.version_no, ref.point) == ("zakup", 1, "2.2")
    assert resolve_point(conn, "Нет такого", "ОЗ", "2.2") is None
