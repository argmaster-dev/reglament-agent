"""Chainlit entry point: `uv run chainlit run src/reglament_agent/chat/app.py`.

Role choice. Chainlit's own login page is a single email/password form and cannot show two role
cards, so the choice lives on a small start page (`/start`, FastAPI routes added to Chainlit's
app, see start_page.py). Role selection issues a signed profile and a Chainlit JWT in
the same redirect. Both use CHAINLIT_AUTH_SECRET. When CHAT_TRUSTED_HEADER is configured,
Chainlit authenticates the identity through its header callback. The reverse proxy
sets that header and strips client-supplied copies (see session.py).

The dialog logic is not here: this module only renders Answer objects (render.py).
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
import logging
import os
import secrets
import time
from weakref import WeakValueDictionary

import chainlit as cl
import httpx
import psycopg

# Chainlit needs a JWT secret for any auth; a random one is fine for a single dev process
# (all sessions are dropped on restart). Set CHAINLIT_AUTH_SECRET for stable sessions.
os.environ.setdefault("CHAINLIT_AUTH_SECRET", secrets.token_urlsafe(32))

from chainlit.server import app as fastapi_app  # noqa: E402

from reglament_agent.answer import Answer, Outcome, SourceRef, UserContext  # noqa: E402
from reglament_agent.storage.sources import faq_confirmations, resolve_point  # noqa: E402
from reglament_agent.chat import render as R  # noqa: E402
from reglament_agent.chat.aspects import ADVICE_OUTCOMES, cached_catalog, select_advice  # noqa: E402
from reglament_agent.chat import session as S  # noqa: E402
from reglament_agent.chat import start_page  # noqa: E402
from reglament_agent.admin import routes as admin_routes  # noqa: E402
from reglament_agent.chat.services import available_departments, build_services  # noqa: E402
from reglament_agent.chat import notifications as N  # noqa: E402

from reglament_agent.chat.admission import AdmissionRejected, AdmissionSettings, TurnAdmission
from reglament_agent.chat.history import router as history_router
from reglament_agent.model.queue import GenerationQueueFull
from reglament_agent.ops.health import router as health_router

services = build_services()
turn_admission = TurnAdmission(AdmissionSettings())
# Chainlit already registers /health. Give the dependency check precedence over its liveness handler.
for route in reversed([*history_router.routes, *health_router.routes]):
    fastapi_app.router.routes.insert(0, route)
start_page.install(fastapi_app)
admin_routes.install(fastapi_app, proposal_model=getattr(services.answerer, "model", None))


@cl.data_layer
def channel_storage():
    # The service owns the conversation and request tables. Chainlit would otherwise
    # interpret the application DSN as a provisioned Chainlit Cloud database at login.
    return None


@cl.header_auth_callback
async def auth(headers) -> cl.User | None:
    ident = S.identity_from_headers(headers, os.environ["CHAINLIT_AUTH_SECRET"])
    return start_page.chainlit_user(ident) if ident else None


def _actions(specs: list[R.ActionSpec]) -> list[cl.Action]:
    return [cl.Action(name=s.name, payload=s.payload, label=s.label) for s in specs]


def _user(message_id: str | None = None) -> UserContext:
    u = cl.user_session.get("user")
    return UserContext(u.identifier, u.metadata.get("role", "employee"), cl.user_session.get("id"), message_id)


def _fetch_notifications(email: str, dialog_id: str) -> list[dict]:
    import psycopg

    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
        return N.pending_notifications(conn, email, dialog_id)


def _mark_delivered(ids: list[int]) -> None:
    import psycopg

    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
        N.mark_delivered(conn, ids)


_notification_locks: WeakValueDictionary[tuple[str, str], asyncio.Lock] = WeakValueDictionary()


async def deliver_notifications() -> None:
    """Show expert answers to the user's tickets; never blocks the chat if the DB is unavailable."""
    u = cl.user_session.get("user")
    dialog_id = cl.user_session.get("id")
    if u is None or not dialog_id or not os.environ.get("DATABASE_URL"):
        return
    lock = _notification_locks.setdefault((u.identifier, dialog_id), asyncio.Lock())
    async with lock:
        try:
            items = await asyncio.to_thread(_fetch_notifications, u.identifier, dialog_id)
            for note in items:
                await cl.Message(content=N.notification_markdown(note)).send()
                await asyncio.to_thread(_mark_delivered, [note["id"]])
        except Exception:  # noqa: BLE001
            pass


async def _poll_notifications() -> None:
    while True:
        await deliver_notifications()
        await asyncio.sleep(3)


@cl.on_chat_end
async def stop_notifications() -> None:
    task = cl.user_session.get("notification_poll")
    if task is not None:
        cl.user_session.set("notification_poll", None)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def _start_notifications() -> None:
    await stop_notifications()
    if os.environ.get("DATABASE_URL"):
        cl.user_session.set("notification_poll", asyncio.create_task(_poll_notifications()))


@cl.on_chat_start
async def start() -> None:
    cl.user_session.set("busy", False)
    u = cl.user_session.get("user")
    if u.metadata.get("department") and not (await services.profiles.get(u.identifier)).confirmed_at:
        await services.profiles.set_department(u.identifier, u.metadata["department"])
    if u.metadata.get("role") == "expert":
        await cl.Message(content=R.expert_notice()).send()
    await cl.Message(content=R.WELCOME, actions=_actions(R.example_actions())).send()
    await _start_notifications()


@cl.on_chat_resume
async def resume(thread) -> None:
    cl.user_session.set("busy", False)
    await _start_notifications()


TURN_TIMEOUT = 60


def _retry_actions(text: str, message_id: str, choice: bool) -> list[cl.Action]:
    return _actions(
        [R.ActionSpec("retry", "🔁 Повторить", {"text": text, "message_id": message_id, "choice": choice})]
    )


async def _run(text: str, message_id: str, *, choice: bool = False) -> None:
    if cl.user_session.get("busy"):
        await cl.Message(content=R.ERROR_TEXTS["busy"]).send()
        return
    user = _user(message_id)
    try:
        with turn_admission.accept(user.email):
            cl.user_session.set("busy", True)
            try:
                await _run_admitted(user, text, message_id, choice=choice)
            finally:
                cl.user_session.set("busy", False)
    except AdmissionRejected as rejected:
        await cl.Message(
            content=R.ERROR_TEXTS[rejected.reason],
            actions=_retry_actions(text, message_id, choice),
        ).send()


async def _run_admitted(user: UserContext, text: str, message_id: str, *, choice: bool) -> None:
    phase = "Обрабатываю вопрос…"
    started = time.monotonic()
    progress = cl.Message(content=f"⏳ {phase}")
    lock = asyncio.Lock()

    async def update_status(label=None):
        nonlocal phase
        async with lock:
            if label is not None:
                phase = label
            progress.content = f"⏳ {phase} · {int(time.monotonic() - started)} с"
            await progress.update()

    async def heartbeat():
        while True:
            await asyncio.sleep(2)
            await update_status()

    clock = None
    try:
        await progress.send()
        clock = asyncio.create_task(heartbeat())
        method = services.answerer.choose if choice else services.answerer.answer
        try:
            async with asyncio.timeout(TURN_TIMEOUT):
                await deliver_notifications()
                answer = await method(user, text, update_status)
            error = "unavailable" if answer.outcome == Outcome.ERROR else None
        except GenerationQueueFull:
            error = "stand_busy"
        except (TimeoutError, httpx.TimeoutException):
            error = "timeout"
        except (httpx.RequestError, httpx.HTTPStatusError, psycopg.Error):
            logging.getLogger(__name__).exception("Chat request failed")
            error = "unavailable"
        finally:
            clock.cancel()
            with suppress(asyncio.CancelledError):
                await clock
        if error:
            progress.content = f"⚠️ {R.ERROR_TEXTS[error]} · {int(time.monotonic() - started)} с"
            progress.actions = _retry_actions(text, message_id, choice)
            await progress.update()
        else:
            await progress.remove()
            await _show(answer, text, message_id, choice_by=user if choice else None)
    except asyncio.CancelledError:
        if clock:
            clock.cancel()
        progress.content = R.ERROR_TEXTS["cancelled"]
        await progress.update()
        raise


async def clear_clarification():
    old = cl.user_session.get("clarify_msg")
    if old:
        await asyncio.gather(*(action.remove() for action in old.actions))
        old.actions = []
        cl.user_session.set("clarify_msg", None)


def _lookup_answer_extras(
    answer: Answer, choice_by: UserContext | None,
) -> tuple[dict[int, str], dict[tuple, SourceRef], str | None]:
    """Read extra references and, for a button choice, its resolved question from the same turn."""
    if not os.environ.get("DATABASE_URL"):
        return {}, {}, None
    try:
        with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
            dates = faq_confirmations(conn, R.faq_ids(answer)) if R.faq_ids(answer) else {}
            near = {}
            for n in R.near_points(answer):
                near[(n.doc_title, n.department, n.point)] = resolve_point(conn, n.doc_title, n.department, n.point)
            question = None
            if choice_by and answer.answer_id and answer.outcome in ADVICE_OUTCOMES and not answer.redirect:
                row = conn.execute(
                    "SELECT restated FROM dialog_requests "
                    "WHERE id=%s AND user_email=%s AND dialog_id=%s AND message_id=%s",
                    (answer.answer_id, choice_by.email, choice_by.dialog_id, choice_by.message_id),
                ).fetchone()
                if row:
                    question = row[0]
            return dates, near, question
    except psycopg.Error:
        logging.getLogger(__name__).exception("answer extras unavailable")
        return {}, {}, None


async def _show(answer: Answer, text: str, message_id: str, *, choice_by: UserContext | None = None) -> None:
    await clear_clarification()
    names = R.assign_chip_names(answer)
    faq_dates, near_refs, question = await asyncio.to_thread(_lookup_answer_extras, answer, choice_by)
    advice = select_advice(cached_catalog(), answer, question or text)
    specs = R.clarify_actions(answer) + R.answer_actions(answer, text, message_id)
    if answer.outcome is Outcome.CLARIFY:
        specs = R.clarify_actions(answer)
    evidence = R.evidence_groups(answer, names)
    note = R.refusal_element(answer, lambda n: near_refs.get((n.doc_title, n.department, n.point)))
    elements = [cl.CustomElement(name=name, display="inline", props=props)
                for name, props in R.element_specs(answer, evidence, note, R.advice_element(advice))]
    msg = cl.Message(content="", elements=elements)
    # Only verified statements reach the stream; raw model tokens may still lose their basis.
    for part in R.answer_markdown(answer, names, separate_evidence=bool(evidence), faq_dates=faq_dates).splitlines(keepends=True):
        await msg.stream_token(part)
    msg.actions = _actions(specs)
    await msg.send()
    if answer.answer_id and answer.outcome != Outcome.CLARIFY:
        cl.user_session.set("last_answer_id", answer.answer_id)
    if answer.outcome is Outcome.CLARIFY:
        cl.user_session.set("clarify_msg", msg)


@cl.on_message
async def on_message(message: cl.Message) -> None:
    if cl.user_session.get("notification_poll") is None:
        await _start_notifications()
    text = message.content.strip()
    if text == "/feedback":
        await open_feedback(cl.user_session.get("last_answer_id"))
        return
    small_reply = len(text) == 2 and cl.user_session.get("clarify_msg") is not None
    if not (small_reply or 3 <= len(text) <= 2000):
        await cl.Message(content="Введите вопрос длиной от 3 до 2000 символов.").send()
        return
    cl.user_session.set("last_mid", message.id)
    await _run(text, message.id)


@cl.action_callback("example")
async def example(action: cl.Action) -> None:
    text = action.payload["text"]
    msg = cl.Message(content=text, author="user", type="user_message")
    await msg.send()
    cl.user_session.set("last_mid", msg.id)
    await _run(text, msg.id)


@cl.action_callback("retry")
async def retry(action: cl.Action) -> None:
    if cl.user_session.get("busy"):
        return
    text, mid = action.payload["text"], action.payload["message_id"]
    await action.remove()
    if action.forId:
        await cl.Message(content="", id=action.forId).remove()
    await _run(text, mid, choice=action.payload.get("choice", False))


@cl.action_callback("choose")
async def choose(action: cl.Action) -> None:
    option = action.payload["option"]
    old = cl.user_session.get("clarify_msg")
    if not old or action.forId != old.id or cl.user_session.get("busy"):
        return
    await clear_clarification()
    await cl.Message(content=option, author="user", type="user_message").send()
    await _run(option, action.id, choice=True)


@cl.action_callback("open_source")
async def open_source(action: cl.Action) -> None:
    ref = R.ref_from_payload(action.payload)
    try:
        pv = await services.sources.point(ref)
    except (ValueError, LookupError):
        await cl.Message(content="Источник обновлён или недоступен. Задайте вопрос ещё раз.").send()
        return
    await cl.ElementSidebar.set_elements(
        [cl.CustomElement(name="SourcePoint", props=R.point_panel(pv), display="side")]
    )
    await cl.ElementSidebar.set_title(ref.label)


@cl.action_callback("open_law")
async def open_law(action: cl.Action) -> None:
    catalog = cached_catalog()
    law = catalog.laws.get(action.payload.get("law_id")) if catalog else None
    if law is None:
        await cl.Message(content="Текст статьи недоступен.").send()
        return
    await cl.ElementSidebar.set_elements([cl.CustomElement(name="LawPanel", props=R.law_panel(law), display="side")])
    await cl.ElementSidebar.set_title(law.reference)


@cl.action_callback("rate_up")
async def rate_up(action: cl.Action) -> None:
    await services.answerer.rate(_user(), action.payload["answer_id"], True, None)
    await action.remove()
    await cl.Message(content="Спасибо за оценку.").send()


async def open_feedback(answer_id):
    if not answer_id:
        await cl.Message(content="Сначала задайте вопрос. Отзыв относится к конкретному ответу.").send()
        return
    panel = cl.CustomElement(
        name="AnswerFeedback", display="side", props={"answer_id": answer_id, "reasons": R.DOWN_REASONS}
    )
    await cl.ElementSidebar.set_elements([panel])
    await cl.ElementSidebar.set_title("Отзыв об ответе")


@cl.action_callback("rate_down")
async def rate_down(action: cl.Action) -> None:
    await open_feedback(action.payload["answer_id"])


@cl.action_callback("save_feedback")
async def save_feedback(action: cl.Action):
    data = action.payload
    reason, comment = data.get("reason"), data.get("comment", "")
    if reason not in R.DOWN_REASONS or not isinstance(comment, str) or len(comment) > 1500:
        raise ValueError("Проверьте причину и длину комментария.")
    full = reason if not comment.strip() else f"{reason}. {comment.strip()}"
    await services.answerer.rate(_user(), data["answer_id"], False, full)
    if reason in ("Неверно", "Неполно"):
        await cl.Message(
            content="Отзыв сохранён. Передать вопрос службе качества?",
            actions=_actions([R.forward_action(data["answer_id"], full)]),
        ).send()


@cl.action_callback("rate_forward")
async def rate_forward(action: cl.Action) -> None:
    ticket = await services.answerer.rate(
        _user(), action.payload["answer_id"], False, action.payload["reason"], forward=True
    )
    await action.remove()
    await cl.Message(
        content=R.ticket_card(ticket) if ticket else "Не удалось создать обращение, попробуйте позже."
    ).send()


@cl.action_callback("change_scope")
async def change_scope(action: cl.Action) -> None:
    if not current_scope_action(action):
        await cl.Message(content="Чтобы вернуться к прежнему вопросу, задайте его ещё раз.").send()
        return
    departments = await available_departments()
    if not current_scope_action(action):
        return
    if not departments:
        await cl.Message(content="Нужен действующий регламент, чтобы выбрать подразделение.").send()
        return
    specs = [
        R.ActionSpec("set_department", d, {"department": d, "answer_id": action.payload["answer_id"]})
        for d in departments
    ]
    await cl.Message(content="Для какого подразделения ответить?", actions=_actions(specs)).send()


def current_scope_action(action: cl.Action) -> bool:
    return bool(
        action.payload.get("answer_id")
        and action.payload["answer_id"] == cl.user_session.get("last_answer_id")
        and not cl.user_session.get("clarify_msg")
        and not cl.user_session.get("busy")
    )


@cl.action_callback("set_department")
async def set_department(action: cl.Action) -> None:
    department = action.payload.get("department")
    if not department or not current_scope_action(action):
        return
    if department not in await available_departments() or not current_scope_action(action):
        return
    await action.remove()
    msg = cl.Message(content=f"А в подразделении {department}?", author="user", type="user_message")
    await msg.send()
    await _run(msg.content, msg.id)


@cl.action_callback("show_profile")
async def show_profile(action: cl.Action) -> None:
    p = await services.profiles.get(_user().email)
    await cl.Message(
        content=R.profile_markdown(p), actions=_actions([R.ActionSpec("reset_profile", "Сбросить", {})])
    ).send()


@cl.action_callback("reset_profile")
async def reset_profile(action: cl.Action) -> None:
    await services.profiles.reset(_user().email)
    await action.remove()
    await cl.Message(content="Готово, профиль сброшен.").send()
