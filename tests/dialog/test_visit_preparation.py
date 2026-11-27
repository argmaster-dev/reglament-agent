from reglament_agent.answer import Outcome, SourceRef
from reglament_agent.dialog import grounding


def rows(point, title, lead, *steps, version=2, document="demo"):
    return [
        grounding.Anchor(SourceRef(document, "Учебный порядок", "Монтаж", version, point, i),
                         text, lead, 4, title)
        for i, text in enumerate(steps, 1)
    ]


def ready_list():
    return rows("7", "Подготовка к началу монтажных работ", "Руководитель проекта:",
                "согласует с клиентом расписание работ на объекте клиента;",
                "получает пропуска для инженеров;",
                "инициирует проверку инструмента, если она предусмотрена договором;",
                "проверяет наличие допуска при необходимости.",
                "После возвращения с объекта оформляет отчёт о результатах.",
                "На совещании согласует протокол встречи.")


def render(question, available, selection):
    claims = [{"ref": grounding.model_ref(k, a), "text": a.text} for k, a in available.items()
              if a.ref in selection]
    return grounding.assemble(question, {"outcome": "answer", "claims": claims}, available,
                              ["Монтаж"], False, {}, list(available.values()))


def test_actor_list_for_work_at_the_site_offers_every_step_in_order_with_its_condition():
    question = "Инженеры выезжают на объект заказчика. Что должен подготовить РП?"
    assigned = ready_list()
    others = rows("8", "Подготовка к началу монтажных работ", "ГИП:",
                  "согласует время работ на объекте клиента;", "получает пропуск другого сотрудника.")
    meeting = rows("9", "Совещание на объекте", "РП:",
                   "согласует повестку встречи на площадке клиента.")
    offered = grounding.vocabulary(question, [*meeting, *others, *assigned])
    assert list(offered.values()) == assigned[:4]
    result = render(question, offered, [assigned[1].ref])
    assert result.outcome == Outcome.ANSWER and result.caveat
    assert "работ" in result.caveat.lower() and "если" in result.caveat.lower()
    assert [(s.text, s.sources) for s in result.statements] == [(a.text, [a.ref]) for a in assigned[:4]]


def test_onsite_and_preparation_cannot_be_taken_from_different_point_or_revision():
    question = "Монтажники выезжают на объект клиента. Что должен подготовить РП?"
    other = rows("7", "Подготовка к началу монтажных работ", "РП:", "получает секретный отчёт другой редакции.", version=1)
    elsewhere = rows("6", "Подготовка к началу монтажных работ", "РП:", "получает письмо из другого пункта.")
    assigned = ready_list()
    assert list(grounding.vocabulary(question, [*other, *elsewhere, *assigned]).values()) == assigned[:4]


def test_onsite_agenda_return_or_report_do_not_inherit_work_preparation():
    assigned = ready_list()
    for question in (
        "Инженеры выезжают на объект. Как подготовиться к совещанию с клиентом?",
        "Что подготовить РП после возвращения из поездки с объекта?",
        "Что РП должен подготовить для отчёта о выезде на объект?",
    ):
        offered = grounding.vocabulary(question, assigned)
        selected = [a.ref for a in offered.values() if a == assigned[1]]
        result = render(question, offered, selected)
        assert not result.caveat or "Если цель выезда" not in result.caveat
        assert assigned[2].text not in [st.text for st in result.statements]


def test_work_at_a_different_partys_site_cannot_establish_the_requested_checklist():
    q = "Инженеры выезжают на объект поставщика. Что должен подготовить РП?"
    assigned = ready_list()
    choices = grounding.vocabulary(q, assigned)
    result = render(q, choices, [a.ref for a in choices.values()])
    assert result.outcome == Outcome.NO_BASIS


def test_direct_departure_rule_remains_available_alongside_a_site_work_checklist():
    q = "Инженеры выезжают на объект клиента. Что должен подготовить РП?"
    direct = rows("9", "Порядок выезда на объект", "РП:",
                  "Перед выездом на объект клиента РП оформляет допуск на въезд.")
    offered = grounding.vocabulary(q, [*ready_list(), *direct])
    assert direct[0] in offered.values()


def test_a_partial_list_shown_to_the_model_does_not_authorize_quoting_unoffered_actions():
    q = "Инженеры выезжают на объект клиента. Что должен подготовить РП?"
    source = ready_list()
    offered = {"S1": source[0]}
    claims = [{"ref": grounding.model_ref("S1", source[0]), "text": source[0].text}]
    result = grounding.assemble(q, {"outcome": "answer", "claims": claims}, offered, ["Монтаж"], False, {}, source)
    assert result.outcome == Outcome.NO_BASIS and not result.statements
