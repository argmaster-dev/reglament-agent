"""A compound question keeps the sentences for every part; a missing part is reported."""

import pytest

from reglament_agent.answer import Outcome, SourceRef
from reglament_agent.dialog.grounding import Anchor, assemble, model_ref, vocabulary
from reglament_agent.dialog.relevance import question_parts

SENTENCES = ["Диспетчер создаёт карточку осмотра.", "Система автоматически публикует расписание осмотра."]


def _rows():
    return [Anchor(SourceRef("synthetic", "Осмотры", "Отдел", 1, "2.3", i), t, SENTENCES[0] if i else "", 0)
            for i, t in enumerate(SENTENCES)]


def _picked(question):
    return sorted(a.ref.sentence for a in vocabulary(question, _rows()).values())


@pytest.mark.parametrize("question", [
    "Кто создаёт карточку осмотра и что система публикует автоматически?",
    "Что система публикует автоматически и кто создаёт карточку осмотра?",
    "Кто создаёт карточку осмотра, а также что система публикует автоматически?",
    "Кто создаёт карточку осмотра, что система публикует автоматически?",
])
def test_both_parts_keep_their_sentences(question):
    assert _picked(question) == [0, 1]


def test_simple_executor_question_keeps_role_check():
    assert _picked("Кто создаёт карточку осмотра?") == [0]


def test_parts_split_only_on_interrogative_boundaries():
    assert len(question_parts("Кто готовит отчёт и протокол осмотра?")) == 1
    assert len(question_parts("Кто готовит отчёт и что делает система?")) == 2


def test_part_without_basis_is_reported():
    rows = _rows()[:1]
    items = {"S1": rows[0]}
    data = {"outcome": "answer", "claims": [{"ref": model_ref("S1", rows[0]), "actor": "диспетчер",
                                             "action": "создаёт", "text": rows[0].text}]}
    q = "Кто создаёт карточку осмотра и какой штраф начисляется за просрочку?"
    answer = assemble(q, data, items, ["Отдел"], False, {}, rows)
    assert answer.outcome is Outcome.LOW_CONFIDENCE
    assert "штраф" in answer.caveat


@pytest.mark.parametrize("proposed", ["answer", "no_basis", "low_confidence"])
def test_partial_evidence_keeps_only_the_supported_part(proposed):
    question = "Кто создаёт карточку осмотра и какой штраф начисляется за просрочку?"
    row = _rows()[0]
    other = Anchor(SourceRef("d", "Справка", "Отдел", 1, "3.1", 0),
                   "Карточку осмотра архивируют по окончании года.", "", 1)
    items = {"S1": row, "S2": other}
    claims = [{"ref": model_ref(k, a), "text": a.text} for k, a in items.items()]
    answer = assemble(question, {"outcome": proposed, "claims": claims}, items, ["Отдел"], False, {})
    assert answer.outcome is Outcome.LOW_CONFIDENCE
    assert [s.text for s in answer.statements] == [row.text]
    assert "штраф" in answer.caveat


@pytest.mark.parametrize(("q", "text"), [
    ("Кто создаёт карточку осмотра и какой штраф начисляется за просрочку?",
     "Карточку осмотра архивируют по окончании года."),
    ("Кто утверждает график проверок и какой штраф начисляется за просрочку?",
     "Диспетчер публикует график проверок."),
])
def test_neighbour_without_a_supported_part_cannot_be_a_partial_answer(q, text):
    a = Anchor(SourceRef("d", "Справка", "Отдел", 1, "3.1", 0), text, "", 1)
    data = {"outcome": "low_confidence", "claims": [{"ref": model_ref("S1", a), "text": a.text}]}
    result = assemble(q, data, {"S1": a}, ["Отдел"], False, {})
    assert result.outcome is Outcome.NO_BASIS and not result.statements
