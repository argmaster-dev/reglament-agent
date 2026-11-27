import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from reglament_agent.answer import Outcome, Statement, UserContext
from reglament_agent.dialog.route import Dialog
from reglament_agent.dialog.state import answers
from reglament_agent.ingest.embedding import FakeEmbedder
from reglament_agent.ingest.parser import load_cards
from reglament_agent.storage.publish import publish
from reglament_agent.storage.retrieve import Candidates
from reglament_agent.storage.sources import Hit, point_view


def point(number, section, text, doc='guide'):
    return Hit(1, doc, 1, number, text, 1, 'Правила', 'Отдел', 1, [text], [], 0.8, True, section)


def test_options_and_related_use_actual_distinct_processes_and_points():
    from reglament_agent.dialog import processes
    first = point('1.1', 'Регистрация заявки', 'Менеджер регистрирует заявку.')
    next_step = point('1.2', first.section_title, 'Архитектор проверяет заявку.')
    alternative = point('2.1', 'Согласование заявки', 'РП согласовывает заявку.')
    noise = point('3.1', 'Учёт отпусков', 'Менеджер регистрирует отпуск.')
    hits = [first, next_step, alternative, noise]
    choices = processes.options('Что делать с заявкой?', hits)
    assert [(name, ref.point) for name, ref in choices] == [(first.section_title, '1.1'), (alternative.section_title, '2.1')]
    assert not processes.options('Кто регистрирует заявку?', hits)
    assert not processes.options('Что делать с заявкой?', [first, next_step, noise])
    assert not processes.options('Что делать с доставкой?', hits)
    for vague in ['Как мне запустить проект?', 'Кто за это отвечает в проекте?']:
        sections = [point('3.1', 'Старт проекта', 'Менеджер запускает проект.'),
                    point('4.1', 'Инициация проекта', 'Архитектор открывает проект.')]
        assert len(processes.options(vague, sections)) == 2
    basis = Statement(first.text, [first.source_ref(sentence=0)])
    assert processes.related([basis], [first, noise, alternative, next_step, next_step,
        replace(next_step, version_no=2), replace(next_step, doc_id='another')]) == [next_step.source_ref()]
    legacy = answers.validate_python({'outcome':'clarify','clarification':{'question':'Где?', 'options':['А'], 'attempt':1}})
    assert legacy.related == [] and legacy.clarification.option_refs == []


class SelectFirst:
    def __init__(self):
        self.prompts = []

    async def embed(self, texts, **kwargs):
        return FakeEmbedder().embed(texts)

    async def chat_json(self, messages, schema, **kwargs):
        sources = json.loads(messages[-1]['content'])['источники']
        self.prompts.append(sources)
        first = sources[0]
        return SimpleNamespace(data={'outcome':'answer','claims':[{'ref':first['ref'],'text':first['цитата']}]})


async def status(_):
    pass


def test_process_choice_survives_retry_and_only_reads_selected_active_section(isolated):
    conn, dsn = isolated
    corpus = Path(__file__).resolve().parents[2] / 'docx'
    card = load_cards(corpus)[0]
    publish(conn, card, (corpus / card.file).read_bytes(), FakeEmbedder(), 'test')
    model = SelectFirst()
    route = Dialog(dsn, model)
    user = UserContext('employee@example.org', 'employee', 'thread', 'ask')
    ask = 'Что делать с заявкой?'
    reply = asyncio.run(route.answer(user, ask, status))
    c = reply.clarification
    assert reply.outcome == Outcome.CLARIFY and c.dimension == 'intent'
    assert c.attempt == 1 and 2 <= len(c.options) == len(c.option_refs) <= 4
    assert len(set(c.options)) == len(c.options) and not model.prompts
    assert all(point_view(conn, ref).point == ref.point for ref in c.option_refs)
    assert asyncio.run(route.answer(user, ask, status)) == reply
    selected = c.option_refs[-1]
    user.message_id = 'choose'
    answer = asyncio.run(Dialog(dsn, model).choose(user, c.options[-1], status))
    assert answer.outcome == Outcome.ANSWER and answer.statements
    section = selected.point.split('.')[0]
    assert all(s['ref']['doc_id'] == selected.doc_id and s['ref']['point'].split('.')[0] == section for s in model.prompts[0])
    assert all(ref.doc_id == selected.doc_id and ref.point.split('.')[0] == section for st in answer.statements for ref in st.sources)
    assert answer.nearby_sources == [] and len(answer.related) <= 3
    assert all(point_view(conn, ref).point == ref.point for ref in answer.related)
    assert asyncio.run(route.choose(user, c.options[-1], status)) == answer and len(model.prompts) == 1
    assert asyncio.run(route.profiles.get(user.email)).department is None

    user.dialog_id = 'unknown'
    user.message_id = 'first'
    prompt = asyncio.run(route.answer(user, ask, status))
    for attempt in (2,3):
        user.message_id = str(attempt)
        again = asyncio.run(route.choose(user, 'Не знаю', status))
        assert again.clarification.attempt == attempt and again.clarification.option_refs == prompt.clarification.option_refs
    user.message_id = 'exhausted'
    exhausted = asyncio.run(route.choose(user, 'Не знаю', status))
    assert exhausted.outcome == Outcome.NO_BASIS and exhausted.ticket and len(model.prompts) == 1

    user.dialog_id = 'stale'
    user.message_id = 'first'
    stale = asyncio.run(route.answer(user, ask, status))
    conn.execute('UPDATE documents SET active_version_id=NULL WHERE doc_id=%s', (card.doc_id,))
    user.message_id = 'choose'
    refused = asyncio.run(route.choose(user, stale.clarification.options[0], status))
    assert refused.outcome == Outcome.NO_BASIS and refused.ticket and not refused.statements and not refused.related
    assert len(model.prompts) == 1


def test_ordinary_answer_caches_only_additional_points_in_its_own_section(isolated):
    _, dsn = isolated
    main = point('9.1', 'Хранение документов', 'Оригинал хранится в архиве.')
    next_step = point('9.2', main.section_title, 'Запрос обрабатывает архивариус.')
    noise = point('2.1', 'Отпуска', 'Об отпуске сотрудник сообщает руководителю.')
    model = SelectFirst()
    route = Dialog(dsn, model, search=lambda *args, **kwargs: Candidates([main, noise, next_step], []),
                   shortlist=lambda *args, **kwargs: [])
    user = UserContext('employee@example.org', 'employee', 'ordinary', '1')
    question = 'Где хранится оригинал?'
    answer = asyncio.run(route.answer(user, question, status))
    assert answer.outcome == Outcome.ANSWER and answer.related == [next_step.source_ref()]
    assert not answer.nearby_sources and answer.statements[0].sources == [main.source_ref(sentence=0)]
    user.message_id = '2'
    again = asyncio.run(route.answer(user, question, status))
    assert again.from_cache and again.related == answer.related and len(model.prompts) == 1
