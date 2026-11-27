"""Acceptance of expert reply publication and private delivery."""
import asyncio
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from reglament_agent.admin import quality_routes
from reglament_agent.chat import history, notifications, session
from reglament_agent.ingest.embedding import FakeEmbedder
from tests.admin.test_tickets import SECRET, client, env, make_ticket


def test_faq_publication_runs_synchronous_embedding_outside_asgi_loop(env, monkeypatch):
    number = make_ticket(env)

    class WorkerEmbedder(FakeEmbedder):
        def embed(self, texts):
            with pytest.raises(RuntimeError, match="no running event loop"):
                asyncio.get_running_loop()
            return super().embed(texts)

    monkeypatch.setattr(quality_routes, "make_embedder", WorkerEmbedder)
    expert = client("expert")
    response = expert.post(f"/admin/tickets/{number}", data={
        "action": "answer", "answer": "Правило проверено.", "basis_kind": "outside",
        "visibility": "faq", "faq_question": "Где найти новый порядок?",
    })
    assert response.status_code == 303
    assert env.execute("SELECT status FROM faq_entries").fetchone() == ("active",)
    assert "Журнал запроса" in expert.get(f"/admin/tickets/{number}").text


def test_private_reply_stays_with_the_originating_dialog_and_owner(env, monkeypatch):
    number = make_ticket(env, email="employee@x.local")
    monkeypatch.setattr(quality_routes, "make_embedder", lambda: pytest.fail("Private reply requested an embedding"))
    expected = "Частный комментарий для автора вопроса."
    expert = client("expert")
    # Posting the reply without a publication choice must keep it private.
    r = expert.post(f"/admin/tickets/{number}", data={
        "action": "answer", "answer": expected, "basis_kind": "outside",
    })
    assert r.status_code == 303
    repeat = expert.post(f"/admin/tickets/{number}", data={
        "action": "answer", "answer": expected, "basis_kind": "outside",
    })
    assert repeat.status_code == 400 and "уже обработано" in repeat.text
    assert env.execute("SELECT visibility FROM ticket_answers").fetchall() == [("private",)]
    assert env.execute("SELECT count(*) FROM faq_entries").fetchone() == (0,)
    assert env.execute("SELECT count(*) FROM mail_outbox WHERE kind='expert_answer'").fetchone() == (1,)
    assert env.execute("SELECT count(*) FROM chat_notifications").fetchone() == (1,)

    [note] = notifications.pending_notifications(env, "employee@x.local", "d1")
    assert notifications.pending_notifications(env, "employee@x.local", "other-dialog") == []
    assert notifications.pending_notifications(env, "other@x.local", "d1") == []
    markdown = notifications.notification_markdown(note)
    assert expected in markdown and "expert@x.local" in markdown and "только сотруднику" in markdown

    env.execute(
        "INSERT INTO dialog_requests(id,user_email,dialog_id,message_id,question,restated,answer,trace) "
        "VALUES (%s,'employee@x.local','d1','request','Вопрос','Вопрос',%s,'{}')",
        (uuid4(), Jsonb({"outcome": "no_basis"})),
    )
    app = FastAPI()
    app.include_router(history.router)
    with TestClient(app) as browser:
        browser.cookies.set(session.COOKIE_NAME, session.sign_identity(
            session.Identity("other@x.local", "employee"), SECRET
        ))
        assert browser.get("/history/d1").status_code == 403
        browser.cookies.set(session.COOKIE_NAME, session.sign_identity(
            session.Identity("employee@x.local", "employee"), SECRET
        ))
        assert "новый ответ" in browser.get("/history").text
        origin = browser.get("/history/d1")
        assert origin.status_code == 200 and expected in origin.text and "expert@x.local" in origin.text
        assert "новый ответ" not in browser.get("/history").text
        assert expected in browser.get("/history/d1").text

    employee = client("employee")
    assert expected in employee.get(f"/tickets/{number}").text
    remote = client("employee")
    remote.cookies.set(session.COOKIE_NAME, session.sign_identity(
        session.Identity("other@x.local", "employee"), SECRET
    ))
    assert remote.get(f"/tickets/{number}").status_code == 404
