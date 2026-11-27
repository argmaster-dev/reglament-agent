import asyncio
from pathlib import Path

import pytest

from reglament_agent.answer import Outcome, Scope, UserContext
from reglament_agent.dialog import context, processes
from reglament_agent.dialog.route import Dialog
from reglament_agent.ingest.embedding import FakeEmbedder
from reglament_agent.ingest.parser import load_cards
from reglament_agent.storage.publish import publish
from reglament_agent.storage.retrieve import retrieve
from reglament_agent.storage.sources import point_view
from tests.dialog.test_process_intent import SelectFirst, status


@pytest.mark.parametrize('scoped,plain,stage', [
    ('Что делать с заявкой на этапе проекта?', 'Что делать с заявкой?', 'project'),
    ('На проектной стадии в ТД что делать с заявкой?', 'Что делать с заявкой?', 'project'),
    ('Что делать с проектной заявкой в Технической дирекции на этапе проекта?',
     'Что делать с проектной заявкой?', 'project'),
    ('Как мне запустить проект на этапе проекта?', 'Как мне запустить проект?', 'project'),
    ('Что делать с заявкой на стадии пресейла?', 'Что делать с заявкой?', 'presale'),
])
def test_explicit_scope_preserves_actual_subject(scoped, plain, stage):
    assert context.stage_in(scoped) == stage
    assert processes.broad_subject(scoped, Scope('Техническая дирекция', stage)) == processes.broad_subject(plain)


@pytest.mark.parametrize('question', [
    'Что делать с заявкой на этапе проекта без согласования?',
    'Кто подписывает договор с подрядчиком на этапе проекта?',
    'Что делать с заявкой на пресейловую экспертизу?',
    'Что делать с заявкой на проектную экспертизу?',
    'Что делать с заявкой на техническую экспертизу?',
])
def test_specific_object_or_condition_does_not_become_open(question):
    assert not processes.broad_subject(question, Scope('Техническая дирекция', 'project'))


def test_adjective_of_presale_is_not_a_stage_name():
    assert context.stage_in('Что делать с заявкой на пресейловую экспертизу?') is None


def test_known_department_and_stage_still_clarify_process_in_active_scope(isolated):
    conn, dsn = isolated
    corpus = Path(__file__).resolve().parents[2] / 'docx'
    card = next(c for c in load_cards(corpus) if c.doc_id == 'td-project')
    publish(conn, card, (corpus / card.file).read_bytes(), FakeEmbedder(), 'tester')
    scopes = []

    def search(*args, **kwargs):
        scopes.append((kwargs['department'], kwargs['stage']))
        return retrieve(*args, **kwargs)

    model = SelectFirst()
    route = Dialog(dsn, model, search=search)
    user = UserContext('employee@example.org', 'employee', 'scoped-process', 'ask')
    asyncio.run(route.profiles.set_department(user.email, card.department))
    answer = asyncio.run(route.answer(user, 'Что делать с заявкой на этапе проекта?', status))
    assert (answer.scope.department, answer.scope.stage) == (card.department, 'project')
    assert scopes == [(card.department, 'project')]
    assert answer.outcome == Outcome.CLARIFY and answer.clarification.dimension == 'intent'
    options = answer.clarification
    assert 2 <= len(options.options) == len(options.option_refs) <= 4 and not model.prompts
    assert all(ref.doc_id == card.doc_id and point_view(conn, ref).point == ref.point for ref in options.option_refs)
