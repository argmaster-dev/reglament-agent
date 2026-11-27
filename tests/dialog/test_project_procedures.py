import asyncio

import pytest

from reglament_agent.answer import Outcome, UserContext
from reglament_agent.dialog.project_procedures import requested
from reglament_agent.dialog.route import Dialog
from reglament_agent.storage.retrieve import Candidates
from tests.dialog.test_exact_evidence import evidence
from tests.dialog.test_route import Model, hit, status


CHANGE = 'Изменение объёма работ оформляется дополнительным соглашением сторон.'
ASSIGN = 'Координатор запрашивает у начальника отдела выделение специалистов на проект.'
ROSTER = 'Подрядчик должен иметь в команде ведущего инженера и архитектора.'


@pytest.mark.parametrize(('question', 'correct', 'wrong'), [
    ('Клиент захотел расширить функциональность, как согласовать?', CHANGE,
     'Стороны оформляют договор поставки. В объём поставки входит одна лицензия.'),
    ('Как зафиксировать сокращение объёма работ по проекту?', CHANGE,
     'Изменение состава проектной команды оформляется приказом руководителя.'),
    ('Как запросить специалистов для проекта?', ASSIGN, ROSTER),
    ('В каком порядке выделить бригаду для выезда на площадку?', ASSIGN,
     'Бригада обязана проверить оборудование на площадке.'),
])
def test_select_only_the_cited_rule_for_requested_procedure(question, correct, wrong):
    result = evidence(question, [wrong, correct])
    assert result.outcome == Outcome.ANSWER
    assert [s.text for s in result.statements] == [correct]
    assert 'Снабжение' in result.summary


@pytest.mark.parametrize(('question', 'wrong'), [
    ('Как согласовать расширение объёма работ по контракту?',
     'Объём работ указывается в договоре; договор аренды оформляется заявкой.'),
    ('Как согласовать расширение объёма работ по контракту?',
     'Изменение объёма работ отметили в плане, а договор аренды оформляется соглашением.'),
    ('Как запросить специалистов для проекта?',
     'Координатор запрашивает смету и выделяет специалистов на проект.'),
    ('Как запросить специалистов для проекта?',
     'Выделение бюджета на проект согласуют специалисты по закупкам.'),
    ('Как запросить специалистов для проекта?',
     'Бригадир направляет специалистов проверить пропускной режим, а не заявку на ресурсы.'),
    ('Как запросить специалистов для проекта?',
     'Подрядчик выделяет специалистов из своей команды по поручению заказчика.'),
    ('Как запросить специалистов подрядчика для проекта?', ASSIGN),
])
def test_adjacent_nouns_do_not_establish_the_procedure(question, wrong):
    assert evidence(question, [wrong]).outcome == Outcome.NO_BASIS


def test_question_about_a_contractors_staff_still_uses_its_staffing_requirement():
    result = evidence('Какие специалисты должны быть в команде подрядчика?', [ROSTER])
    assert [s.text for s in result.statements] == [ROSTER]


def test_dialog_clarifies_when_departments_formalize_changes_differently(isolated):
    _, dsn = isolated
    sources = [hit('Снабжение', CHANGE),
               hit('Производство', 'Изменение состава работ фиксируется протоколом.'),
               hit('Поддержка', 'Клиент хочет расширить функциональность продукта.')]
    model = Model()
    model.result = {'outcome': 'no_basis', 'claims': []}
    dialog = Dialog(dsn, model, shortlist=lambda *args, **kwargs: sources,
                    search=lambda *args, **kwargs: Candidates(sources, []))
    result = asyncio.run(dialog.answer(UserContext('reader@example.org', 'employee', 'example', '1'),
                                       'Клиент хочет расширить функциональность, как согласовать?', status))
    assert result.outcome == Outcome.CLARIFY
    assert set(result.clarification.options) == {'Снабжение', 'Производство'}
    assert not model.calls


@pytest.mark.parametrize('question', [
    'Как запросить командировку для проекта?', 'Как запросить инженерный паспорт для клиента?',
    'Клиент хочет добавить работника в список доступа, как оформить?',
    'Каким документом оформить изменение объёма работ, если у клиента нет договора?',
])
def test_other_objects_and_unverified_exceptions_keep_the_existing_route(question):
    assert requested(question) is None
