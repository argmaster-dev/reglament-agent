from reglament_agent.answer import Outcome, SourceRef
from reglament_agent.dialog import grounding, project_procedures, relevance


def rule(point, text, department="Сервис"):
    return grounding.Anchor(SourceRef(department, "Правила", department, 1, point, 0), text, "", 0)


def respond(question, sources):
    items = {f"S{i}": a for i, a in enumerate(sources, 1)}
    proposal = {"outcome": "answer", "claims": [
        {"ref": grounding.model_ref(key, a), "text": a.text} for key, a in items.items()
    ]}
    return grounding.assemble(question, proposal, items, ["Сервис"], False, {}, sources)


def test_party_and_obligation_are_bound_before_rendering_a_conditional_procedure():
    question = "Клиент затягивает согласование, что делать?"
    correct = rule("4.3", "В случае просрочки согласования сметы со стороны заказчика "
                   "координатор напоминает о сроке письмом.")
    other_party = rule("7.8", "В случае просрочки согласования сметы со стороны поставщика "
                       "координатор направляет заказчику уведомление.")
    other_obligation = rule("3.2", "В случае просрочки поставки со стороны заказчика "
                            "координатор согласует новую дату.")
    unconditional = rule("5.1", "Клиент передаёт согласование в архив.")
    observation = rule("5.2", "В случае просрочки согласования сметы со стороны заказчика "
                       "общая длительность работ увеличивается.")
    result = respond(question, [other_obligation, unconditional, correct, other_party, observation])
    assert result.outcome == Outcome.ANSWER
    assert [(s.text, s.sources) for s in result.statements] == [(correct.text, [correct.ref])]
    assert result.caveat and "срок" in result.caveat.lower()
    assert relevance.applicable_refs(question, [correct, other_party, other_obligation]) == {correct.ref}


def test_can_reuse_the_delay_rule_for_a_supplier_and_a_different_operation():
    question = "Поставщик задерживает поставку, как поступить?"
    correct = rule("8.3", "В случае нарушения срока поставки со стороны поставщика "
                   "логист направляет уведомление перевозчику.")
    result = respond(question, [correct])
    assert result.outcome == Outcome.ANSWER and result.statements[0].sources == [correct.ref]
    assert result.caveat


def test_explicit_future_due_date_cannot_trigger_an_overdue_remedy():
    question = "Заказчик задерживает оплату, но срок ещё не наступил. Что делать?"
    later = rule("1.4", "В случае просрочки оплаты со стороны заказчика менеджер инициирует взыскание.")
    assert not relevance.applicable_refs(question, [later])
    result = respond(question, [later])
    assert result.outcome == Outcome.NO_BASIS and not result.statements


def test_recording_or_disputing_a_delay_asks_for_another_action():
    quote = rule("1.4", "В случае просрочки оплаты со стороны заказчика менеджер инициирует взыскание.")
    question = "Как отметить в CRM, что клиент задерживает оплату?"
    assert not relevance.applicable_refs(question, [quote])
    assert respond(question, [quote]).outcome == Outcome.NO_BASIS
    assert project_procedures.requested("Клиент задерживает оплату, что делать с отпуском сотрудника?") is None


def test_an_additional_consequence_or_a_different_obligation_requires_its_own_rule():
    assert project_procedures.requested(
        "Поставщик задерживает поставку, наш проект срывается, что делать?"
    ) is None
    assert project_procedures.requested(
        "Что делать, если поставщик задерживает отгрузку, а наш проект срывается?"
    ) is None
    assert project_procedures.requested("Что делать, если поставщик задерживает отгрузку?")
    question = "Клиент затягивает оплату аванса, что делать?"
    final_payment = rule("1.7", "В случае просрочки оплаты за готовые работы со стороны заказчика "
                         "бухгалтер оформляет акт сверки.")
    assert not relevance.applicable_refs(question, [final_payment])
