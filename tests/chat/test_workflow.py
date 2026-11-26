import asyncio

from reglament_agent.answer import PointView
from reglament_agent.chat import render
from reglament_agent.chat.start_page import render_page
from reglament_agent.ingest.model import Block, BlockKind, Table


def test_start_page_lists_examples_and_fits_phone():
    page = render_page()
    assert all(q in page for q in render.EXAMPLES)
    assert "другом браузере" in page
    assert "minmax(0,1fr)" in page or "@media" in page


def test_source_marks_only_selected_cell_and_labels_stage():
    row = "Обязательно: План | При наличии: Схема"
    pv = PointView(
        "Правила",
        "Проекты",
        ["project"],
        1,
        "2026-11-26",
        ["Раздел 1", "1.2"],
        "1.2",
        ["Материалы:", row],
        1,
        "При наличии",
        [],
        [Block(BlockKind.TABLE, "", Table(["Обязательно", "При наличии"], [["План", "Схема"]]))],
    )
    panel = render.point_panel(pv)
    assert panel["entries"][1]["cells"] == [
        {"header": "Обязательно", "text": "План", "selected": False},
        {"header": "При наличии", "text": "Схема", "selected": True},
    ]
    assert panel["stages"] == ["реализация"] and panel["version"] == 1


def test_generation_progress_reports_wait_then_admission():
    from reglament_agent.model.queue import generation_progress

    seen = []

    async def run():
        async def receive(text):
            seen.append(text)

        async with generation_progress(receive) as notify:
            notify(2)
            notify(1)
            notify(0)

    asyncio.run(run())
    assert seen == ["В очереди: 2", "В очереди: 1", "Формирую ответ…"]


def test_retry_on_timeout_preserves_original_operation(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from reglament_agent.answer import UserContext

    monkeypatch.setenv("CHAINLIT_APP_ROOT", str(tmp_path))
    from reglament_agent.chat import app

    messages = []
    calls = []

    class Message:
        def __init__(self, content):
            self.content, self.actions = content, []

        async def send(self):
            messages.append(self)

        async def update(self):
            pass

    class Slow:
        async def choose(self, user, option, status):
            calls.append((user.message_id, option))
            await asyncio.sleep(5)

    session = {}
    monkeypatch.setattr(app.cl, "user_session", SimpleNamespace(get=session.get, set=session.__setitem__))
    monkeypatch.setattr(app.cl, "Message", Message)
    monkeypatch.setattr(app, "_user", lambda mid: UserContext("demo@example.org", "employee", "thread", mid))
    monkeypatch.setattr(app, "_actions", lambda specs: specs)
    monkeypatch.setattr(app, "services", SimpleNamespace(answerer=Slow()))
    monkeypatch.setattr(app, "TURN_TIMEOUT", 0.01)
    asyncio.run(app._run("Отдел А", "choice-id", choice=True))
    assert calls == [("choice-id", "Отдел А")] and session["busy"] is False
    assert len(messages) == 1 and "⚠" in messages[0].content
    (retry,) = messages[0].actions
    assert retry.payload == {"text": "Отдел А", "message_id": "choice-id", "choice": True}


def test_history_scopes_both_index_and_details_to_owner(isolated):
    import uuid
    from psycopg.types.json import Jsonb
    from reglament_agent.answer import Answer, Outcome
    from reglament_agent.chat.history import load, render
    from reglament_agent.dialog.state import answers

    conn, _ = isolated
    for owner in ("first", "second"):
        conn.execute(
            """INSERT INTO dialog_requests(id,user_email,dialog_id,question,restated,answer,trace)
                       VALUES(%s,%s,%s,%s,'',%s,'{}')""",
            (
                uuid.uuid4(),
                owner,
                owner,
                "Вопрос " + owner,
                Jsonb(answers.dump_python(Answer(Outcome.ANSWER, summary="Ответ " + owner), mode="json")),
            ),
        )
    assert [r[0] for r in load(conn, "first")] == ["first"]
    assert "Ответ first" in render(load(conn, "first", "first"), "first").body.decode()
    assert render(load(conn, "first", "second"), "second").status_code == 403


def test_clickable_statements_keep_the_exact_reference_for_each_variant():
    from reglament_agent.answer import Answer, Outcome, SourceRef, Statement, Variant

    primary = SourceRef("doc", "Регламент", "Отдел", 2, "5", 0)
    following = SourceRef("doc", "Регламент", "Отдел", 2, "5", 1, "Обязательно")
    rule = Statement("Правило первого отдела.", [primary, following])
    answer = Answer(
        Outcome.ANSWER,
        summary=rule.text,
        statements=[rule],
        variants=[Variant("Второй отдел", [Statement("Условие второго отдела.", [following])])],
    )
    names = render.assign_chip_names(answer)
    main, variant = render.evidence_groups(answer, names)
    assert main["items"][0]["text"] == answer.summary
    assert [r["source"]["sentence"] for r in main["items"][0]["refs"]] == [0, 1]
    assert variant["items"][0]["refs"][0]["source"]["column"] == "Обязательно"
    assert answer.summary not in render.answer_markdown(answer, names, separate_evidence=True)


def test_history_http_uses_the_signed_employee_identity(isolated, monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from reglament_agent.answer import UserContext
    from reglament_agent.chat import session, start_page
    from reglament_agent.chat.history import router
    from reglament_agent.dialog.route import Dialog

    _, dsn = isolated
    secret = "test-session-signing-value"
    monkeypatch.setenv("CHAINLIT_AUTH_SECRET", secret)
    monkeypatch.setenv("DATABASE_URL", dsn)
    monkeypatch.delenv("CHAT_TRUSTED_HEADER", raising=False)
    app = FastAPI()
    app.include_router(router)
    start_page.install(app)

    async def quiet(_):
        pass

    with TestClient(app) as first, TestClient(app) as other:
        assert first.post("/start/employee", follow_redirects=False).status_code == 303
        assert other.post("/start/employee", follow_redirects=False).status_code == 303
        first_cookie = first.cookies.get(session.COOKIE_NAME)
        other_cookie = other.cookies.get(session.COOKIE_NAME)
        identity = session.verify_identity(first_cookie, secret)
        assert first_cookie != other_cookie and identity is not None
        answerer = Dialog(dsn, model=SimpleNamespace())
        owner = UserContext(identity.email, identity.role, "private-history", "1")
        asyncio.run(answerer.answer(owner, "Что такое РП?", quiet))
        address = "/history/private-history"
        history = first.get(address)
        assert history.status_code == 200 and "Что такое РП?" in history.text
        history = other.get(address, headers={"X-User-Email": identity.email})
        assert history.status_code == 403 and "Что такое РП?" not in history.text


def test_scope_action_uses_current_catalog_and_rejects_retired_choice(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    monkeypatch.setenv("CHAINLIT_APP_ROOT", str(tmp_path))
    from reglament_agent.chat import app

    departments = AsyncMock(return_value=["Новый отдел"])
    messages = []

    class Message:
        def __init__(self, content, actions=None, **kwargs):
            self.content, self.actions, self.id = content, actions, "user-message"

        async def send(self):
            messages.append(self)

    run = AsyncMock()
    monkeypatch.setattr(app, "available_departments", departments)
    monkeypatch.setattr(app, "current_scope_action", lambda action: True)
    monkeypatch.setattr(app, "_actions", lambda specs: specs)
    monkeypatch.setattr(app, "_run", run)
    monkeypatch.setattr(app.cl, "Message", Message)

    answers = SimpleNamespace(payload={"answer_id": "answer"})
    asyncio.run(app.change_scope(answers))
    [choice] = messages[0].actions
    assert choice.payload == {"department": "Новый отдел", "answer_id": "answer"}

    retired = SimpleNamespace(payload={"department": "ДБРЦТ", "answer_id": "answer"}, remove=AsyncMock())
    asyncio.run(app.set_department(retired))
    retired.remove.assert_not_called()
    run.assert_not_called()

    current = SimpleNamespace(payload=choice.payload, remove=AsyncMock())
    asyncio.run(app.set_department(current))
    current.remove.assert_awaited_once()
    run.assert_awaited_once_with("А в подразделении Новый отдел?", "user-message")
