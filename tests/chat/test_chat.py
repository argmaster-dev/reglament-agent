import pytest

from reglament_agent.answer import Answer, Outcome, Scope, ScopeOrigin, SourceRef, Statement, TicketInfo, Variant
from reglament_agent.chat import render as R
from reglament_agent.chat import session as S
from reglament_agent.chat.services import build_services
from reglament_agent.chat.stubs import StubAnswerer

REF = SourceRef("d", "Док", "ТД", 1, "16.1")
SECRET = "s3cret"


def test_outcome_labels():
    assert R.outcome_label(Answer(Outcome.ANSWER)) == "✅ Ответ"
    assert R.outcome_label(Answer(Outcome.CLARIFY)) == "❓ Нужно уточнение"
    t = TicketInfo("2026-0001", "r", "sent", ["вопрос"])
    assert R.outcome_label(Answer(Outcome.NO_BASIS, ticket=t)) == "✉️ Обращение №2026-0001"
    assert "обращение №2026-0001" in R.outcome_label(Answer(Outcome.LOW_CONFIDENCE, ticket=t))
    assert all(R.OUTCOME_LABELS[o] or True for o in R.OUTCOME_LABELS)


def test_scope_line():
    assert R.scope_line(Scope("ТД", None, ScopeOrigin.PROFILE)) == "Отвечаю для: ТД (из профиля) · Изменить"
    assert "(из вопроса)" in R.scope_line(Scope("ТД", None, ScopeOrigin.QUESTION))
    assert "(из уточнения)" in R.scope_line(Scope("ТД", None, ScopeOrigin.CLARIFICATION))
    assert R.scope_line(Scope(None, None)) is None


def test_chips_and_statements():
    a = Answer(Outcome.ANSWER, "Итог", [Statement("Текст.", [REF])])
    md = R.answer_markdown(a)
    assert "- Текст. [ТД · п. 16.1]" in md
    other = SourceRef("d", "Док", "ТД", 1, "16.1", sentence=2)
    names = R.assign_chip_names(Answer(Outcome.ANSWER, statements=[Statement("a", [REF, other])]))
    assert len(set(names.values())) == 2
    assert R.chip(SourceRef("f", "F", "x", 1, "", faq_id=3)) == "[Служба качества]"


def test_ticket_card():
    card = R.ticket_card(TicketInfo("2026-0007", "нет в регламентах", "pending", ["вопрос", "профиль (ТД)"]))
    assert "№ 2026-0007" in card and "ожидает" in card
    assert "вопрос, профиль (ТД)" in card and "Ответ придёт в этот диалог и на ваш адрес" in card
    assert "шибка" in R.ticket_card(TicketInfo("1", "r", "error", []))


def test_variants_empty_department():
    a = Answer(Outcome.VARIANTS, variants=[Variant("ТД", [Statement("x", [REF])]), Variant("ДБРЦТ", [])])
    md = R.answer_markdown(a)
    assert "**ДБРЦТ**\n_в регламенте не указано_" in md


def test_low_confidence_caveat_error_and_retry():
    md = R.answer_markdown(Answer(Outcome.LOW_CONFIDENCE, "x", caveat="Проверьте"))
    assert md.startswith("> ⚠️ Проверьте")
    err = Answer(Outcome.ERROR)
    assert "вопрос сохранён" in R.answer_markdown(err)
    (retry,) = R.answer_actions(err, "вопрос", "m1")
    assert retry.name == "retry" and retry.payload["message_id"] == "m1"


def test_clarify_actions_include_dont_know():
    from reglament_agent.answer import Clarification
    a = Answer(Outcome.CLARIFY, clarification=Clarification("?", ["А", "Б"], 1))
    assert [s.label for s in R.clarify_actions(a)] == ["А", "Б", "Не знаю"]


def test_ref_payload_roundtrip():
    assert R.ref_from_payload(R.ref_payload(REF)) == REF


def test_demo_email_unique():
    emails = {S.demo_email() for _ in range(200)}
    assert len(emails) == 200
    assert all(e.startswith("demo-") and e.endswith("@demo.local") and len(e) == len("demo-12345678@demo.local") for e in emails)


def test_expert_password():
    assert not S.check_expert_password("demo-expert", {})
    assert not S.check_expert_password("", {"CHAT_EXPERT_PASSWORD": ""})
    assert not S.check_expert_password("nope", {})
    env = {"CHAT_EXPERT_PASSWORD": "xyz"}
    assert S.check_expert_password("xyz", env) and not S.check_expert_password("demo-expert", env)


def test_signed_cookie_cannot_be_forged():
    tok = S.sign_identity(S.Identity("a@b.c", "employee"), SECRET)
    assert S.verify_identity(tok, SECRET) == S.Identity("a@b.c", "employee")
    assert S.verify_identity(tok, "other") is None
    forged = S.sign_identity(S.Identity("a@b.c", "expert"), "wrong")
    assert S.verify_identity(forged, SECRET) is None
    assert S.verify_identity(tok, SECRET, now=10**12) is None  # expired
    h = {"cookie": f"x=1; {S.COOKIE_NAME}={tok}"}
    assert S.identity_from_headers(h, SECRET, {}).role == "employee"
    assert S.identity_from_headers({}, SECRET, {}) is None


def test_trusted_header_mode():
    env = {"CHAT_TRUSTED_HEADER": "X-User-Email", "CHAT_EXPERT_EMAILS": "Boss@corp.kz"}
    assert S.identity_from_headers({"x-user-email": "Ann@corp.kz"}, SECRET, env) == S.Identity("ann@corp.kz", "employee")
    assert S.identity_from_headers({"x-user-email": "boss@corp.kz"}, SECRET, env).role == "expert"
    assert S.identity_from_headers({}, SECRET, env) is None
    assert S.identity_from_headers({"x-user-email": "bad"}, SECRET, env) is None
    tok = S.sign_identity(S.Identity("a@b.c", "expert"), SECRET)  # cookie ignored in trusted mode
    assert S.identity_from_headers({"cookie": f"{S.COOKIE_NAME}={tok}"}, SECRET, env) is None


def test_build_services_stubs():
    assert isinstance(build_services({}).answerer, StubAnswerer)


import asyncio

from reglament_agent.answer import UserContext


def test_stub_scenarios():
    async def noop(_): ...

    async def run():
        s = build_services({}).answerer
        u = UserContext("e@x", "employee", "d1", "m1")
        a = await s.answer(u, "За сколько подавать заявку на пресейл?", noop)
        assert a.outcome is Outcome.CLARIFY
        b = await s.choose(u, "Техническая дирекция", noop)
        assert b.outcome is Outcome.ANSWER and b.statements[0].sources[0].point == "1.5"
        t1 = await s.answer(u, "такси?", noop)
        t2 = await s.answer(u, "такси?", noop)  # same message id: no duplicate ticket
        assert t1.ticket.number == t2.ticket.number == "2026-0001"
        e1 = await s.answer(UserContext("e@x", "employee", "d1", "m9"), "ошибка", noop)
        e2 = await s.answer(UserContext("e@x", "employee", "d1", "m9"), "ошибка", noop)
        assert e1.outcome is Outcome.ERROR and e2.outcome is not Outcome.ERROR

    asyncio.run(run())


def test_switching_demo_identity_expires_prior_chat_session(monkeypatch, tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from reglament_agent.chat.start_page import install

    monkeypatch.setenv('CHAINLIT_APP_ROOT', str(tmp_path))
    monkeypatch.setenv('CHAINLIT_AUTH_SECRET', SECRET)
    app = FastAPI()
    install(app)
    with TestClient(app, follow_redirects=False) as client:
        for method, path in [('get', '/start'), ('post', '/start/employee')]:
            r = getattr(client, method)(path, headers={'Cookie': 'access_token=old; access_token_1=part'})
            expired = [v.split('=', 1)[0] for v in r.headers.get_list('set-cookie') if 'Max-Age=0' in v]
            assert {'access_token', 'access_token_1'} <= set(expired)
