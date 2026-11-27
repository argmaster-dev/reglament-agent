from reglament_agent.answer import Outcome, SourceRef
from reglament_agent.dialog.grounding import Anchor, assemble, model_ref


def answer(question, *sentences):
    anchors = {
        str(i): Anchor(SourceRef("guide", "Порядок", "Продажи", 1, str(i), 0), text, "", i)
        for i, text in enumerate(sentences, 1)
    }
    claims = [{"ref": model_ref(key, anchor), "text": anchor.text} for key, anchor in anchors.items()]
    return assemble(question, {"outcome": "answer", "claims": claims}, anchors, [], False, {})


def test_recording_instruction_binds_the_event_to_the_named_system():
    question = "Как отметить в карточке в CRM, что клиент просрочил оплату?"
    other_event = "Сотрудники фиксируют расхождения в карточке CRM в виде комментариев."
    other_action = "При просрочке оплаты менеджер направляет заказчику требование."
    other_system = "Просрочку оплаты фиксируют в карточке ERP."
    other_party = "В CRM отмечают просрочку оплаты со стороны поставщика."
    separate_instructions = "В CRM фиксируют смену адреса; просрочку оплаты клиента рассматривает бухгалтерия."
    matching = "В карточке CRM менеджер отмечает просрочку оплаты клиента отдельным статусом."
    unrelated = other_event, other_action, other_system, other_party, separate_instructions
    result = answer(question, *unrelated, matching)
    assert [statement.text for statement in result.statements] == [matching]
    assert answer(question, *unrelated).outcome is Outcome.NO_BASIS


def test_recording_another_event_and_system_preserves_the_proven_instruction():
    question = "Как указать в ERP, что поставка отменена?"
    matching = "В ERP укажите, что поставка отменена, в поле состояния."
    assert [s.text for s in answer(question, matching).statements] == [matching]
