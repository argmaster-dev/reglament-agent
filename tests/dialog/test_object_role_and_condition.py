"""A role in the genitive after a verbal noun is the object of the act; a «без …» question needs that very condition."""

from reglament_agent.answer import SourceRef
from reglament_agent.dialog import relevance
from reglament_agent.dialog.checks import Claim, Evidence, SentenceRef, verify_claims
from reglament_agent.dialog.grounding import Anchor

SENTENCE = "За обучение Архитектора отвечает РП, который ведёт реестр курсов."
REF = SentenceRef("synthetic", "5.1", 0)


def reasons(actor, action):
    result = verify_claims([Claim(SENTENCE, REF, actor, action)], {REF: Evidence(REF, SENTENCE)})
    return [r for v in result.dropped for r in v.reasons]


def test_performer_binds_to_the_verbal_noun_despite_the_object_role_beside_it():
    assert reasons("рп", "обучение") == []
    assert reasons("рп", "отвечает") == []


def test_object_role_is_not_the_performer():
    assert reasons("архитектор", "обучение") == ["actor_not_bound"]


def test_other_role_still_not_bound_to_another_verb():
    assert reasons("архитектор", "ведёт") == ["actor_not_bound"]


def anchor(doc, point, text, context=""):
    return Anchor(SourceRef(doc, "Т " + doc, "Отдел А", 1, point, 0), text, context, 0)


def test_without_phrase_requires_its_own_condition():
    rows = [
        anchor("stage-a", "1.1", "Запрещено начинать работы без утверждённого лимита затрат."),
        anchor("stage-b", "2.1", "Работы по заявке начинаются после подписания акта, затраты фиксируются в отчёте."),
    ]
    related = relevance.applicable_refs("Можно ли начать работы без утверждённого лимита затрат?", rows)
    assert {r.doc_id for r in related} == {"stage-a"}


def test_executor_question_mentioning_a_time_limit_is_not_a_deadline_question():
    assert not relevance.asks_deadline("Кто контролирует сроки закупки?")
    assert relevance.asks_deadline("Какой срок закупки?")
