"""An unmistakable household action may be the question even without a place or a named dish."""
import asyncio

import pytest

from reglament_agent.answer import Outcome, UserContext
from reglament_agent.dialog.guards import operational_request
from reglament_agent.dialog.route import Dialog
from reglament_agent.storage.retrieve import Candidates
from tests.dialog.test_request_scope import FirstSource
from tests.dialog.test_route import status


@pytest.mark.parametrize('message', [
    'Что можно испечь из оставшихся продуктов?',
    'Что бы мне испечь?',
    'При какой температуре выпекать заготовку?',
    'Сколько минут запекать порцию?',
    'Помоги запечь заготовку без масла.',
    'Испеките что-нибудь из этих продуктов.',
    'Подскажите, чем постирать деликатную вещь?',
    'Постирай хлопковую вещь вручную.',
    'Пропылесось коврик, пожалуйста.',
    'Чем лучше приготовить ужин для семьи?',
])
def test_explicit_household_action_accepts_different_request_forms(message):
    assert operational_request(message)


@pytest.mark.parametrize('message', [
    'Как сварить металлическую конструкцию дома?',
    'Как сварить два элемента на площадке по рабочей инструкции?',
    'Как запечь покрытие по технологической карте?',
    'Что приготовить к встрече с заказчиком?',
    'Приготовь коммерческое предложение для нового клиента.',
    'Можно ли его приготовить дома?',
    'Как готовить заготовку?',
    'Можно ли её запечь?',
    'Что из этого можно запечь?',
    'Что означает метка «испечь» в интерфейсе?',
    'Подскажите, как готовить акт передачи из дома?',
    'Кто должен стирать форму дежурных по правилам охраны труда?',
])
def test_industrial_work_preparation_or_a_context_dependent_reference_stays_in_search(message):
    assert not operational_request(message)


@pytest.mark.parametrize(('question','outside'), [
    ('При какой температуре выпекать порцию?', True),
    ('Постирайте салфетку вручную.', True),
    ('Как сварить два элемента дома по карте сборки?', False),
])
def test_scope_and_ticket_with_empty_corpus(isolated, question, outside):
    conn, dsn = isolated
    model = FirstSource()
    dialog = Dialog(dsn, model, shortlist=lambda *a, **kw: [], search=lambda *a, **kw: Candidates([], []))
    user = UserContext('reader@example.org', 'employee', 'chat', 'message')
    answer = asyncio.run(dialog.answer(user, question, status))
    assert answer.outcome is (Outcome.OFF_TOPIC if outside else Outcome.NO_BASIS)
    assert bool(answer.ticket) == bool(model.calls) == (not outside)
    assert conn.execute('SELECT count(*) FROM mail_outbox').fetchone() == (int(not outside),)
