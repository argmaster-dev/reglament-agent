from reglament_agent.answer import Outcome, SourceRef
from reglament_agent.dialog import grounding, project_procedures, relevance


def row(department, point, text):
    return grounding.Anchor(SourceRef(department, "Порядок снабжения", department, 1, point, 0), text, "", 0)


def test_purchase_binds_the_requested_item_to_the_operation_not_a_different_item_in_the_sentence():
    query = "Надо купить датчики для проекта, как оформить?"
    needed = row("Склад", "2.8", "Закупка датчиков осуществляется через заявку поставщику.")
    also = row("Лаборатория", "5.4", "Инженер организует размещение заказов на датчики и кабели.")
    wrong = row("Склад", "7.1", "Закупка кабелей осуществляется через заявку, к которой прикладывается каталог датчиков.")
    incidental = row("Склад", "7.2", "При выдаче датчиков специалист обращается в отдел закупок.")
    excluded = row("Склад", "7.3", "Закупка оборудования, исключая датчики, осуществляется через фонд.")
    negative = row("Склад", "7.4", "Закупка датчиков не осуществляется через отдел снабжения.")
    arr = [wrong, incidental, excluded, negative, needed, also]
    assert relevance.applicable_refs(query, arr) == {needed.ref, also.ref}
    items = grounding.vocabulary(query, arr)
    assert set(items.values()) == {needed, also}


def test_software_acquisition_identifies_the_procurement_only_and_preserves_the_licence_boundary():
    question = "Требуется приобрести лицензии на ПО для проекта, как это оформить?"
    correct = row("Снабжение", "8.3", "Закупка программного обеспечения осуществляется через внутренний заказ.")
    lowered = row("Снабжение", "4.4", "Закупка спецодежды осуществляется по заказу подразделения.")
    rows = [lowered, correct]
    items = grounding.vocabulary(question, rows)
    assert list(items.values()) == [correct]
    key = next(iter(items))
    result = grounding.assemble(question, {"outcome": "answer", "claims": [{"ref": grounding.model_ref(key, correct)}]},
                                items, ["Снабжение"], False, {}, rows)
    assert result.outcome == Outcome.ANSWER and result.statements[0].sources == [correct.ref]
    assert result.caveat and "лиценз" in result.caveat.lower()


def test_right_to_perform_work_is_not_a_purchase_procedure():
    assert project_procedures.requested("У нас есть лицензия на монтаж, можем ли приступать к работам?") is None
    assert project_procedures.requested("Как получить лицензию на деятельность?") is None
    assert project_procedures.requested("Надо купить лицензию на перевозки, как оформить?") is None
    assert project_procedures.requested("Клиент хочет купить датчики, как оформить продажу?") is None
