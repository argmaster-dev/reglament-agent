"""A project expense is not a payroll or leave rule for an employee."""
import pytest

from reglament_agent.answer import Outcome
from tests.dialog.test_exact_evidence import evidence


@pytest.mark.parametrize(('question', 'unrelated', 'rule'), [
    ('Как в отделе по закону оплачивают сверхурочные часы на проекте?',
     'Работы сотрудника на проекте включают в бюджет; сверхурочные расходы планируют заранее.',
     'Сверхурочные часы оплачиваются сотруднику по утверждённому табелю.'),
    ('Как на проекте по закону оплачивают выходной в командировке?',
     'На проекте оплачивают питание и проезд в командировке. Работу в выходной согласует руководитель.',
     'Работа в выходной день в командировке оплачивается на основании согласованного табеля.'),
    ('Как сотрудник по закону переносит отпуск на время проекта?',
     'На время проекта сотрудник переносит задачи в карточку проекта и назначает замещающего.',
     'Сотрудник переносит ежегодный отпуск по согласованию с руководителем отдела.'),
])
def test_internal_employment_answer_requires_the_benefit_and_procedure(question, unrelated, rule):
    assert evidence(question, [unrelated]).outcome == Outcome.NO_BASIS
    response = evidence(question, [unrelated, rule])
    assert response.outcome == Outcome.ANSWER
    assert [s.text for s in response.statements] == [rule]


def test_leave_duration_cannot_come_from_project_duration():
    question = 'Может ли сотрудник отдела по закону перенести отпуск и какова его продолжительность?'
    transfer = 'Отпуск переносится по согласованию с кадровой службой.'
    duration = 'Продолжительность проекта определяет руководитель.'
    response = evidence(question, [transfer, duration])
    assert response.outcome == Outcome.LOW_CONFIDENCE
    assert [s.text for s in response.statements] == [transfer]


def test_employee_scope_opens_one_ticket_for_an_unregulated_benefit(isolated):
    from tests.dialog.test_cache import Model, ask, route_for, seed, user

    conn, dsn = isolated
    seed(conn, {'td': ('ТД', ['В бюджете проекта учитывают стоимость работы на проекте.'])})
    model = Model()
    route = route_for(conn, dsn, model)
    who = user('employee@example.org', 'hr', 'first')
    question = 'Как в ТД по закону оплачиваются сверхурочные на проекте?'
    first = ask(route, who, question)
    repeated = ask(route, who, question)
    assert first.outcome is Outcome.NO_BASIS and not first.statements and not first.redirect
    assert first.ticket and repeated.ticket == first.ticket
    assert not model.calls


@pytest.mark.parametrize(('question', 'different_object'), [
    ('Как по закону оплачивают выходной в командировке на проекте?',
     'В командировке в выходной день расходы на проживание оплачиваются по чекам.'),
    ('Как сотрудник отдела по закону переносит отпуск?',
     'Руководитель переносит работы на период отпуска сотрудника.'),
    ('Какова по закону продолжительность отпуска сотрудника отдела?',
     'Продолжительность проекта во время отпуска сотрудника определяется графиком.'),
])
def test_a_nearby_employment_term_does_not_change_the_object_of_the_rule(question, different_object):
    assert evidence(question, [different_object]).outcome is Outcome.NO_BASIS


def test_payroll_and_invoices_in_the_same_working_period_are_separate():
    question = 'Как в отделе по закону оплачивают сверхурочные часы на проекте?'
    invoice = 'Во время сверхурочной работы счета поставщика оплачиваются по графику.'
    payroll = 'Сверхурочная работа оплачивается сотруднику по табелю отдельно от расходов на проезд.'
    assert evidence(question, [invoice]).outcome is Outcome.NO_BASIS
    result = evidence(question, [invoice, payroll])
    assert result.outcome is Outcome.ANSWER
    assert [statement.text for statement in result.statements] == [payroll]
