import asyncio
from types import SimpleNamespace

import pytest

from reglament_agent.answer import UserContext
from reglament_agent.chat.admission import AdmissionRejected, AdmissionSettings, TurnAdmission


def test_identity_limit_combines_conversations_and_leaves_room_for_other_users():
    admission = TurnAdmission(AdmissionSettings(max_in_flight=2))
    with admission.accept("first@example.org"):
        with pytest.raises(AdmissionRejected, match="user_busy"), admission.accept("first@example.org"):
            pytest.fail("The same signed identity started another turn")
        with admission.accept("other@example.org"):
            with pytest.raises(AdmissionRejected, match="stand_busy"), admission.accept("third@example.org"):
                pytest.fail("The process admitted more simultaneous work than configured")
    with admission.accept("first@example.org"):
        pass


def test_request_windows_expire_and_bound_history_across_new_identities():
    now = 0.0
    settings = AdmissionSettings(user_turns_per_minute=2, total_turns_per_minute=3)
    admission = TurnAdmission(settings, clock=lambda: now)
    for _ in range(2):
        with admission.accept("first@example.org"):
            pass
    with pytest.raises(AdmissionRejected, match="user_rate"), admission.accept("first@example.org"):
        pytest.fail("A third turn was accepted in one window")
    with admission.accept("second@example.org"):
        pass
    with pytest.raises(AdmissionRejected, match="stand_rate"), admission.accept("new@example.org"):
        pytest.fail("Fresh sessions exceeded the process request limit")

    now = 60.0
    with admission.accept("new@example.org"):
        assert len(admission._history) == 1
        assert set(admission._recent) == {"new@example.org"}


def test_cancellation_releases_admission():
    admission = TurnAdmission(AdmissionSettings(max_in_flight=1))

    async def cancelled():
        with admission.accept("first@example.org"):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(cancelled())
    with admission.accept("other@example.org"):
        pass


def test_chat_rejects_before_search_and_keeps_retry_choice(monkeypatch, tmp_path):
    monkeypatch.setenv("CHAINLIT_APP_ROOT", str(tmp_path))
    from reglament_agent.chat import app

    messages = []

    class Message:
        def __init__(self, content, actions=None):
            self.content = content
            self.actions = actions or []

        async def send(self):
            messages.append(self)

    async def must_not_run(*args):
        pytest.fail("Rejected turns must not reach the model or database")

    admission = TurnAdmission(AdmissionSettings())
    monkeypatch.setattr(app, "turn_admission", admission)
    monkeypatch.setattr(app.cl, "user_session", SimpleNamespace(get=lambda key: False))
    monkeypatch.setattr(app.cl, "Message", Message)
    monkeypatch.setattr(app, "_actions", lambda items: items)
    monkeypatch.setattr(app, "deliver_notifications", must_not_run)
    identity = "first@example.org"
    monkeypatch.setattr(app, "_user", lambda mid: UserContext(identity, "employee", "second tab", mid))
    monkeypatch.setattr(app, "services", SimpleNamespace(answerer=SimpleNamespace(choose=must_not_run)))
    with admission.accept(identity):
        asyncio.run(app._run("Отдел А", "choice-id", choice=True))

    assert len(messages) == 1 and "другом диалоге" in messages[0].content
    (retry,) = messages[0].actions
    assert retry.payload == {"text": "Отдел А", "message_id": "choice-id", "choice": True}


def test_generation_queue_overflow_keeps_retry_available_and_releases_admission(monkeypatch, tmp_path):
    from reglament_agent.model.queue import GenerationQueueFull

    monkeypatch.setenv("CHAINLIT_APP_ROOT", str(tmp_path))
    from reglament_agent.chat import app

    messages = []
    session = {}
    identity = "employee@example.org"

    class Message:
        def __init__(self, content):
            self.content = content
            self.actions = []

        async def send(self):
            messages.append(self)

        async def update(self):
            pass

    async def queue_full(user, text, status):
        raise GenerationQueueFull("Generation queue is full")

    admission = TurnAdmission(AdmissionSettings(max_in_flight=1))
    monkeypatch.setattr(app, "turn_admission", admission)
    monkeypatch.setattr(app.cl, "user_session", SimpleNamespace(get=session.get, set=session.__setitem__))
    monkeypatch.setattr(app.cl, "Message", Message)
    monkeypatch.setattr(app, "_actions", lambda items: items)
    monkeypatch.setattr(app, "_user", lambda mid: UserContext(identity, "employee", "tab", mid))
    monkeypatch.setattr(app, "services", SimpleNamespace(answerer=SimpleNamespace(answer=queue_full)))

    asyncio.run(app._run("Кто подписывает договор?", "question"))
    assert session["busy"] is False
    assert len(messages) == 1 and "Стенд занят" in messages[0].content
    (retry,) = messages[0].actions
    assert retry.payload == {"text": "Кто подписывает договор?", "message_id": "question", "choice": False}
    with admission.accept(identity):
        pass
