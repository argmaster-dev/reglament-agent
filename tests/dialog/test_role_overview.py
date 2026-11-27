"""An introduction does not tell the agent whose regulations govern another role."""
import asyncio
from dataclasses import replace

import pytest

from reglament_agent.answer import Outcome, UserContext
from reglament_agent.dialog.route import Dialog
from reglament_agent.storage.retrieve import Candidates
from tests.dialog.test_process_intent import point
from tests.dialog.test_request_scope import FirstSource
from tests.dialog.test_route import status


@pytest.mark.parametrize(('question', 'should_ask'), [
    ('Я инженер. Что делает архитектор?', True),
    ('За что отвечает логист?', True),
    ('Какие обязанности у архитектора?', True),
    ('Что делает архитектор в отделе закупок?', False),
    ('Что делает архитектор при проверке сметы?', False),
    ('Кто отвечает за проверку сметы?', False),
])
def test_role_overview_requires_direction_only_when_unspecified(isolated, question, should_ask):
    conn, dsn = isolated
    departments = ['Отдел закупок', 'Техническая служба']
    for index, department in enumerate(departments):
        conn.execute("INSERT INTO documents(doc_id,title,department,stages) VALUES (%s,'Порядок',%s,'{project}')",
                     (f'doc-{index}', department))
    source = replace(point('2.1', 'Проверка сметы', 'Архитектор проверяет смету.'), department=departments[0])
    model = FirstSource()
    # The best retrieval result belongs to one direction. It cannot establish the employee's intended direction.
    route = Dialog(dsn, model, search=lambda *a, **kw: Candidates([source], []), shortlist=lambda *a, **kw: [source])
    user = UserContext('employee@example.org', 'employee', 'duties', '1')
    answer = asyncio.run(route.answer(user, question, status))
    if should_ask:
        assert answer.clarification and answer.clarification.dimension == 'department'
        assert set(answer.clarification.options) == set(departments) and answer.clarification.attempt == 1
        assert not model.calls and not answer.ticket
        user.message_id = '2'
        after = asyncio.run(route.choose(user, departments[0], status))
        assert after.scope.department == departments[0] and not after.clarification and model.calls
    else:
        assert not answer.clarification and model.calls
