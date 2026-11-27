"""Keep the requested act and document bound to the signer evidence."""

import hashlib
import json
from dataclasses import asdict

import pytest

from reglament_agent.answer import Outcome, Scope, SourceRef, UserContext
from reglament_agent.dialog.grounding import Anchor, assemble, model_ref, vocabulary
from reglament_agent.dialog.relevance import asks_signer
from reglament_agent.dialog.state import cache_key


def anchor(point, text):
    ref = SourceRef("sample", "Регламент аренды", "Отдел аренды", 1, point, sentence=0)
    return Anchor(ref, text, "", 0)


@pytest.mark.parametrize("question", [
    "Кто подписывает договор с арендодателем?",
    "На ком подписание договора с арендодателем?",
    "Кто отвечает за подписание договора с арендодателем?",
    "Кто подписант договора с арендодателем?",
])
def test_signer_intent_accepts_direct_and_indirect_questions(question):
    assert asks_signer(question)


@pytest.mark.parametrize("question", [
    "Кто инициирует подписание договора с арендодателем?",
    "На ком организация подписания договора с арендодателем?",
    "Кто согласует подписание договора с арендодателем?",
    "Кто готовит договор к подписанию?",
    "Когда подписывают договор с арендодателем?",
])
def test_signer_intent_excludes_other_questions_about_signing(question):
    assert not asks_signer(question)


@pytest.mark.parametrize("question", [
    "Кто подписывает договор с арендодателем?",
    "Где узнать, на ком подписание договора с арендодателем?",
])
def test_signer_evidence_requires_the_act_and_document(question):
    rows = [
        anchor("1.1", "РП инициирует подписание договора с арендодателем."),
        anchor("1.2", "РП инициирует подписание дополнительного соглашения аналогично заключению договора с арендодателем."),
        anchor("1.3", "Дополнительное соглашение с арендодателем подписывает Директор направления."),
        anchor("1.4", "Для подписания договора с арендодателем РП готовит комплект документов."),
        anchor("1.5", "Подписание договора с арендодателем осуществляется в электронной системе."),
        anchor("1.6", "За подготовку и подписание договора с арендодателем отвечает Логист."),
    ]
    assert list(vocabulary(question, rows).values()) == [rows[-1]]


def test_direct_signer_for_agreement_keeps_its_own_document():
    question = "Кто подписывает дополнительное соглашение с арендодателем?"
    contract = anchor("1.1", "Договор с арендодателем подписывает Логист.")
    addendum = anchor("1.2", "Дополнительное соглашение с арендодателем подписывает Директор направления.")
    assert list(vocabulary(question, [contract, addendum]).values()) == [addendum]


def test_initiator_question_keeps_initiator_evidence():
    question = "Кто инициирует подписание договора с арендодателем?"
    signer = anchor("1.1", "Договор с арендодателем подписывает Логист.")
    initiator = anchor("1.2", "РП инициирует подписание договора с арендодателем.")
    assert list(vocabulary(question, [signer, initiator]).values()) == [initiator]


def test_an_unrelated_initiator_clause_does_not_hide_the_signer():
    question = "На ком подписание договора с арендодателем?"
    provision = anchor("1.1", "РП инициирует согласование, за подписание договора с арендодателем отвечает Логист.")
    assert list(vocabulary(question, [provision]).values()) == [provision]


@pytest.mark.parametrize("external", [False, True])
def test_indirect_signer_is_not_invented_when_grounded_role_is_absent(external):
    question = "Где узнать, на ком подписание субподрядного договора?"
    wrong = anchor("1.1", "РП инициирует подписание дополнительного соглашения аналогично заключению договора.")
    pointer = anchor("2.1", "Субподрядный договор оформляется согласно Регламенту подбора субподрядчиков.")
    rows = [wrong, pointer] if external else [wrong]
    items = vocabulary(question, rows)
    claims = [
        {"ref": model_ref(key, row), "text": row.text, "actor": "", "action": ""}
        for key, row in items.items()
    ]
    answer = assemble(question, {"outcome": "answer", "claims": claims}, items, ["Отдел аренды"], False, {}, rows)

    if external:
        assert answer.outcome is Outcome.EXTERNAL_DOC
        assert [source for statement in answer.statements for source in statement.sources] == [pointer.ref]
    else:
        assert answer.outcome is Outcome.NO_BASIS
        assert answer.statements == []



@pytest.mark.parametrize("question", [
    "На ком подписание договора?",
    "Какой срок подачи заявки?",
])
def test_answer_policy_revision_invalidates_the_previous_cache_key(isolated, question):
    conn, _ = isolated
    user = UserContext("reader@example.org", "employee", "chat", "1")
    scope = Scope(None, None)
    model = "model"
    old_data = ["extract-v1", model, user.role, asdict(scope), question.strip().casefold(), [], []]
    old_key = hashlib.sha256(json.dumps(old_data, ensure_ascii=False).encode()).hexdigest()

    assert cache_key(conn, user, question, scope, model) != old_key
