"""A compound question must keep the appointed person distinct from the appointing person."""
import pytest

from reglament_agent.answer import Outcome, SourceRef
from reglament_agent.dialog.grounding import Anchor, assemble, model_ref


QUESTION = 'На ком назначение логиста для аудита и кто его привлекает?'
DUTY = 'За назначение логиста для аудита отвечает РП, которого привлекает заказчик.'
OTHER = 'Для аудита РП назначает координатора департамента.'


def evaluate(*claims, question=QUESTION):
    items, proposed = {}, []
    for i, (source, quote, actor, action) in enumerate(claims, 1):
        key = f'S{i}'
        anchor = Anchor(SourceRef('p', 'Порядок', 'Управление', 1, str(i), 0), source, '', i)
        items[key] = anchor
        proposed.append(dict(ref=model_ref(key, anchor), text=quote, actor=actor, action=action))
    trace = {}
    answer = assemble(question, {'outcome': 'answer', 'claims': proposed}, items, [], False, trace,
                      list(items.values()))
    return answer, trace


def test_verbatim_duty_with_wrong_action_metadata_still_answers_both_clauses():
    answer, trace = evaluate((DUTY, DUTY, 'РП', 'проверяет'), (OTHER, OTHER, 'РП', 'назначает'))
    assert answer.outcome == Outcome.ANSWER
    assert [s.text for s in answer.statements] == [DUTY]
    assert trace['dropped'] == ['topic_mismatch']


@pytest.mark.parametrize('source', [
    OTHER,
    'При аудите логист назначает заказчика.',
    'При аудите логист назначает заказчика и привлекает подрядчика.',
    'Для аудита РП назначает заказчика и привлекает подрядчика.',
])
def test_another_appointee_or_unconnected_action_answers_neither_clause(source):
    answer, _ = evaluate((source, source, '', 'назначает'))
    assert answer.outcome == Outcome.NO_BASIS
    assert not answer.statements


@pytest.mark.parametrize(('quote', 'actor'), [
    ('Назначение логиста якобы согласует заказчик.', 'РП'),
    (DUTY, 'заказчик'),
])
def test_cannot_repair_paraphrase_or_an_incorrect_owner(quote, actor):
    answer, _ = evaluate((DUTY, quote, actor, 'проверяет'))
    assert answer.outcome == Outcome.NO_BASIS


def test_other_process_in_same_text_does_not_establish_responsibility():
    source = 'За утверждение бюджета отвечает РП, назначение логиста проводит заказчик.'
    answer, _ = evaluate((source, source, 'РП', 'проверяет'))
    assert answer.outcome == Outcome.NO_BASIS


def test_missing_second_rule_yields_only_the_first_part():
    source = 'РП отвечает за назначение логиста для аудита.'
    unrelated = 'Для аудита заказчик привлекает координатора департамента.'
    answer, _ = evaluate((source, source, 'РП', 'отвечает'), (unrelated, unrelated, '', 'привлекает'))
    assert answer.outcome == Outcome.LOW_CONFIDENCE
    assert [s.text for s in answer.statements] == [source]


def test_distinct_citation_can_name_the_target_in_the_second_clause():
    source = 'РП отвечает за назначение логиста для аудита.'
    second = 'Заказчик привлекает логиста.'
    answer, _ = evaluate((source, source, 'РП', 'отвечает'), (second, second, 'заказчик', 'привлекает'))
    assert answer.outcome == Outcome.ANSWER
    assert [s.text for s in answer.statements] == [source, second]
