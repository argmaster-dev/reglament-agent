"""Only the scope of the employee's question directs a legal topic to internal regulations."""
import asyncio

import pytest

from reglament_agent.answer import Outcome, UserContext
from reglament_agent.dialog.route import Dialog
from reglament_agent.dialog.state import set_department
from reglament_agent.storage.retrieve import Candidates
from tests.dialog.test_request_scope import FirstSource
from tests.dialog.test_route import status


@pytest.mark.parametrize(('question', 'internal'), [
    ('Как по закону в Дирекции логистики компенсируют ночное дежурство?', True),
    ('Как по ТК дежурному ДЛ предоставляют перерыв между сменами?', True),
    ('Как по закону на этапе пресейла компенсируют проезд на встречу?', True),
    ('По ТК нужен перерыв. Как его учитывают в Дирекции логистики?', True),
    ('Как по ТК рассчитывают оплату ночной смены?', False),
    ('Я работаю в Дирекции логистики. Как по ТК рассчитывают оплату ночной смены?', False),
    ('Пресейл завершён. Кто по закону вправе требовать алименты?', False),
])
def test_legal_request_with_explicit_or_profile_only_scope(isolated, question, internal):
    conn, dsn = isolated
    department = 'Дирекция логистики'
    conn.execute("INSERT INTO documents(doc_id,title,department,stages) VALUES ('manual','Порядок',%s,'{project}')",
                 (department,))
    model = FirstSource()
    search_calls = []

    def search(*args, **kwargs):
        search_calls.append(kwargs)
        return Candidates([], [])

    route = Dialog(dsn, model, shortlist=lambda *a, **kw: [], search=search)
    user = UserContext('check@example.org', 'employee', 'session', 'message')
    set_department(conn, user.email, department)
    conn.commit()
    ans = asyncio.run(route.answer(user, question, status))
    if internal:
        assert search_calls and model.calls
        assert ans.redirect is None and ans.outcome is Outcome.NO_BASIS and ans.ticket
    else:
        assert not search_calls and not model.calls
        assert ans.redirect == 'legal' and ans.outcome is Outcome.OFF_TOPIC and not ans.ticket
    assert conn.execute('SELECT count(*) FROM mail_outbox').fetchone() == (int(internal),)
