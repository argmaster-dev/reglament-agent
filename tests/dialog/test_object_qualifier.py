"""«Акт от X», «акт на X» и «акт на Y» are different objects: the preposition and head must agree."""

import asyncio
from types import SimpleNamespace

from reglament_agent.answer import Outcome, SourceRef, UserContext
from reglament_agent.dialog import relevance
from reglament_agent.dialog.grounding import Anchor, assemble, model_ref, vocabulary
from reglament_agent.dialog.route import Dialog
from reglament_agent.storage.retrieve import Candidates
from reglament_agent.storage.sources import Hit

Q = "Кто создаёт акт от поставщика?"
ON_RECEIPT = "Кладовщик создаёт акт на приёмку товара."
ON_SUPPLIER = "Бухгалтер создаёт акт на поставщика в учётной системе."
FROM_SUPPLIER = "Менеджер закупок создаёт акт от поставщика в системе."
BARE = "Кладовщик создаёт акт и передаёт его бухгалтеру."


def _anchor(point, text, department="Отдел склада", rank=0):
    return Anchor(SourceRef("synthetic", "Приёмка", department, 1, point, sentence=0), text, "", rank)


def test_different_preposition_or_head_conflicts():
    assert relevance.qualifier_conflict(Q, _anchor("1.1", ON_RECEIPT))
    assert relevance.qualifier_conflict(Q, _anchor("1.2", ON_SUPPLIER))


def test_same_qualifier_and_unqualified_noun_do_not_conflict():
    assert not relevance.qualifier_conflict(Q, _anchor("1.3", FROM_SUPPLIER))
    assert not relevance.qualifier_conflict(Q, _anchor("1.4", BARE))
    assert not relevance.qualifier_conflict("Кто создаёт акт на приёмку?", _anchor("1.1", ON_RECEIPT))
    assert not relevance.qualifier_conflict("Кто создаёт акт на субпоставщика?", _anchor("1.5", "Бухгалтер создаёт акт на поставщика."))


def test_non_executor_question_is_not_filtered():
    assert not relevance.qualifier_conflict("Где описан акт от поставщика?", _anchor("1.1", ON_RECEIPT))


def test_vocabulary_keeps_only_matching_object():
    rows = [_anchor("1.1", ON_RECEIPT), _anchor("1.2", ON_SUPPLIER), _anchor("1.3", FROM_SUPPLIER, rank=1)]
    assert [a.ref.point for a in vocabulary(Q, rows).values()] == ["1.3"]
    assert [a.ref.point for a in vocabulary("Кто создаёт акт на приёмку?", rows).values()] == ["1.1"]


def test_assemble_drops_claim_on_other_object():
    a = _anchor("1.1", ON_RECEIPT)
    data = {"outcome": "answer", "claims": [
        {"ref": model_ref("S1", a), "actor": "кладовщик", "action": "создаёт", "text": a.text}]}
    trace = {}
    answer = assemble(Q, data, {"S1": a}, ["Отдел склада"], False, trace, [a])
    assert answer.outcome is Outcome.NO_BASIS
    assert "object_qualifier_mismatch" in trace["dropped"]


def test_near_basis_lists_other_object_with_point_and_department():
    rows = [_anchor("1.1", ON_RECEIPT), _anchor("2.2", ON_SUPPLIER, "Отдел учёта"), _anchor("3.1", BARE)]
    text = relevance.near_basis(Q, rows)
    assert "п. 2.2" in text and "Отдел учёта" in text and "акт на поставщика" in text
    assert "п. 3.1" not in text


def _hit(department, number, text):
    return Hit(1, department, 1, number, text, 0.04, "Приёмка", department, 1, [text], [], 0.8, True)


class Model:
    def __init__(self):
        self.calls = []

    async def embed(self, texts, **kw):
        return [[0.01] * 1024]

    async def chat_json(self, messages, schema, **kw):
        self.calls.append(messages)
        return SimpleNamespace(data={"outcome": "no_basis", "claims": []})


async def _status(_):
    pass


def test_profile_department_without_basis_points_to_other_department(isolated):
    _, dsn = isolated
    points = [_hit("ТД", "1.1", ON_RECEIPT), _hit("ДБРЦТ", "2.2", ON_SUPPLIER)]

    def search(conn, question, vector, *, department=None, **kw):
        return Candidates([p for p in points if department in (None, p.department)], [])

    route = Dialog(dsn, Model(), search=search, shortlist=lambda *a, **k: points)
    user = UserContext("qual@example.org", "employee", "chat", "q1")
    reply = asyncio.run(route.answer(user, "Кто создаёт акт от поставщика в ТД?", _status))
    assert reply.outcome == Outcome.NO_BASIS
    assert "п. 2.2" in reply.summary and "ДБРЦТ" in reply.summary
