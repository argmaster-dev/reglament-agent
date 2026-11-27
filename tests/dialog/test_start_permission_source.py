from reglament_agent.answer import Outcome, SourceRef
from reglament_agent.dialog.grounding import Anchor, assemble, model_ref


def provision(point, *sentences):
    return [
        Anchor(SourceRef("dispatch", "Порядок отгрузки", "Склад", 1, point, index),
               text, " ".join(sentences), 0)
        for index, text in enumerate(sentences)
    ]


def select(question, rows, selected):
    candidates = {f"S{i}": row for i, row in enumerate(rows, 1)}
    claims = [{"text": row.text, "ref": model_ref(key, row)}
              for key, row in candidates.items() if row in selected]
    return assemble(question, {"outcome": "answer", "claims": claims}, candidates, ["Склад"], False, {}, rows)


HEADER = "Исполнение проекта начинается при выполнении одного из условий:"


def test_alternative_start_conditions_do_not_replace_verified_prohibition_from_another_point():
    rule = provision("2.3", "При отсутствии согласованной заявки запрещается начинать работы на складе.")
    other = provision("9.4", HEADER, "получена согласованная накладная;", "перевозчик оплатил доставку.")
    answer = select("Можно ли начать работы без согласованной заявки?", other + rule, rule)
    assert answer.outcome == Outcome.ANSWER
    assert [s.text for s in answer.statements] == [rule[0].text]
    assert answer.statements[0].sources == [rule[0].ref]


def test_alternatives_expand_within_selected_point_when_other_points_have_similar_headers():
    shipping = provision("2.3", HEADER, "подписан договор на отгрузку;", "получено разрешение владельца.")
    other = provision("9.4", HEADER, "подписан договор на обучение;", "согласовано обучение.")
    answer = select("Можно ли начать работы без договора?", other + shipping, [shipping[1]])
    assert answer.outcome == Outcome.ANSWER
    assert [(s.text, s.sources) for s in answer.statements] == [(a.text, [a.ref]) for a in shipping]
