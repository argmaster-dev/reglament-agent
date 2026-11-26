"""Угроза: чрезмерно длинный ввод валит сервис (500) или раздувает обращения и письма."""

import asyncio
from types import SimpleNamespace

import pytest

from reglament_agent.answer import Outcome, UserContext

from .conftest import Q_SIGN

LONG = "Кто подписывает договор? " * 800  # около 20 000 символов


def test_dialog_survives_20000_chars_without_error(env):
    """Очень длинный вопрос доходит до модели и сохраняется без исключения (отказ — на уровне чата, см. ниже)."""
    answer, _, _ = env.ask(LONG, lambda s: {"outcome": "no_basis", "claims": []})
    assert answer.outcome in {Outcome.NO_BASIS, Outcome.ERROR, Outcome.OFF_TOPIC, Outcome.CLARIFY}


def test_chat_handler_politely_rejects_long_and_empty_messages(monkeypatch, tmp_path):
    """on_message отвечает вежливым отказом на 20 000 символов и на пустой ввод и не запускает обработку."""
    monkeypatch.setenv("CHAINLIT_APP_ROOT", str(tmp_path))
    from reglament_agent.chat import app

    sent, runs = [], []

    class Message:
        def __init__(self, content="", **kw):
            self.content = content

        async def send(self):
            sent.append(self.content)

    async def run(text, mid, **kw):
        runs.append(text)

    async def nothing():
        pass

    monkeypatch.setattr(app.cl, "Message", Message)
    monkeypatch.setattr(app.cl, "user_session", SimpleNamespace(get=lambda k: None, set=lambda k, v: None))
    monkeypatch.setattr(app, "_run", run)
    monkeypatch.setattr(app, "deliver_notifications", nothing)
    for text in (LONG, "x" * 2001, "  ", "ab"):
        asyncio.run(app.on_message(SimpleNamespace(content=text, id="1")))
    assert runs == [] and len(sent) == 4 and all("от 3 до 2000" in s for s in sent)
    asyncio.run(app.on_message(SimpleNamespace(content=Q_SIGN, id="2")))
    assert runs == [Q_SIGN]


@pytest.mark.xfail(reason="нет лимита длины — передано в ops-b01", strict=False)
def test_dialog_layer_refuses_oversized_question(env):
    """Retry/choose обходят проверку чата: Dialog должен сам вежливо отказать и не создавать обращение с 20 000 символов."""
    answer, model, _ = env.ask(LONG, lambda s: {"outcome": "no_basis", "claims": []})
    assert not env.tickets() or all(len(q) <= 2000 for _, q, _ in env.tickets())
    assert not model.prompts or all(len(m["content"]) < 20000 for m in model.prompts[0])
