"""chat-b04: ссылки у вариантов уточнения намерения и «Смотрите также» (поля приходят из ветки диалога, читаются через getattr)."""

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from reglament_agent.answer import Answer, Clarification, Outcome, SourceRef, Statement
from reglament_agent.chat import render as R

JSX = Path(__file__).parents[2] / "public/elements"


@dataclass(frozen=True)
class NewClar(Clarification):
    option_refs: list = field(default_factory=list)
    dimension: str = "department"


@dataclass
class NewAnswer(Answer):
    related: list = field(default_factory=list)


def ref(point, dep="ДБРЦТ", doc="d1"):
    return SourceRef(doc, "Регламент", dep, 1, point)


def clar(options, refs, dim="intent"):
    return NewAnswer(Outcome.CLARIFY, "", clarification=NewClar("Что вы хотите узнать?", options, 1, refs, dim))


def grounded(outcome=Outcome.ANSWER, related=(), cited="1.1", **kw):
    return NewAnswer(outcome, "Итог.", [Statement("Текст.", [ref(cited)])], related=list(related), **kw)


def test_intent_refs_have_correct_payloads_and_buttons_stay():
    a = clar(["Срок", "Размер"], [ref("1.4"), ref("2.2", "ОЗ", "d2")])
    assert [s.label for s in R.clarify_actions(a)] == ["Срок", "Размер", "Не знаю"]
    assert all(s.name == "choose" and s.payload == {"option": s.label} for s in R.clarify_actions(a)[:2])
    el = R.intent_element(a)
    assert [(i["option"], i["label"]) for i in el["items"]] == [("Срок", "ДБРЦТ · п. 1.4"), ("Размер", "ОЗ · п. 2.2")]
    assert R.ref_from_payload(el["items"][1]["source"]) == ref("2.2", "ОЗ", "d2")


def test_missing_refs_show_nothing_for_that_option():
    el = R.intent_element(clar(["А", "Б", "В"], [None, ref("1.4"), None]))
    assert [i["option"] for i in el["items"]] == ["Б"]
    assert R.intent_element(clar(["А"], [None])) is None
    assert R.intent_element(clar(["А", "Б"], [])) is None  # короче списка вариантов


@pytest.mark.parametrize("dim", ["department", "stage"])
def test_department_and_stage_clarification_unchanged(dim):
    a = clar(["ОЗ", "ДИБ"], [ref("1.1"), ref("1.2")], dim)
    assert R.intent_element(a) is None
    assert [s.label for s in R.clarify_actions(a)] == ["ОЗ", "ДИБ", "Не знаю"]
    assert R.element_specs(a, [], None, None) == []


def test_related_up_to_three_without_cited_and_duplicates():
    rel = [ref("1.1"), ref("2.1"), ref("2.1"), ref("3.1"), ref("4.1"), ref("5.1")]
    el = R.related_element(grounded(related=rel))
    assert [c["label"] for c in el["related"]] == ["ДБРЦТ · п. 2.1", "ДБРЦТ · п. 3.1", "ДБРЦТ · п. 4.1"]
    assert R.ref_from_payload(el["related"][0]["source"]) == ref("2.1")
    assert el["title"] == "Смотрите также:"


def test_related_only_cited_leaves_nothing():
    assert R.related_element(grounded(related=[ref("1.1")])) is None


@pytest.mark.parametrize("outcome", [Outcome.ANSWER, Outcome.VARIANTS, Outcome.LOW_CONFIDENCE])
def test_related_shown_for_grounded(outcome):
    assert R.related_element(grounded(outcome, [ref("2.1")]))


@pytest.mark.parametrize("outcome", [Outcome.NO_BASIS, Outcome.CONFLICT, Outcome.EXTERNAL_DOC, Outcome.OFF_TOPIC,
                                     Outcome.CLARIFY, Outcome.ERROR, Outcome.EXPERT_ANSWER])
def test_related_hidden_for_other_outcomes(outcome):
    assert R.related_element(NewAnswer(outcome, "x", related=[ref("2.1")])) is None


@pytest.mark.parametrize("kind", ["legal", "manager"])
def test_related_hidden_on_redirect(kind):
    a = grounded(related=[ref("2.1")])
    object.__setattr__(a, "redirect", kind)
    assert R.related_element(a) is None


def test_related_comes_before_advice_after_evidence():
    a = grounded(related=[ref("2.1")])
    specs = R.element_specs(a, [{"label": "", "items": []}], None, {"title": "t"})
    assert [n for n, _ in specs] == ["AnswerEvidence", "RelatedLinks", "AdviceBlock"]


def test_old_answer_without_new_fields_still_renders():
    old = Answer(Outcome.ANSWER, "Итог.", [Statement("Текст.", [ref("1.1")])])
    assert R.related_element(old) is None and R.intent_element(old) is None
    oldc = Answer(Outcome.CLARIFY, "", clarification=Clarification("В каком?", ["А"], 1))
    assert R.intent_element(oldc) is None
    assert [s.label for s in R.clarify_actions(oldc)] == ["А", "Не знаю"]
    assert R.element_specs(old, [], None, None) == []


def test_jsx_one_row_and_open_source():
    for name in ("RelatedLinks.jsx", "IntentRefs.jsx"):
        jsx = (JSX / name).read_text()
        for needle in ('flexWrap: "nowrap"', 'textOverflow: "ellipsis"', "open_source"):
            assert needle in jsx
    assert "dangerouslySetInnerHTML" not in (JSX / "RelatedLinks.jsx").read_text()
