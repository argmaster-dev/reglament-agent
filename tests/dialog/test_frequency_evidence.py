import asyncio
import json
from types import SimpleNamespace

import pytest

from reglament_agent.answer import Outcome, UserContext
from reglament_agent.dialog.route import Dialog
from reglament_agent.storage.retrieve import Candidates
from tests.dialog.test_route import Model, hit, status


class SelectAll(Model):
    async def chat_json(self, messages, schema, **kwargs):
        self.calls.append(messages)
        sources = json.loads(messages[1]['content'])['источники']
        return SimpleNamespace(data={'outcome': 'answer', 'claims': [
            {'ref': source['ref'], 'text': source['цитата']} for source in sources
        ]})


@pytest.mark.parametrize(('question', 'rule', 'expected'), [
    ('Как часто диспетчер проверяет журнал?', 'Диспетчер проверяет журнал раз в неделю.', Outcome.ANSWER),
    ('Как часто диспетчер проверяет журнал?', 'Диспетчер проверяет журнал в течение двух дней после смены.', Outcome.NO_BASIS),
    ('Как часто диспетчер проверяет журнал?', 'Диспетчер проверяет журнал, а бухгалтер еженедельно сверяет остатки.', Outcome.NO_BASIS),
    ('Как часто диспетчер проверяет журнал?', 'Диспетчер проверяет журнал и ежедневно регистрирует заявки.', Outcome.NO_BASIS),
    ('Как часто диспетчер проверяет журнал?', 'Диспетчер ежедневно обновляет журнал.', Outcome.NO_BASIS),
    ('Как часто диспетчер проверяет журнал?', 'Диспетчер еженедельно проверяет склад.', Outcome.NO_BASIS),
    ('Как часто диспетчер проверяет журнал?', 'Диспетчер еженедельно получает отчёт о проверке журнала.', Outcome.NO_BASIS),
    ('Как часто диспетчер проверяет журнал?', 'Инспектор ежедневно проверяет журнал диспетчера.', Outcome.NO_BASIS),
    ('Как часто диспетчер проверяет журнал?', 'Диспетчер ежемесячно проверяет накладные по журналу.', Outcome.NO_BASIS),
    ('Как часто диспетчер проверяет журнал?', 'Журнал еженедельно проверяет диспетчер.', Outcome.ANSWER),
    ('Как часто диспетчер проверяет журнал?', 'По журналу еженедельно проверяет накладные диспетчер.', Outcome.NO_BASIS),
    ('Как часто диспетчер проверяет журнал?', 'Журнал еженедельно проверяет руководитель диспетчера.', Outcome.NO_BASIS),
    ('Как часто мастера плетут образцы?', 'Мастера плетут образцы раз в месяц.', Outcome.ANSWER),
    ('Как часто инженер синхронизирует архив?', 'Инженер синхронизирует архив ежедневно.', Outcome.ANSWER),
    ('С какой периодичностью отдел обновляет реестр?', 'Отдел обновляет реестр каждый месяц.', Outcome.ANSWER),
    ('За сколько дней до проверки нужно передать журнал?', 'Журнал передают за два рабочих дня до проверки.', Outcome.ANSWER),
])
def test_frequency_must_apply_to_requested_action_and_object(isolated, question, rule, expected):
    conn, dsn = isolated
    source = hit('Отдел', rule)
    model = SelectAll()
    dialog = Dialog(dsn, model, shortlist=lambda *a, **kw: [], search=lambda *a, **kw: Candidates([source], []))
    answer = asyncio.run(dialog.answer(UserContext('reader@example.org', 'employee', 'frequency', '1'), question, status))
    assert answer.outcome == expected
    assert [s.text for s in answer.statements] == ([rule] if expected == Outcome.ANSWER else [])


@pytest.mark.parametrize(('second', 'complete'), [
    ('Диспетчер проверяет журнал после закрытия смены.', False),
    ('Диспетчер ежемесячно проверяет журнал.', True),
])
def test_compound_keeps_cited_first_part_and_escalates_only_a_missing_frequency(isolated, second, complete):
    conn, dsn = isolated
    first = 'Логист подписывает договор с подрядчиком.'
    source = hit('Отдел', first)
    source.sentences.append(second)
    model = SelectAll()
    dialog = Dialog(dsn, model, shortlist=lambda *a, **kw: [], search=lambda *a, **kw: Candidates([source], []))
    user = UserContext('reader@example.org', 'employee', 'compound', '1')
    question = 'Кто подписывает договор с подрядчиком и как часто диспетчер проверяет журнал?'
    answer = asyncio.run(dialog.answer(user, question, status))
    assert answer.outcome == (Outcome.ANSWER if complete else Outcome.LOW_CONFIDENCE)
    assert [(s.text, s.sources[0].sentence) for s in answer.statements] == ([(first, 0), (second, 1)] if complete else [(first, 0)])
    assert len(model.calls) == 1 and asyncio.run(dialog.answer(user, question, status)) == answer
    counts = conn.execute('SELECT (SELECT count(*) FROM tickets), (SELECT count(*) FROM mail_outbox)').fetchone()
    if not complete:
        assert answer.ticket and 'как часто' in answer.caveat
        assert counts == (1, 1)
    else:
        assert not answer.ticket and counts == (0, 0)
