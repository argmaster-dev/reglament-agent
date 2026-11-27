"""Executor questions: the schema admits the verbs of the offered sentences, the verifier still binds them."""

from reglament_agent.answer import Outcome, SourceRef
from reglament_agent.dialog.grounding import Anchor, assemble, model_ref, prompt

DEPARTMENT = "Служба эксплуатации"


def _items():
    def anchor(point, text, rank):
        return Anchor(SourceRef("synthetic", "Регламент эксплуатации", DEPARTMENT, 1, point, sentence=0), text, "", rank)

    return {
        "S1": anchor("1.1", "Архитектор сверяет комплект актов перед закрытием наряда.", 0),
        "S2": anchor("2.1", "Бухгалтер согласовывает график отпусков.", 1),
    }


def _assemble(question, action):
    items = _items()
    item = items["S1"]
    trace = {}
    data = {"outcome": "answer", "claims": [
        {"ref": model_ref("S1", item), "actor": "архитектор", "action": action, "text": item.text}]}
    return items, assemble(question, data, items, [DEPARTMENT], False, trace, list(items.values())), trace


def test_schema_contains_verb_of_each_offered_sentence():
    question = "Кто сверяет комплект актов перед закрытием наряда?"
    _, schema = prompt(question, _items(), False)
    by_id = {
        shape["properties"]["ref"]["enum"][0]["id"]: shape["properties"]
        for shape in schema["properties"]["claims"]["items"]["anyOf"]
    }
    assert set(by_id) == {"S1", "S2"}
    assert {"", "сверяет"} <= set(by_id["S1"]["action"]["enum"])
    assert "согласовывает" not in by_id["S1"]["action"]["enum"]
    assert {"", "согласовывает"} <= set(by_id["S2"]["action"]["enum"])
    assert "сверяет" not in by_id["S2"]["action"]["enum"]
    assert "архитектор" in by_id["S1"]["actor"]["enum"] and "бухгалтер" not in by_id["S1"]["actor"]["enum"]
    assert "бухгалтер" in by_id["S2"]["actor"]["enum"] and "архитектор" not in by_id["S2"]["actor"]["enum"]


def test_schema_verbs_for_unlisted_stems_in_other_sentences():
    items = {"S1": Anchor(SourceRef("d", "Т", DEPARTMENT, 1, "3.1", sentence=0),
                          "Бухгалтер выверяет остатки склада ежемесячно.", "", 0)}
    _, schema = prompt("Кто выверяет остатки склада?", items, False)
    assert "выверяет" in schema["properties"]["claims"]["items"]["properties"]["action"]["enum"]


def test_own_action_is_kept():
    _, answer, trace = _assemble("Кто сверяет комплект актов перед закрытием наряда?", "сверяет")
    assert answer.outcome is Outcome.ANSWER
    assert trace["dropped"] == []


def test_action_from_another_point_is_not_attributed():
    items, answer, trace = _assemble("Кто сверяет комплект актов перед закрытием наряда?", "согласовывает")
    assert answer.outcome is Outcome.NO_BASIS
    assert trace["dropped"] == ["action_missing"]


def test_nouns_with_verb_like_endings_are_not_actions():
    from reglament_agent.dialog.grounding import sentence_actions
    from reglament_agent.dialog.lexicon import words

    found = sentence_actions(words(
        "Руководитель проекта отвечает за бюджет и результат, ответственность за часть работ несёт Логист"))
    assert {"отвечает", "несет"} <= found
    assert not found & {"бюджет", "результат", "ответственность", "часть"}
