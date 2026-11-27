"""К4: каждый отказ полезен; ответ из FAQ помечен «подтверждено службой качества» с датой."""

import pytest

from reglament_agent.answer import Answer, Outcome, SourceRef, Statement, TicketInfo
from reglament_agent.chat import render as R
from reglament_agent.storage.sources import faq_confirmations, resolve_point

from .test_practice import add_doc, add_faq

TICKET = TicketInfo("2026-0042", "в регламентах нет ответа", "sent", ["вопрос"])
NEAR = "п. 2.2 «Правила закупок» (ОЗ): Закупку до лимита оформляет заказчик."
SUMMARY = f"В регламентах ответа нет.\nТочного пункта нет. Есть пункты о том же предмете:\n{NEAR}"
COVERED = Statement("Заявку на закупку подаёт заказчик.", [SourceRef("zakup", "Правила закупок", "ОЗ", 1, "2.1")])
REFUSALS = [Outcome.NO_BASIS, Outcome.CONFLICT, Outcome.EXTERNAL_DOC]  # EXTERNAL_DOC = вне регламентов


@pytest.mark.parametrize("outcome", REFUSALS)
def test_every_refusal_shows_ticket_and_where_answer_comes(outcome):
    md = R.answer_markdown(Answer(outcome, "Ответа нет.", ticket=TICKET))
    assert "Обращение № 2026-0042" in md  # мелкой строкой, а не только в ссылке на статус
    assert md.index("в копии") < md.index("2026-0042")  # сначала что произошло, номер вторичен
    assert "по почте" in md and "в этот диалог" in md and "службу качества" in md
    assert "**✉️ Обращение" not in md  # номер не заголовок


@pytest.mark.parametrize("outcome", REFUSALS)
def test_near_points_are_listed_and_clickable(outcome):
    a = Answer(outcome, SUMMARY, ticket=TICKET)
    md = R.answer_markdown(a)
    assert "Близкие пункты: [ОЗ · п. 2.2]" in md
    assert "Закупку до лимита" not in md  # коротко: только ссылки, без длинного блока
    ref = SourceRef("zakup", "Правила закупок", "ОЗ", 3, "2.2")
    (act,) = R.near_actions(a, lambda near: ref)
    assert act.name == "open_source" and R.ref_from_payload(act.payload) == ref


def test_near_points_are_limited_to_three_in_one_line():
    lines = "\n".join(f"п. 2.{i} «Правила закупок» (ОЗ): Текст {i}." for i in range(1, 6))
    a = Answer(Outcome.NO_BASIS, f"Нет.\n{lines}", ticket=TICKET)
    line = next(l for l in R.answer_markdown(a).splitlines() if l.startswith("Близкие пункты"))
    assert line.count("[ОЗ") == 3
    assert len(R.near_actions(a, lambda n: SourceRef("zakup", "Правила закупок", "ОЗ", 1, n.point))) == 3


def test_off_topic_sends_to_manager_without_ticket():
    from reglament_agent.dialog.route import _ESCALATE
    assert Outcome.OFF_TOPIC not in _ESCALATE  # маршрут не заводит обращение и письмо
    md = R.answer_markdown(Answer(Outcome.OFF_TOPIC, "Я помогаю с вопросами по внутренним регламентам."))
    assert "обратитесь к своему руководителю" in md and "не касается регламентов" in md
    assert "Обращение" not in md and "службу качества" not in md and "почт" not in md
    assert md.count("руководителю") == 1 and "Я помогаю" not in md


def test_unresolvable_near_point_gets_no_button():
    assert R.near_actions(Answer(Outcome.NO_BASIS, SUMMARY), lambda near: None) == []


def test_refusal_without_near_points_has_no_empty_header():
    assert "Близкие пункты" not in R.answer_markdown(Answer(Outcome.NO_BASIS, "Нет.", ticket=TICKET))


def test_covered_part_is_answered_next_to_the_refusal():
    a = Answer(Outcome.NO_BASIS, "На вторую часть ответа нет.", [COVERED], ticket=TICKET)
    md = R.answer_markdown(a)
    assert "Заявку на закупку подаёт заказчик." in md and "2026-0042" in md


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
