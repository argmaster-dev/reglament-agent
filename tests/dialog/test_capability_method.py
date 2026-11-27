import asyncio

import pytest

from reglament_agent.answer import Answer, Outcome, Statement, UserContext
from reglament_agent.dialog import capability, grounding, similar, state
from reglament_agent.dialog.route import Dialog
from reglament_agent.storage.retrieve import Candidates
from tests.dialog.test_request_scope import FirstSource
from tests.dialog.test_route import hit, status


QUESTION = "Заказчику нужна наладка оборудования. Как проверить, сможем ли мы это выполнить?"
METHOD = "Инженер по матрице навыков оценивает возможность выполнения задачи своими силами."


def check(dsn, question, preview_points, primary=None, profile=None):
    model = FirstSource()
    searches = []

    def shortlist(conn, text, *, department, stage):
        searches.append((text, department, stage))
        return [p for p in preview_points if department in (None, p.department)]

    route = Dialog(dsn, model, search=lambda *a, **kw: Candidates(primary or [], []), shortlist=shortlist)
    user = UserContext("reader@example.org", "employee", "chat", "1")
    if profile:
        asyncio.run(route.profiles.run(lambda conn: state.update_profile(conn, user.email, department=profile)))
    answer = asyncio.run(route.answer(user, question, status))
    return answer, searches


def test_general_capability_method_answers_part_of_the_question_and_keeps_one_ticket(isolated):
    conn, dsn = isolated
    answer, _ = check(dsn, QUESTION, [hit("Сервис", METHOD)])
    assert answer.outcome == Outcome.LOW_CONFIDENCE and answer.ticket
    assert [(s.text, s.sources[0].department) for s in answer.statements] == [(METHOD, "Сервис")]
    assert "конкретн" in answer.caveat and "право" in answer.caveat
    retry, _ = check(dsn, QUESTION, [hit("Сервис", METHOD)])
    assert retry == answer
    assert conn.execute("SELECT reason FROM tickets").fetchall() == [("low_confidence",)]


@pytest.mark.parametrize("text", [
    "Менеджер по матрице навыков оценивает возможность выполнения задачи силами поставщика.",
    "Бухгалтер по матрице навыков не определяет возможность выполнения задачи своими силами.",
    "Подрядчик передаёт перечень навыков специалистов и копию разрешения.",
    "Инженер по матрице навыков оценивает должность. Координатор проверяет возможность выполнения задачи.",
])
def test_unrelated_or_negative_sources_do_not_establish_a_method(isolated, text):
    _, dsn = isolated
    answer, _ = check(dsn, QUESTION, [hit("Сервис", text)])
    assert answer.outcome == Outcome.NO_BASIS and answer.ticket and not answer.statements


def test_specific_authorisation_question_cannot_be_answered_from_skills(isolated):
    _, dsn = isolated
    answer, _ = check(dsn, "Заказчику нужен ремонт. Как узнать, есть ли у нас лицензия на эти работы?",
                      [hit("Сервис", METHOD)])
    assert answer.outcome in {Outcome.OFF_TOPIC, Outcome.NO_BASIS} and not answer.statements


@pytest.mark.parametrize(("text", "expected"), [
    (METHOD, Outcome.LOW_CONFIDENCE),
    ("Отдел сервиса выполняет наладку оборудования для заказчиков по согласованной заявке.", Outcome.ANSWER),
])
def test_already_verified_specific_evidence_is_preserved_and_method_evidence_is_bounded(
    isolated, monkeypatch, text, expected
):
    _, dsn = isolated
    point = hit("Сервис", text)
    statement = Statement(text, [point.source_ref(sentence=0)])
    monkeypatch.setattr(grounding, "assemble", lambda *args: Answer(Outcome.ANSWER, statements=[statement]))
    answer, searches = check(dsn, QUESTION, [point], primary=[point])
    assert answer.outcome == expected and answer.statements == [statement]
    assert bool(answer.ticket) == (expected == Outcome.LOW_CONFIDENCE)
    assert all(query != capability.SEARCH_QUERY for query, _, _ in searches)


def test_a_capability_procedure_of_another_department_is_outside_the_selected_scope(isolated):
    _, dsn = isolated
    answer, searches = check(dsn, QUESTION, [hit("Разработка", METHOD)], profile="Филиал")
    assert answer.outcome == Outcome.NO_BASIS and answer.ticket and not answer.statements
    assert (capability.SEARCH_QUERY, "Филиал", None) in searches


def test_distinct_project_procedures_need_a_choice_of_scope(isolated):
    _, dsn = isolated
    answer, _ = check(dsn, QUESTION, [hit("Сервис", METHOD), hit("Филиал", METHOD)])
    assert answer.outcome in {Outcome.CLARIFY, Outcome.NO_BASIS} and not answer.statements


def test_asking_to_approve_an_unrelated_action_does_not_trigger_a_skills_lookup():
    assert not capability.requested("Клиенту нужна отсрочка. Как проверить, можем ли мы это оплатить?")
    assert not capability.requested("Как узнать, кто ведёт проект и что мы это можем?")
    assert capability.requested("Клиенту нужен новый сервис: как понять, что мы это можем?")


@pytest.mark.parametrize("question", [
    QUESTION, "Поставщик затягивает отгрузку, что делать?", "Надо купить сканеры, как оформить?",
])
def test_similar_cache_cannot_resolve_a_party_asset_or_project_specific_condition(isolated, monkeypatch, question):
    _, dsn = isolated
    def unsafe_reuse(*args):
        pytest.fail("A similar question must not establish eligibility or the binding procedure")
    monkeypatch.setattr(similar, "find", unsafe_reuse)
    route = Dialog(dsn, FirstSource(), similar_cache=True, search=lambda *a, **kw: Candidates([], []),
                   shortlist=lambda *a, **kw: [])
    answer = asyncio.run(route.answer(UserContext("reader@example.org", "employee", "chat", "1"), question, status))
    assert answer.outcome == Outcome.NO_BASIS and answer.ticket
