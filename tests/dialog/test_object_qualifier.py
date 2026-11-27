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


def test_origin_nearby_filters_only_clear_workflow_stages_before_the_link_limit():
    question = "Кто оформляет акт от поставщика?"
    stages = [
        _anchor("1.1", "Бухгалтер оформляет акт на пресейл."),
        _anchor("1.2", "Координатор оформляет акт на пресейл в портале."),
    ]
    object_alternatives = [
        _anchor("2.1", ON_SUPPLIER, "Отдел учёта"),
        _anchor("2.2", ON_RECEIPT),
        _anchor("2.3", "Бухгалтер оформляет акт на пресейл-специалиста."),
    ]
    text, refs = relevance.nearby_evidence(question, [*stages, *object_alternatives])
    assert refs == [anchor.ref for anchor in object_alternatives]
    assert "(Отдел учёта)" in text


def test_origin_nearby_keeps_a_sentence_with_both_a_stage_and_an_object():
    mixed = _anchor("1.1", "Бухгалтер оформляет акт на пресейл и акт на поставщика.")
    _, refs = relevance.nearby_evidence(Q, [mixed])
    assert refs == [mixed.ref]


def test_stage_target_stays_available_when_the_request_does_not_name_an_origin():
    stage = _anchor("1.1", "Бухгалтер оформляет акт на пресейл.")
    _, refs = relevance.nearby_evidence("Кто оформляет акт на приёмку?", [stage])
    assert refs == [stage.ref]


def test_nearby_links_keep_versions_and_first_evidence_with_a_three_point_limit():
    rows = [_anchor(point, ON_RECEIPT) for point in ("1.1", "2.2", "3.3", "4.4")]
    rows[1] = Anchor(SourceRef("other", "Другой регламент", "Отдел учёта", 7, "2.2", 2, "Действие"),
                     ON_SUPPLIER, "", 1)
    text, refs = relevance.nearby_evidence(Q, [rows[0], *rows])
    assert refs == [row.ref for row in rows[:3]]
    assert len(text.splitlines()) == 3 and "п. 4.4" not in text
    assert relevance.nearby_evidence(Q, [_anchor("1.1", FROM_SUPPLIER)]) == ("", [])


def test_saved_answer_without_nearby_sources_can_still_be_read():
    from reglament_agent.dialog.state import answers

    old = {"outcome": "no_basis", "summary": "В регламентах ответа нет."}
    assert answers.validate_python(old).nearby_sources == []


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
    assert not reply.statements and reply.ticket
    assert [(ref.doc_id, ref.version_no, ref.point, ref.sentence) for ref in reply.nearby_sources] == [
        ("ТД", 1, "1.1", 0), ("ДБРЦТ", 1, "2.2", 0),
    ]
    restored = asyncio.run(route.answer(user, "Кто создаёт акт от поставщика в ТД?", _status))
    assert restored.nearby_sources == reply.nearby_sources and restored.ticket == reply.ticket


VERBS = ["создаёт", "оформляет", "формирует", "подаёт", "готовит", "заводит"]
OTHER_ACT = "Бухгалтер оформляет согласование спецификации с поставщиком."


def test_object_noun_is_required_whatever_the_verb():
    near = "Менеджер размещает акт вместе с оценкой Поставщика."  # both words, but not «акт от поставщика»
    rows = [_anchor("7.1", OTHER_ACT), _anchor("7.2", FROM_SUPPLIER, rank=1), _anchor("7.3", near, rank=2)]
    for verb in VERBS:
        picked = [a.ref.point for a in vocabulary(f"Кто {verb} акт от поставщика?", rows).values()]
        assert picked == ["7.2"], verb


def test_department_clarification_does_not_depend_on_the_verb(isolated):
    from reglament_agent.dialog import state

    _, dsn = isolated
    points = [_hit("ТД", "1.1", ON_SUPPLIER), _hit("ДБРЦТ", "2.2", "Менеджер закупок формирует акт на поставщика."),
              _hit("ДБРЦТ", "2.3", OTHER_ACT)]

    def search(conn, question, vector, *, department=None, **kw):
        return Candidates([p for p in points if department in (None, p.department)], [])

    for n, verb in enumerate(VERBS):
        route = Dialog(dsn, Model(), search=search, shortlist=lambda *a, **k: points)
        user = UserContext(f"verb{n}@example.org", "employee", "chat", f"v{n}")
        asked = asyncio.run(route.answer(user, f"Кто {verb} акт от поставщика?", _status))
        assert asked.outcome == Outcome.CLARIFY, verb
        asyncio.run(route.profiles.run(lambda conn: state.update_profile(conn, user.email, department="ТД")))
        user.message_id = f"w{n}"
        chosen = asyncio.run(route.answer(user, f"Кто {verb} акт от поставщика?", _status))
        assert chosen.outcome == Outcome.NO_BASIS and "п. 1.1" in chosen.summary, verb


def test_later_sentence_continues_the_object_of_its_point():
    follow = Anchor(SourceRef("synthetic", "Приёмка", "Отдел склада", 1, "8.1", 1),
                    "Затем документ передаётся директору.", "Бухгалтер создаёт акт на поставщика.", 0)
    assert relevance.qualifier_conflict(Q, follow)
    same = Anchor(SourceRef("synthetic", "Приёмка", "Отдел склада", 1, "8.2", 1),
                  "Затем документ передаётся директору.", "Менеджер создаёт акт от поставщика.", 0)
    assert not relevance.qualifier_conflict(Q, same) and not relevance.lacks_requested_object(Q, same)
