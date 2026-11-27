from reglament_agent.answer import Outcome, SourceRef
from reglament_agent.dialog.grounding import Anchor, assemble, model_ref


def evaluate(question, pairs):
    items, claims = {}, []
    for i, (text, proposed) in enumerate(pairs):
        key = f"S{i+1}"
        anchor = Anchor(SourceRef("inspection", "Проверка", "Управление контроля", 1, str(i+1), 0), text, "", i)
        items[key] = anchor
        claims.append({"ref": model_ref(key, anchor), "text": proposed or text})
    trace = {}
    result = assemble(question, {"outcome": "answer", "claims": claims}, items,
                      ["Управление контроля"], False, trace, list(items.values()))
    return result, trace


QUESTION = "Кто в Управлении контроля проводит инвентаризацию приборов?"


def test_accepts_explicit_process_owner_and_rejects_a_quote_bound_to_the_recipient():
    duty = "За инвентаризацию приборов отвечает инженер по оборудованию."
    address = "Начальнику склада, который получает результаты инвентаризации приборов."
    other_quote = "Группа снабжения проводит проверку накладных."
    answer, trace = evaluate(QUESTION, [(duty, None), (address, other_quote)])
    assert answer.outcome == Outcome.ANSWER
    assert [(s.text, s.sources[0].point) for s in answer.statements] == [(duty, "1")]
    assert "quote_reference_mismatch" in trace["dropped"]


def test_only_receiving_results_and_mismatching_participant_groups_are_not_evidence():
    for text in ["Начальнику склада направляют итоги инвентаризации приборов.",
                 "Менеджер проводит анкетирование работников.",
                 "За анкетирование заказчиков не отвечает начальник смены."]:
        question = QUESTION if "приборов" in text else "Кто проводит анкетирование клиентов?"
        answer, _ = evaluate(question, [(text, None)])
        assert answer.outcome == Outcome.NO_BASIS and not answer.statements


def test_active_predicate_accepts_owner_and_same_process_but_not_another_activity():
    duty = "Инженер организует инвентаризацию приборов."
    wrong = "Инженер организует совещание, а результаты инвентаризации приборов получает по почте."
    result, _ = evaluate("Кто организует инвентаризацию приборов?", [(duty, None), (wrong, None)])
    assert result.outcome == Outcome.ANSWER
    assert [s.text for s in result.statements] == [duty]


def test_customer_wording_maps_to_the_party_in_the_accountability_clause():
    duty = "Координатор отвечает за анкетирование заказчиков."
    result, _ = evaluate("Кто проводит анкетирование клиентов?", [(duty, None)])
    assert result.outcome == Outcome.ANSWER and [s.text for s in result.statements] == [duty]
