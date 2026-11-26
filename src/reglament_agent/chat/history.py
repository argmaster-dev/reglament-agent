"""Read the signed-in employee's conversations from the product request journal."""

import asyncio
import html
import os
from urllib.parse import quote

import psycopg
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from reglament_agent.chat import notifications
from reglament_agent.dialog.state import answers
from reglament_agent.chat.session import identity_from_headers
from reglament_agent.chat.render import outcome_label

router = APIRouter()


def load(conn, email, dialog_id=None):
    if dialog_id is None:
        return conn.execute(
            """
            SELECT r.dialog_id, (array_agg(r.question ORDER BY r.created_at,r.id))[1], max(r.created_at),
                   EXISTS (SELECT 1 FROM chat_notifications n WHERE n.user_email=r.user_email
                           AND n.dialog_id=r.dialog_id AND n.delivered_at IS NULL)
            FROM dialog_requests r WHERE r.user_email=%s GROUP BY r.user_email,r.dialog_id
            ORDER BY max(r.created_at) DESC LIMIT 100""",
            (email,),
        ).fetchall()
    return conn.execute(
        """
        SELECT question,answer,created_at FROM dialog_requests WHERE user_email=%s AND dialog_id=%s
        ORDER BY created_at,id""",
        (email, dialog_id),
    ).fetchall()


def page(title, content, status=200):
    title = html.escape(title)
    return HTMLResponse(
        f"""<!doctype html><html lang="ru"><meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
    <style>body{{font:1rem/1.6 system-ui;max-width:50rem;margin:4vh auto;padding:0 1rem;
    color:#202a36;overflow-wrap:anywhere}}article{{border-top:1px solid #ddd}}time{{color:#46576a}}
    a{{color:#164d91}}</style><main><a href="/">Вернуться в чат</a><h1>{title}</h1>{content}</main></html>""",
        status_code=status,
        headers={"Cache-Control": "no-store"},
    )


def render(rows, dialog_id, notes=()):
    esc = html.escape
    if dialog_id is None:
        content = "".join(
            f'<li><a href="/history/{quote(key, safe="")}">{esc(title)}</a> '
            f"<time>{date:%d.%m.%Y %H:%M}</time>"
            f"{' · <strong>новый ответ</strong>' if unread else ''}</li>"
            for key, title, date, unread in rows
        )
        return page(
            "Мои диалоги", f"<ul>{content}</ul>" if content else "<p>Сохранённых вопросов пока нет.</p>"
        )
    if not rows:
        return page("Диалог недоступен", "<p>Проверьте, что вы вошли под своим профилем.</p>", 403)
    blocks = ['<a href="/history">Все мои диалоги</a>']
    for question, payload, date in rows:
        answer = answers.validate_python(payload)
        parts = [answer.summary] if answer.summary else []
        if answer.clarification:
            parts.append(answer.clarification.question)
        for st in answer.statements:
            if st.text not in parts:
                parts.append(st.text)
        for variant in answer.variants:
            parts.append(
                f"{variant.department}: "
                + (" ".join(s.text for s in variant.statements) or "В регламенте не указано.")
            )
        if answer.ticket:
            parts.append("Обращение № " + answer.ticket.number)
        content = "".join(f"<p>{esc(p)}</p>" for p in parts)
        label = outcome_label(answer)
        blocks.append(
            f"<article><h2>{esc(question)}</h2><time>{date:%d.%m.%Y %H:%M}</time> · {esc(label)}{content}</article>"
        )
    for note in notes:
        date = note["created_at"]
        details = "".join(f"<p>{esc(line)}</p>" for line in notifications.notification_details(note))
        blocks.append(
            f"<article><h2>Ответ службы качества · обращение №{esc(note['ticket_number'])}</h2>"
            f"<time>{date:%d.%m.%Y %H:%M}</time><p>{esc(note['payload']['answer'])}</p>{details}</article>"
        )
    return page("История диалога", "".join(blocks))


@router.get("/history")
@router.get("/history/{dialog_id}")
async def history(request: Request, dialog_id: str | None = None):
    identity = identity_from_headers(request.headers, os.environ["CHAINLIT_AUTH_SECRET"])
    if not identity:
        return RedirectResponse("/start", status_code=303)

    def read():
        with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
            rows = load(conn, identity.email, dialog_id)
            if dialog_id is None or not rows:
                return render(rows, dialog_id)
            notes = notifications.conversation_notifications(conn, identity.email, dialog_id)
            response = render(rows, dialog_id, notes)
            notifications.mark_delivered(conn, [note["id"] for note in notes])
            return response

    return await asyncio.to_thread(read)
