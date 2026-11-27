"""A document used as input does not establish who creates that document."""

import pytest

from reglament_agent.answer import Outcome, SourceRef
from reglament_agent.dialog import relevance
from reglament_agent.dialog.grounding import Anchor, assemble, model_ref, vocabulary

QUESTION = "Кто создаёт акт на поставку?"
SOURCE_ONLY = "Оператор создаёт накладную и переносит дату из согласованного акта на поставку."


def anchor(point, text):
    ref = SourceRef("test", "Регламент", "Склад", 1, point, sentence=0)
    return Anchor(ref, text, "", 0)


def example_proposal(items):
    return {"outcome": "answer", "claims": [
        {"ref": model_ref(key, row), "text": row.text, "action": "создаёт"}
        for key, row in items.items()
    ]}


@pytest.mark.parametrize("text", [
    "Из черновика оператор создаёт акт на поставку.",
    "Из старого акта на поставку оператор создаёт новый акт на поставку.",
])
def test_creation_topic_distinguishes_direct_object_from_source(text):
    positive = anchor("1.1", text)
    source_only = anchor("2.1", SOURCE_ONLY)
    assert list(vocabulary(QUESTION, [positive, source_only]).values()) == [positive]


def test_assembly_rechecks_the_object_even_if_the_model_selected_the_wrong_reference():
    correct = anchor("1.1", "Из черновика оператор создаёт акт на поставку.")
    wrong = anchor("2.1", SOURCE_ONLY)
    items = {"S1": correct, "S2": wrong}
    trace = {}
    reply = assemble(QUESTION, example_proposal(items), items, ["Склад"], False, trace, list(items.values()))
    assert reply.outcome == Outcome.ANSWER
    assert [ref for statement in reply.statements for ref in statement.sources] == [correct.ref]
    assert trace["dropped"] == ["creation_object_is_source"]


def test_navigation_can_still_show_an_explicit_alternative_object():
    nearby = anchor("2.1", "Оператор создаёт накладную и переносит дату из акта на приёмку.")
    text, refs = relevance.nearby_evidence(QUESTION, [nearby])
    assert refs == [nearby.ref]
    assert "(Склад)" in text
