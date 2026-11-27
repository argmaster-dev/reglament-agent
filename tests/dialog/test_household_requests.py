"""A personal recommendation and a question about the organization's work can share nouns."""
import asyncio

import pytest

from reglament_agent.answer import Outcome, UserContext
from reglament_agent.dialog.route import Dialog
from reglament_agent.storage.retrieve import Candidates
from tests.dialog.test_request_scope import FirstSource
from tests.dialog.test_route import hit, status


@pytest.mark.parametrize(('question', 'personal'), [
    ('Как выбрать подходящий корм моему щенку?', True),
    ('Посоветуйте поводок для взрослой собаки.', True),
    ('Порекомендуй лакомство для домашнего питомца.', True),
    ('Посоветуй бюджетный ошейник для питомца.', True),
    ('Помоги выбрать компактную палатку для рыбалки.', True),
    ('Как выбрать фотоаппарат для семейной поездки?', True),
    ('Посоветуйте проектор для семейного вечера.', True),
    ('Как выбрать дни отпуска для семейной поездки?', False),
    ('Как выбрать чемодан для семейного отпуска?', True),
    ('Помогите выбрать для себя.', False),
    ('Помогите выбрать вариант.', False),
    ('Посоветуйте палатку для закупки снаряжения на проект.', False),
    ('Подберите проектор для семейного мероприятия по условиям договора.', False),
    ('Кто согласует закупку палатки для похода сотрудников?', False),
    ('Какие документы нужны для списания рюкзака?', False),
    ('Какой курс евро закладывать в проектную смету?', False),
    ('Помоги подобрать курс доллара для расчёта стоимости услуг.', False),
    ('Какой сейчас курс юаня?', True),
    ('Помоги подобрать комплект документов для расчёта бюджета проекта.', False),
    ('Помоги подобрать корм для служебной собаки по условиям закупки.', False),
    ('Кто по инструкции выбирает поводок для собаки охраны?', False),
    ('Помоги выбрать поставщика котлов для закупки оборудования.', False),
])
def test_personal_advice_versus_an_uncovered_workflow(isolated, question, personal):
    conn, dsn = isolated
    model = FirstSource()
    searches = []

    def search(*args, **kwargs):
        searches.append(True)
        return Candidates([], [])

    route = Dialog(dsn, model, shortlist=lambda *a, **kw: [], search=search)
    user = UserContext('reader@example.org', 'employee', 'thread', 'message')
    result = asyncio.run(route.answer(user, question, status))
    if personal:
        assert result.outcome is Outcome.OFF_TOPIC and not result.ticket and not result.statements
        assert 'руководител' in result.summary and not searches and not model.calls
    else:
        assert result.outcome is Outcome.NO_BASIS and result.ticket and searches and model.calls
    assert conn.execute('SELECT count(*) FROM tickets').fetchone() == (int(not personal),)
    assert conn.execute('SELECT count(*) FROM mail_outbox').fetchone() == (int(not personal),)


def test_documented_budget_question_uses_the_source(isolated):
    _, dsn = isolated
    source = hit('Сервис', 'Для расчёта бюджета проекта курс евро фиксируют в день утверждения сметы.')
    model = FirstSource()
    route = Dialog(dsn, model, shortlist=lambda *a, **kw: [], search=lambda *a, **kw: Candidates([source], []))
    answer = asyncio.run(route.answer(UserContext('u@example.org', 'employee', 't', '1'),
                                      'Какой курс евро закладывать в смету проекта?', status))
    assert answer.outcome is Outcome.ANSWER and answer.statements[0].text == source.text
    assert answer.statements[0].sources[0].point == '4.1' and not answer.ticket


def test_procurement_question_continues_after_department_choice(isolated):
    _, dsn = isolated
    td = hit('ТД', 'За подписание договора на закупку палатки отвечает Логист.')
    ib = hit('ИБ', 'Договор на закупку палатки подписывает директор.')

    def found(*args, department=None, **kwargs):
        return [point for point in (td, ib) if department in (None, point.department)]

    dialog = Dialog(dsn, FirstSource(), shortlist=found,
                    search=lambda *a, **kw: Candidates(found(*a, **kw), []))
    user = UserContext('employee@example.org', 'employee', 'thread', '1')
    question = 'Кто подписывает договор на закупку, если нужно выбрать палатку для похода сотрудников?'
    first = asyncio.run(dialog.answer(user, question, status))
    assert first.outcome is Outcome.CLARIFY and 'ТД' in first.clarification.options
    user.message_id = '2'
    second = asyncio.run(dialog.choose(user, 'ТД', status))
    assert second.outcome is Outcome.ANSWER and second.statements[0].text == td.text
    assert not second.ticket
