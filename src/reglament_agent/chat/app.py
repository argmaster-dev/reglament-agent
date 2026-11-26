"""Chainlit entry point: `uv run chainlit run src/reglament_agent/chat/app.py`.

Role choice. Chainlit's own login page is a single email/password form and cannot show two role
cards, so the choice lives on a small start page (`/start`, FastAPI routes added to Chainlit's
app, see start_page.py). The page sets an HMAC-signed cookie; Chainlit's header_auth_callback
verifies it and puts the role into the user metadata. A client cannot forge a role without
CHAINLIT_AUTH_SECRET. With CHAT_TRUSTED_HEADER set (production) identity comes only from that
header, which the reverse proxy must set and strip from client requests (see session.py).

The dialog logic is not here: this module only renders Answer objects (render.py).
"""

from __future__ import annotations

import asyncio
import os
import secrets
import time

import chainlit as cl
import httpx

# Chainlit needs a JWT secret for any auth; a random one is fine for a single dev process
# (all sessions are dropped on restart). Set CHAINLIT_AUTH_SECRET for stable sessions.
os.environ.setdefault("CHAINLIT_AUTH_SECRET", secrets.token_urlsafe(32))

from chainlit.server import app as fastapi_app  # noqa: E402

from reglament_agent.answer import Answer, Outcome, UserContext  # noqa: E402
from reglament_agent.chat import render as R  # noqa: E402
from reglament_agent.chat import session as S  # noqa: E402
from reglament_agent.chat import start_page  # noqa: E402
from reglament_agent.chat.services import build_services  # noqa: E402

services = build_services()
start_page.install(fastapi_app)


@cl.header_auth_callback
async def auth(headers) -> cl.User | None:
    ident = S.identity_from_headers(headers, os.environ["CHAINLIT_AUTH_SECRET"])
    if ident is None:
        return None
    return cl.User(identifier=ident.email, metadata={"role": ident.role, "department": ident.department})


def _actions(specs: list[R.ActionSpec]) -> list[cl.Action]:
    return [cl.Action(name=s.name, payload=s.payload, label=s.label) for s in specs]


def _user(message_id: str | None = None) -> UserContext:
    u = cl.user_session.get("user")
    return UserContext(u.identifier, u.metadata.get("role", "employee"), cl.user_session.get("id"), message_id)


@cl.on_chat_start
async def start() -> None:
    cl.user_session.set("busy", False)
    u = cl.user_session.get("user")
    if u.metadata.get("department"):
        await services.profiles.set_department(u.identifier, u.metadata["department"])
    if u.metadata.get("role") == "expert":
        await cl.Message(content=R.expert_notice()).send()
    await cl.Message(content=R.WELCOME, actions=_actions(R.example_actions())).send()


async def _run(call, text: str, message_id: str) -> None:
    """Run one answerer call with a progress message, then render the Answer."""
    if cl.user_session.get("busy"):
        await cl.Message(content=R.ERROR_TEXTS["busy"]).send()
        return
    cl.user_session.set("busy", True)
    cl.user_session.set("last_text", text)
    progress = cl.Message(content="⏳ Обрабатываю вопрос…")
    await progress.send()

    async def on_status(s: str) -> None:
        progress.content = f"⏳ {s}"
        await progress.update()

    try:
        answer: Answer = await call(on_status)
        await progress.remove()
        await _show(answer, text, message_id)
    except httpx.TimeoutException:
        await _fail(progress, "timeout")
    except httpx.HTTPStatusError:
        await _fail(progress, "http")
    except httpx.RequestError:
        await _fail(progress, "unavailable")
    except asyncio.CancelledError:
        await _fail(progress, "cancelled")
        raise
    finally:
        cl.user_session.set("busy", False)


async def _fail(progress: cl.Message, kind: str) -> None:
    progress.content = R.ERROR_TEXTS[kind]
    progress.actions = _actions(R.answer_actions(Answer(Outcome.ERROR), cl.user_session.get("last_text", ""), cl.user_session.get("last_mid", "")))
    await progress.update()


async def _show(answer: Answer, text: str, message_id: str) -> None:
    names = R.assign_chip_names(answer)
    specs = R.source_actions(names) + R.clarify_actions(answer) + R.answer_actions(answer, text, message_id)
    if answer.outcome is Outcome.CLARIFY:
        specs = R.clarify_actions(answer)
    msg = cl.Message(content=R.answer_markdown(answer, names), actions=_actions(specs))
    await msg.send()
    if answer.outcome is Outcome.CLARIFY:
        cl.user_session.set("clarify_msg", msg)


@cl.on_message
async def on_message(message: cl.Message) -> None:
    text = message.content.strip()
    if not 3 <= len(text) <= 2000:
        await cl.Message(content="Введите вопрос длиной от 3 до 2000 символов.").send()
        return
    cl.user_session.set("last_mid", message.id)
    await _run(lambda st: services.answerer.answer(_user(message.id), text, st), text, message.id)


@cl.action_callback("example")
async def example(action: cl.Action) -> None:
    text = action.payload["text"]
    msg = cl.Message(content=text, author="user", type="user_message")
    await msg.send()
    cl.user_session.set("last_mid", msg.id)
    await _run(lambda st: services.answerer.answer(_user(msg.id), text, st), text, msg.id)


@cl.action_callback("retry")
async def retry(action: cl.Action) -> None:
    text, mid = action.payload["text"], action.payload["message_id"]
    await action.remove()
    await _run(lambda st: services.answerer.answer(_user(mid), text, st), text, mid)  # same message id


@cl.action_callback("choose")
async def choose(action: cl.Action) -> None:
    option = action.payload["option"]
    old = cl.user_session.get("clarify_msg")
    if old:
        old.actions = []
        await old.update()
    await cl.Message(content=option, author="user", type="user_message").send()
    await _run(lambda st: services.answerer.choose(_user(), option, st), cl.user_session.get("last_text", ""), "")


@cl.action_callback("open_source")
async def open_source(action: cl.Action) -> None:
    ref = R.ref_from_payload(action.payload)
    pv = await services.sources.point(ref)
    await cl.ElementSidebar.set_elements([cl.Text(name=ref.label, content=R.point_markdown(pv), display="side")])
    await cl.ElementSidebar.set_title(ref.label)


@cl.action_callback("rate_up")
async def rate_up(action: cl.Action) -> None:
    await services.answerer.rate(_user(), action.payload["answer_id"], True, None)
    await action.remove()
    await cl.Message(content="Спасибо за оценку.").send()


@cl.action_callback("rate_down")
async def rate_down(action: cl.Action) -> None:
    await action.remove()
    await cl.Message(content="Что не так с ответом?", actions=_actions(R.reason_actions(action.payload["answer_id"]))).send()


@cl.action_callback("rate_reason")
async def rate_reason(action: cl.Action) -> None:
    aid, reason = action.payload["answer_id"], action.payload["reason"]
    await services.answerer.rate(_user(), aid, False, reason)
    await action.remove()
    await cl.Message(
        content=f"Записал: «{reason}». Могу передать вопрос в службу качества.",
        actions=_actions([R.forward_action(aid, reason)]),
    ).send()


@cl.action_callback("rate_forward")
async def rate_forward(action: cl.Action) -> None:
    ticket = await services.answerer.rate(_user(), action.payload["answer_id"], False, action.payload["reason"], forward=True)
    await action.remove()
    await cl.Message(content=R.ticket_card(ticket) if ticket else "Не удалось создать обращение, попробуйте позже.").send()


@cl.action_callback("change_scope")
async def change_scope(action: cl.Action) -> None:
    specs = [R.ActionSpec("set_department", d, {"department": d, "text": action.payload["text"]}) for d in S.DEPARTMENTS]
    await cl.Message(content="Для какого подразделения ответить?", actions=_actions(specs)).send()


@cl.action_callback("set_department")
async def set_department(action: cl.Action) -> None:
    u = _user()
    await services.profiles.set_department(u.email, action.payload["department"])
    await action.remove()
    text = action.payload["text"]
    await cl.Message(content=f"Подразделение: {action.payload['department']}. Отвечаю заново.").send()
    mid = f"{cl.user_session.get('last_mid', '')}:{secrets.token_hex(3)}"  # new id: this is a new request
    await _run(lambda st: services.answerer.answer(_user(mid), text, st), text, mid)


@cl.action_callback("show_profile")
async def show_profile(action: cl.Action) -> None:
    p = await services.profiles.get(_user().email)
    await cl.Message(content=R.profile_markdown(p), actions=_actions([R.ActionSpec("reset_profile", "Сбросить", {})])).send()


@cl.action_callback("reset_profile")
async def reset_profile(action: cl.Action) -> None:
    await services.profiles.reset(_user().email)
    await action.remove()
    await cl.Message(content="Готово, профиль сброшен.").send()
