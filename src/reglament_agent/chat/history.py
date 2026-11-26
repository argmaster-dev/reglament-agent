"""Read the signed-in employee's conversations from the product request journal."""

import asyncio
import html
import os
from urllib.parse import quote

import psycopg
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from reglament_agent.dialog.state import answers
from reglament_agent.chat.session import identity_from_headers
from reglament_agent.chat.render import outcome_label

router = APIRouter()


def load(conn, email, dialog_id=None):
    if dialog_id is None:
        return conn.execute(
            """
            SELECT dialog_id, (array_agg(question ORDER BY created_at,id))[1], max(created_at)
            FROM dialog_requests WHERE user_email=%s GROUP BY dialog_id
            ORDER BY max(created_at) DESC LIMIT 100""",
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


def render(rows, dialog_id):
    esc = html.escape
    if dialog_id is None:
        content = "".join(
            f'<li><a href="/history/{quote(key, safe="")}">{esc(title)}</a> '
            f"<time>{date:%d.%m.%Y %H:%M}</time></li>"
            for key, title, date in rows
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
    return page("История диалога", "".join(blocks))


@router.get("/history")
@router.get("/history/{dialog_id}")
async def history(request: Request, dialog_id: str | None = None):
    identity = identity_from_headers(request.headers, os.environ["CHAINLIT_AUTH_SECRET"])
    if not identity:
        return RedirectResponse("/start", status_code=303)

    def read():
        with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
            return render(load(conn, identity.email, dialog_id), dialog_id)

    return await asyncio.to_thread(read)
