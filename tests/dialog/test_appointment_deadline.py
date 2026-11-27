import pytest

from reglament_agent.answer import Outcome, SourceRef
from reglament_agent.dialog import grounding, relevance
from reglament_agent.dialog.deadlines import question_mode


def rule(point, sentence):
    ref = SourceRef("support", "Порядок сопровождения", "Сервис", 1, point, 0)
    return grounding.Anchor(ref, sentence, "", 0)


@pytest.mark.parametrize(("query", "expected"), [
    ("За сколько часов назначают ПА на сопровождение?", "any"),
    ("За сколько часов проверяют отчёт?", "any"),
    ("За сколько часов до встречи назначают ПА на сопровождение?", "before"),
    ("За сколько часов заранее проверяют отчёт?", "before"),
    ("За сколько часов после регистрации назначают ПА?", "after"),
    ("За сколько дней надо заводить заявку на приёмку?", "before"),
    ("За сколько дней следует завести заявку на приёмку?", "before"),
])
def test_elapsed_time_with_a_finite_verb_preserves_explicit_reference_events(query, expected):
    assert question_mode(query) == expected


def test_elapsed_appointment_checks_the_role_being_assigned_and_its_stage():
    question = "За сколько часов назначают ПА на сопровождение?"
    right = rule("4.2", "Руководитель назначает на сопровождение сделки ПА не позднее 4 рабочих часов с момента запроса.")
    swapped = rule("4.3", "ПА назначает на сопровождение сделки РП не позднее 8 рабочих часов с момента запроса.")
    stage = rule("4.4", "Руководитель назначает ПА на подготовку сделки в течение 6 часов после запроса.")
    reference = rule("4.5", "ПА передаёт отчёт в течение 7 часов после назначения на сопровождение.")
    rows = [swapped, stage, right, reference]
    items = grounding.vocabulary(question, rows)
    assert list(items.values()) == [right]
    key = next(iter(items))
    answer = grounding.assemble(question, {"outcome": "answer", "claims": [{"ref": grounding.model_ref(key, right)}]},
                                items, ["Сервис"], False, {}, rows)
    assert answer.outcome == Outcome.ANSWER
    assert [(s.text, s.sources) for s in answer.statements] == [(right.text, [right.ref])]


def test_explicit_advance_excludes_an_elapsed_deadline_even_when_the_appointment_matches():
    question = "За сколько часов до встречи назначают ПА на сопровождение?"
    after = rule("3.9", "Руководитель назначает ПА на сопровождение в течение 6 часов после встречи.")
    before = rule("4.1", "Руководитель назначает ПА на сопровождение за 3 часа до встречи.")
    assert relevance.applicable_refs(question, [after, before]) == {before.ref}


def test_shared_word_for_stage_cannot_replace_the_specific_requested_stage():
    question = "За сколько часов назначают ПА на этап проверки?"
    rule_one = rule("3.9", "Отдел назначает ПА на этап проверки в течение 4 часов после обращения.")
    rule_two = rule("4.1", "Отдел назначает ПА на этап согласования в течение 8 часов после обращения.")
    assert relevance.applicable_refs(question, [rule_one, rule_two]) == {rule_one.ref}
