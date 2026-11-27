"""A deadline question without a department asks for it when departments give different values, whatever the wording."""

import asyncio
from types import SimpleNamespace

import pytest

from reglament_agent.answer import Outcome, UserContext
from reglament_agent.dialog import state
from reglament_agent.dialog.route import Dialog
from reglament_agent.storage.retrieve import Candidates
from reglament_agent.storage.sources import Hit

LEAVE_A = ["Работник оформляет заявление на отпуск в кадровой системе.",
           "Заявление на отпуск подаётся не позднее чем за 14 дней до начала отпуска."]
LEAVE_B = ["Сотрудник оформляет заявление на отпуск у руководителя.",
           "Важно: заявление на отпуск подаётся в течение 3 рабочих дней после согласования графика."]
REPORT_A = ["Подотчётное лицо составляет авансовый отчёт.", "Авансовый отчёт сдаётся в течение 5 рабочих дней после возвращения."]
REPORT_B = ["Подотчётное лицо составляет авансовый отчёт.", "Авансовый отчёт сдаётся в течение 10 календарных дней после поездки."]
ONLY_A = ["Кладовщик готовит акт сверки склада.", "Акт сверки склада подписывается в течение 7 дней после инвентаризации."]
OTHER_B = ["Водитель оформляет путевой лист.", "Путевой лист закрывается в течение 2 дней после рейса."]

ADVANCE_B = ["Сотрудник оформляет заявление на отпуск у руководителя.",
             "Заявление на отпуск подаётся не менее чем за 3 рабочих дня до начала отпуска."]
LEAVE_Q = [
    "Какой срок подачи заявления на отпуск?",
    "В течение какого времени нужно подать заявление на отпуск?",
    "Сколько дней есть на подачу заявления на отпуск?",
    "Когда нужно подать заявление на отпуск?",
    "Не позднее какого срока подаётся заявление на отпуск?",
]
REPORT_Q = ["Какой срок сдачи авансового отчёта?", "Сколько дней дается на сдачу авансового отчёта?",
            "В течение какого времени сдаётся авансовый отчёт?"]


def hit(department, number, sentences):
    return Hit(hash((department, number)) % 10**6, department.lower(), 1, number, " ".join(sentences), 0.04,
               "Правила " + department, department, 1, sentences, [], 0.8, True)


class Model:
    def __init__(self):
        self.calls = []

    async def embed(self, texts, **kw):
        return [[0.01] * 1024]

    async def chat_json(self, messages, schema, **kw):
        self.calls.append(messages)
        return SimpleNamespace(data={"outcome": "no_basis", "claims": []})


async def status(_):
    pass


def route_for(dsn, points):
    def pick(department):
        return [p for p in points if department in (None, p.department)]

    model = Model()
    route = Dialog(
        dsn, model,
        search=lambda conn, q, v, *, department=None, **kw: Candidates(pick(department), []),
        shortlist=lambda conn, q, *, department=None, **kw: pick(department),
    )
    return route, model


def ask(route, question, n, profile=None):
    email = f"deadline{n}@example.org"
    if profile:
        asyncio.run(route.profiles.run(lambda conn: state.update_profile(conn, email, department=profile)))
    return asyncio.run(route.answer(UserContext(email, "employee", "chat", str(n)), question, status))


ADVANCE = [hit("Отдел А", "3.1", LEAVE_A), hit("Отдел Б", "5.2", ADVANCE_B)]
LEAVE = [hit("Отдел А", "3.1", LEAVE_A), hit("Отдел Б", "5.2", LEAVE_B)]
REPORT = [hit("Бухгалтерия", "2.4", REPORT_A), hit("Филиал", "7.1", REPORT_B)]


@pytest.mark.parametrize("n,question", list(enumerate(LEAVE_Q)))
def test_no_department_asks_for_it_for_any_phrasing(isolated, n, question):
    _, dsn = isolated
    route, model = route_for(dsn, LEAVE)
    reply = ask(route, question, n)
    assert reply.outcome == Outcome.CLARIFY and model.calls == []
    assert set(reply.clarification.options) >= {"Отдел А", "Отдел Б"}


def test_advance_wording_asks_when_both_departments_give_an_advance(isolated):
    _, dsn = isolated
    route, model = route_for(dsn, ADVANCE)
    reply = ask(route, "За сколько подавать заявление на отпуск?", 0)
    assert reply.outcome == Outcome.CLARIFY and model.calls == []


@pytest.mark.parametrize("n,question", list(enumerate(REPORT_Q)))
def test_neutral_process_asks_for_department(isolated, n, question):
    _, dsn = isolated
    route, model = route_for(dsn, REPORT)
    reply = ask(route, question, n)
    assert reply.outcome == Outcome.CLARIFY and model.calls == []


@pytest.mark.parametrize("n,question", list(enumerate(LEAVE_Q[:3])))
def test_profile_goes_straight_to_its_own_department(isolated, n, question):
    _, dsn = isolated
    route, model = route_for(dsn, LEAVE)
    reply = ask(route, question, n, profile="Отдел Б")
    assert reply.outcome != Outcome.CLARIFY and len(model.calls) == 1
    assert "3 рабочих дней" in model.calls[0][1]["content"] and "14 дней" not in model.calls[0][1]["content"]


def test_process_present_in_one_department_needs_no_clarification(isolated):
    _, dsn = isolated
    route, model = route_for(dsn, [hit("Склад", "4.2", ONLY_A), hit("Транспорт", "1.3", OTHER_B)])
    reply = ask(route, "Какой срок подписания акта сверки склада?", 0)
    assert reply.outcome != Outcome.CLARIFY and len(model.calls) == 1


def test_identical_deadline_in_both_departments_needs_no_clarification(isolated):
    _, dsn = isolated
    same = [hit("Бухгалтерия", "2.4", REPORT_A), hit("Филиал", "7.1", REPORT_A)]
    route, model = route_for(dsn, same)
    assert ask(route, "Какой срок сдачи авансового отчёта?", 0).outcome != Outcome.CLARIFY


def test_note_without_the_subject_takes_it_from_its_point(isolated):
    _, dsn = isolated
    note_b = ["После согласования графика работник обращается к руководителю.",
              "Для этого сотрудник оформляет заявление на отпуск.", "Важно: не позднее чем за 5 рабочих дней."]
    route, model = route_for(dsn, [hit("Отдел А", "3.1", LEAVE_A), hit("Отдел Б", "5.2", note_b)])
    reply = ask(route, "Какой срок подачи заявления на отпуск?", 0)
    assert reply.outcome == Outcome.CLARIFY and model.calls == []
