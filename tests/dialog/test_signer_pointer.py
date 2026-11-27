"""A signer question whose only direct-looking point is a reference to another document."""

from reglament_agent.answer import Outcome, SourceRef
from reglament_agent.dialog.grounding import Anchor, assemble, model_ref, vocabulary

Q = "Кто подписывает договор с арендодателем склада?"


def _anchor(point, text, rank):
    return Anchor(SourceRef("synthetic", "Регламент аренды", "Отдел аренды", 1, point, sentence=0), text, "", rank)


def _rows():
    return [
        _anchor("4.1", "Договор с арендодателем склада оформляется согласно Регламенту подбора арендодателей.", 5),
        _anchor("9.2", "Подписание акта приёмки склада выполняет Кладовщик.", 0),
    ]


def test_pointer_point_reaches_the_model_and_no_signer_is_invented():
    rows = _rows()
    items = vocabulary(Q, rows)
    assert [a.ref.point for a in items.values()] == ["4.1"]
    key, a = next(iter(items.items()))
    data = {"outcome": "answer", "claims": [{"ref": model_ref(key, a), "actor": "", "action": "", "text": a.text}]}
    answer = assemble(Q, data, items, ["Отдел аренды"], False, {}, rows)
    assert answer.outcome is Outcome.EXTERNAL_DOC
    assert answer.statements[0].sources == [a.ref]
    assert "подписант не указан" in answer.caveat


def test_non_signer_question_gets_no_signer_remark():
    q = "Где описан порядок подбора арендодателей склада?"
    rows = _rows()[:1]
    a = rows[0]
    items = {"S1": a}
    data = {"outcome": "answer", "claims": [{"ref": model_ref("S1", a), "actor": "", "action": "", "text": a.text}]}
    answer = assemble(q, data, items, ["Отдел аренды"], False, {}, rows)
    assert answer.outcome is Outcome.ANSWER
    assert "подписант" not in (answer.caveat or "")
