from reglament_agent.answer import Outcome, SourceRef
from reglament_agent.dialog import grounding, relevance


def row(doc, point, text, idx=0):
    return grounding.Anchor(SourceRef(doc, doc, doc, 1, point, idx), text, "", 0)


def test_request_creation_deadline_binds_the_timed_action_and_the_explicit_event():
    setup = row("Лаборатория", "4.2", "Сотрудник создаёт заявку для привлечения лаборатории.")
    test = row("Лаборатория", "4.2", "Аудит должен быть инициирован за две недели до подачи конкурса.", 1)
    intake = row("Склад", "2.7", "Заявка на аудит должна быть инициирована за три рабочих дня до отправки отчёта.")
    risk = row("Склад", "6.9", "Обсуждение риска проводится до завершения аудита. Заявка учитывается отдельно.")
    rows = [risk, intake, test, setup]
    plain = "За сколько дней надо заводить заявку на аудит?"
    event = "Идём на конкурс, за сколько надо заводить заявку на аудит?"
    assert relevance.applicable_refs(plain, rows) == {test.ref, intake.ref}
    assert relevance.applicable_refs(event, rows) == {test.ref}
    items = grounding.vocabulary(event, rows)
    assert list(items.values()) == [test]
    key = next(iter(items))
    answer = grounding.assemble(
        event, {"outcome": "answer", "claims": [{"ref": grounding.model_ref(key, test)}]},
        items, ["Лаборатория"], False, {}, rows,
    )
    assert answer.outcome == Outcome.ANSWER and answer.statements[0].sources == [test.ref]


def test_generic_request_uses_its_documented_process_and_cannot_inherit_an_unrelated_purpose():
    generic = row("Склад", "2.7", "Заявка должна быть инициирована за три рабочих дня до отправки отчёта.")
    attached = row("Склад", "5.1", "Аудит выдаётся в Заявку после согласования.")
    wrong_type = row("Склад", "6.9", "Заявка на ремонт должна быть создана за семь дней до отправки отчёта.")
    wrong_action = row("Склад", "7.1", "Заявку на аудит закрывают за два дня до отправки отчёта.")
    remote = row("Другой отдел", "5.1", "Выплата выдаётся в Заявку после согласования.")
    rows = [generic, attached, wrong_type, wrong_action, remote]
    assert relevance.applicable_refs("За сколько дней надо создать заявку на аудит?", rows) == {generic.ref}
    assert not relevance.applicable_refs("За сколько дней надо создать заявку на выплату?", rows)
    assert not relevance.applicable_refs("За сколько дней надо создать заявку на перевозку?", rows)
    question = "За сколько дней надо создать заявку на аудит?"
    items = grounding.vocabulary(question, rows)
    key = next(key for key, candidate in items.items() if candidate == generic)
    partial = grounding.assemble(question, {"outcome": "answer", "claims": [{"ref": grounding.model_ref(key, generic)}]},
                                 items, ["Склад"], False, {}, rows)
    assert partial.outcome == Outcome.LOW_CONFIDENCE and partial.caveat
    assert [(s.text, s.sources) for s in partial.statements] == [(generic.text, [generic.ref])]


def test_unrelated_opening_or_subordinate_event_cannot_supply_the_timed_object():
    other = row("Склад", "6.9", "Склад создаёт реестр за две недели до регистрации заявки на аудит.")
    later = row("Склад", "7.4", "При создании заявки на аудит инспектор рассматривает риски "
                "за два рабочих дня до конкурса.")
    assert not relevance.applicable_refs("Идём на конкурс, за сколько надо заводить заявку на аудит?", [other, later])
    prohibited = row("Склад", "8.4", "Для плановой работы склад не создаёт заявку.")
    separate = row("Склад", "8.4", "Аудит должен быть инициирован за две недели до конкурса.", 1)
    assert not relevance.applicable_refs("За сколько надо создать заявку на аудит?", [prohibited, separate])


def test_full_request_type_and_its_local_definition_take_precedence_over_a_document_mention():
    rule = row("Сервис", "1.4", "Заявка на проверку оборудования должна быть создана за два дня до отправки отчёта.")
    definition = row("Сервис", "2.7", "Оператор создаёт заявку на замену.")
    generic = row("Сервис", "2.7", "Заявка должна быть инициирована за пять дней до отправки отчёта.", 1)
    elsewhere = row("Сервис", "4.8", "Проверка выдаётся в заявку после подтверждения.")
    rows = [rule, definition, generic, elsewhere]
    assert relevance.applicable_refs("За сколько надо создать заявку на проверку оборудования?", rows) == {rule.ref}
    assert not relevance.applicable_refs("За сколько надо создать заявку на проверку помещения?", rows)
    assert not relevance.applicable_refs("За сколько надо создать заявку на проверку?", rows)
