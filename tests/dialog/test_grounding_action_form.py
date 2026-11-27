"""The schema must not offer an instrumental complement as an executable action."""
from reglament_agent.answer import Outcome, SourceRef
from reglament_agent.dialog.grounding import Anchor, assemble, model_ref, prompt


QUESTION = "Кто проверяет заявку и по чему сверяется?"
DEPARTMENT = "Отдел контроля"


def sources():
    def anchor(point, text):
        return Anchor(SourceRef("synthetic", "Проверка", DEPARTMENT, 1, point, 0), text, "", 0)

    return {
        "S1": anchor("1.1", "Архитектор проводит первичную проверку заявки по контрольному листу."),
        "S2": anchor("2.2", "Бухгалтер направляет запрос с перечнем вопросов и с приложением."),
        "S3": anchor("3.3", "Мы ведём реестр и проверяем карточки."),
    }


def test_action_options_exclude_instrumental_nouns_and_preserve_finite_verbs():
    _, schema = prompt(QUESTION, sources(), False)
    actions = set(schema["properties"]["claims"]["items"]["properties"]["action"]["enum"])

    assert {"проводит", "направляет", "ведем", "проверяем"} <= actions
    assert not {"перечнем", "приложением"} & actions


def test_wrong_action_is_rejected_for_exact_source_and_correct_role():
    items = sources()
    source = items["S1"]
    trace = {}
    data = {"outcome": "answer", "claims": [
        {"ref": model_ref("S1", source), "actor": "архитектор", "action": "направляет", "text": source.text}
    ]}
    result = assemble(QUESTION, data, items, [DEPARTMENT], False, trace, list(items.values()))

    assert result.outcome == Outcome.NO_BASIS
    assert trace["dropped"] == ["action_missing"]
