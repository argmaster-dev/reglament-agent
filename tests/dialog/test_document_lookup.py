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
    ('Где найти регламент о хранении оригиналов?', True, True),
    ('В каком внутреннем документе описано хранение оригиналов?', True, True),
    ('Как хранить оригиналы? Дайте ссылку на инструкцию.', True, True),
    ('Где по регламенту хранить оригиналы?', True, False),
    ('Как хранить оригиналы?', True, False),
    ('Где найти регламент о хранении оригиналов?', False, False),
])
def test_navigation_names_only_the_verified_document(isolated, question, has_evidence, name_expected):
    _, dsn = isolated
    main = replace(point('3.1', 'Хранение оригиналов', 'Оригиналы хранят в архиве.'),
                   doc_title='Передача и хранение оригиналов', version_no=4)
    other = replace(point('3.2', 'Другие вопросы', 'Отчёты печатают по средам.', doc='another'),
                    doc_title='Работа с отчётами')
    found = [main, other] if has_evidence else []
    route = Dialog(dsn, FirstSource(), search=lambda *a, **kw: Candidates(found, []),
                   shortlist=lambda *a, **kw: [])
    answer = asyncio.run(route.answer(UserContext('reader@example.org', 'employee', 'lookup', '1'), question, status))
    assert answer.outcome is (Outcome.ANSWER if has_evidence else Outcome.NO_BASIS)
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
