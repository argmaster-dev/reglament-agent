"""Household instruction versus consulting a company process when working from home."""
import asyncio

import pytest

from reglament_agent.answer import Outcome, UserContext
from reglament_agent.dialog.guards import operational_request
from reglament_agent.dialog.route import Dialog
from reglament_agent.storage.retrieve import Candidates
from tests.dialog.test_request_scope import FirstSource
from tests.dialog.test_route import hit, status


@pytest.mark.parametrize('question', [
    'Посоветуйте рецепт для семейного завтрака.',
    'Дай рецепт начинки из сезонных продуктов.',
    'Как приготовить начинку в домашних условиях?',
    'Подскажи, как дома испечь угощение на двоих?',
    'Как постирать шерстяной шарф дома?',
    'Как приготовить завтрак на двоих?',
    'Как правильно запечь овощи в духовке?',
])
def test_explicit_household_instruction(question):
    assert operational_request(question)


@pytest.mark.parametrize('question', [
    'Как приготовить КП дома?',
    'Как правильно приготовить дома ТКП для встречи?',
    'Как подготовить смету, если я работаю из дома?',
    'Как приготовить дома квартальный отчёт?',
    'Как приготовить файл дома для отправки в офис?',
    'Как оформить документы на закупку продуктов для завтрака?',
    'По каким правилам согласовать рецепт с поставщиком кейтеринга?',
    'Как обрабатывать из дома документы с ограничением доступа?',
    'Как приготовить его дома?',
    'А как приготовить дома?',
    'Посоветуйте рецепт согласования договора.',
    'Кто отвечает за питание сотрудников на выездном мероприятии?',
])
def test_workflow_or_unresolved_reference_follows_the_dialogue(question):
    assert not operational_request(question)


@pytest.mark.parametrize(('question','outside'), [
    ('Подскажи рецепт для семейного обеда.', True),
    ('Как приготовить тесто дома?', True),
    ('Как приготовить дома коммерческое предложение?', False),
    ('Какие документы нужны для закупки продуктов для обеда?', False),
])
def test_real_route_and_ticket_effects(isolated, question, outside):
    conn, dsn = isolated
    model = FirstSource()
    route = Dialog(dsn, model, shortlist=lambda *a, **kw: [], search=lambda *a, **kw: Candidates([], []))
    user = UserContext('reader@example.org', 'employee', 'chat', 'm')
    answer = asyncio.run(route.answer(user, question, status))
    assert answer.outcome is (Outcome.OFF_TOPIC if outside else Outcome.NO_BASIS)
    assert bool(answer.ticket) == bool(model.calls) == (not outside)
    assert conn.execute('SELECT count(*) FROM tickets').fetchone() == (int(not outside),)
    assert conn.execute('SELECT count(*) FROM mail_outbox').fetchone() == (int(not outside),)


def test_procurement_scope_choice_after_a_question_about_food(isolated):
    _, dsn = isolated
    first = hit('ТД', 'За подписание договора на закупку продуктов для обеда отвечает Логист.')
    second = hit('ИБ', 'Договор на закупку продуктов для обеда подписывает директор.')

    def applicable(*args, department=None, **kwargs):
        return [p for p in (first, second) if department in (None, p.department)]

    route = Dialog(dsn, FirstSource(), shortlist=applicable,
                   search=lambda *a, **kw: Candidates(applicable(*a, **kw), []))
    user = UserContext('reader@example.org', 'employee', 'chat', 'one')
    opening = asyncio.run(route.answer(user, 'Кто подписывает договор на закупку продуктов для обеда?', status))
    assert opening.outcome is Outcome.CLARIFY
    user.message_id = 'two'
    answer = asyncio.run(route.choose(user, 'ТД', status))
    assert answer.outcome is Outcome.ANSWER and answer.statements[0].text == first.text and not answer.ticket
