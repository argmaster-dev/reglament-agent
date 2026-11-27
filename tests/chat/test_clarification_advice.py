"""Advice for an authenticated employee choosing an option from a pending question."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from reglament_agent.answer import Answer, Outcome, SourceRef, Statement, UserContext


@pytest.mark.parametrize(("turn", "expected", "excluded"), [
    ("choice", "public-procurement", "offer"),
    ("new-question", "offer", "public-procurement"),
    ("other-thread", None, "public-procurement"),
    ("other-user", None, "public-procurement"),
    ("other-message", None, "public-procurement"),
    ("legal", None, "public-procurement"),
])
def test_advice_after_clarification_is_scoped_to_the_actual_answer(isolated, monkeypatch, tmp_path,
                                                                  turn, expected, excluded):
    conn, dsn = isolated
    monkeypatch.setenv("CHAINLIT_APP_ROOT", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", dsn)
    from reglament_agent.chat import app, aspects

    owner = UserContext("reader@example.org", "employee", "first-thread", "click-1")
    option = "Техническая дирекция"
    response_id = str(uuid4())
    conn.execute(
        "INSERT INTO dialog_requests(id,user_email,dialog_id,message_id,question,restated,answer,trace) "
        "VALUES (%s,%s,%s,%s,%s,%s,'{}','{}')",
        (response_id, owner.email, owner.dialog_id, owner.message_id, option, "Как устроен тендер?"),
    )
    ref = SourceRef("td-project", "Правила", option, 1, "3.1")
    answer = Answer(Outcome.ANSWER, "Правило.", [Statement("Основание.", [ref])], answer_id=response_id)
    if turn == "legal":
        answer.redirect = "legal"
    user = {"other-user": replace(owner, email="else@example.org"),
            "other-thread": replace(owner, dialog_id="next-thread"),
            "other-message": replace(owner, message_id="another-click")}.get(turn, owner)
    text = "Как подготовить оферту?" if turn == "new-question" else option
    selected = []

    async def get_answer(*args):
        return answer

    async def quiet(*args):
        pass

    class Message:
        def __init__(self, content, **kwargs):
            self.content = content

        send = update = remove = stream_token = quiet

    def captured(catalog, result, question):
        advice = aspects.select_advice(catalog, result, question)
        selected.extend(item.aspect.id for item in advice)
        return advice

    session = {}
    monkeypatch.setattr(app.cl, "user_session", SimpleNamespace(get=session.get, set=session.__setitem__))
    monkeypatch.setattr(app.cl, "Message", Message)
    monkeypatch.setattr(app.cl, "CustomElement", lambda **kwargs: kwargs)
    monkeypatch.setattr(app, "_actions", lambda specs: specs)
    monkeypatch.setattr(app, "deliver_notifications", quiet)
    monkeypatch.setattr(app, "services", SimpleNamespace(answerer=SimpleNamespace(choose=get_answer, answer=get_answer)))
    monkeypatch.setattr(app, "select_advice", captured)
    asyncio.run(app._run_admitted(user, text, user.message_id, choice=turn != "new-question"))
    if expected:
        assert expected in selected
    assert excluded not in selected
    if turn == "legal":
        assert selected == []
