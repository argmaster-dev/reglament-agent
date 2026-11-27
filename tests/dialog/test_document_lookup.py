"""A request for the regulation names the document owning the verified evidence."""
import asyncio
from dataclasses import replace

import pytest

from reglament_agent.answer import Outcome, Statement, UserContext
from reglament_agent.dialog import processes
from reglament_agent.dialog.route import Dialog
from reglament_agent.storage.retrieve import Candidates
from tests.dialog.test_process_intent import point
from tests.dialog.test_request_scope import FirstSource
from tests.dialog.test_route import status


@pytest.mark.parametrize(('question', 'has_evidence', 'name_expected'), [
    ('Где найти регламент, где указано место хранения оригиналов?', True, True),
    ('В каком внутреннем документе описано место хранения оригиналов?', True, True),
    ('Где указано место хранения оригиналов? Дайте ссылку на инструкцию.', True, True),
    ('Где по регламенту указано место хранения оригиналов?', True, False),
    ('Какое предусмотрено место хранения оригиналов?', True, False),
    ('Где найти регламент, где указано место хранения оригиналов?', False, False),
])
def test_navigation_names_only_the_verified_document(isolated, question, has_evidence, name_expected):
    conn, dsn = isolated
    main = replace(point('3.1', 'Хранение оригиналов', 'Место хранения оригиналов — центральный архив.'),
                   doc_title='Передача и хранение оригиналов', version_no=4)
    other = replace(point('3.2', 'Другие вопросы', 'Отчёты печатают по средам.', doc='another'),
                    doc_title='Работа с отчётами')
    found = [main, other] if has_evidence else []
    route = Dialog(dsn, FirstSource(), search=lambda *a, **kw: Candidates(found, []),
                   shortlist=lambda *a, **kw: [])
    answer = asyncio.run(route.answer(UserContext('reader@example.org', 'employee', 'lookup', '1'), question, status))
    assert answer.outcome is (Outcome.ANSWER if has_evidence else Outcome.NO_BASIS), conn.execute(
        "SELECT trace->'dropped' FROM dialog_requests WHERE id=%s", (answer.answer_id,)
    ).fetchone()
    assert (main.doc_title in answer.summary) is name_expected
    assert other.doc_title not in answer.summary
    if has_evidence:
        assert answer.statements[0].sources == [main.source_ref(sentence=0)]
    else:
        assert answer.ticket and not answer.statements


def test_operational_document_is_not_a_request_for_its_source():
    source = point('4.2', 'Маршрут пакета', 'Акт передают в архив.')
    claim = Statement(source.text, [source.source_ref(sentence=0)])
    assert processes.document_summary('Какой документ нужно передать в архив?', [claim]) is None


@pytest.mark.parametrize('question', [
    'Где найти инструкцию о том, кто передаёт оригиналы в архив?',
    'В каком положении указано, кто передаёт оригиналы в архив?',
    'Пришлите ссылку на инструкцию, где объясняется, кто передаёт оригиналы в архив.',
])
def test_request_for_source_containing_one_supported_fact_has_no_missing_part(isolated, question):
    conn, dsn = isolated
    src = replace(point('5.1', 'Передача оригиналов', 'Логист передаёт оригиналы в архив.'),
                  doc_title='Обработка оригиналов', version_no=3)
    model = FirstSource()
    route = Dialog(dsn, model, search=lambda *a, **kw: Candidates([src], []), shortlist=lambda *a, **kw: [])
    user = UserContext('reader@example.org', 'employee', 'single', '1')
    answer = asyncio.run(route.answer(user, question, status))
    assert answer.outcome is Outcome.ANSWER and src.doc_title in answer.summary and not answer.caveat
    assert answer.statements[0].sources == [src.source_ref(sentence=0)]
    assert not answer.ticket and conn.execute('SELECT count(*) FROM mail_outbox').fetchone() == (0,)


@pytest.mark.parametrize(('question', 'expect'), [
    ('Где найти инструкцию о том, кто передаёт оригиналы в архив и как часто архивариус сверяет реестр?', Outcome.LOW_CONFIDENCE),
    ('Где найти инструкцию о том, как часто архивариус сверяет реестр?', Outcome.NO_BASIS),
])
def test_navigation_cannot_supply_a_missing_rule(isolated, question, expect):
    conn, dsn = isolated
    source = point('5.1', 'Передача оригиналов', 'Логист передаёт оригиналы в архив.')
    route = Dialog(dsn, FirstSource(), search=lambda *a, **kw: Candidates([source], []), shortlist=lambda *a, **kw: [])
    result = asyncio.run(route.answer(UserContext('reader@example.org', 'employee', 'missing', '1'), question, status))
    assert result.outcome is expect and result.ticket and conn.execute('SELECT count(*) FROM mail_outbox').fetchone() == (1,)
    if expect is Outcome.LOW_CONFIDENCE:
        assert result.statements[0].sources == [source.source_ref(sentence=0)] and 'как часто' in result.caveat
        assert 'найти инструкцию' not in result.caveat.lower()
    else:
        assert not result.statements


def test_navigation_does_not_swallow_an_independently_requested_fact():
    separate = 'Какой срок хранения и где найти инструкцию о том, кто передаёт оригиналы?'
    assert processes.evidence_subject(separate) == separate
    separate = 'Где найти регламент и как проверить сведения о том, кто передаёт оригиналы?'
    assert processes.evidence_subject(separate) == separate
    separate = 'Где найти регламент. Второй вопрос о том, кто передаёт оригиналы?'
    assert processes.evidence_subject(separate) == separate
    simple = 'Подскажите, где найти регламент о том, сколько дней хранить оригиналы?'
    assert processes.evidence_subject(simple) == 'сколько дней хранить оригиналы?'
