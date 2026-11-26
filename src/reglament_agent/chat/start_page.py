"""Start page `/start` with two role cards, served by the same FastAPI app as Chainlit."""

from __future__ import annotations

import html
import os
from collections import deque
from collections.abc import Sequence
from math import ceil
from time import monotonic
from typing import TYPE_CHECKING

from fastapi import Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from reglament_agent.chat import session as S
from reglament_agent.chat.render import EXAMPLES
from reglament_agent.chat.services import available_departments

if TYPE_CHECKING:
    from chainlit.user import User

PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Агент по регламентам</title><style>
body{{font-family:system-ui,sans-serif;max-width:760px;margin:6vh auto;padding:0 16px;color:#1b1b1b}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px}}
.card{{border:1px solid #c9c9c9;border-radius:12px;padding:20px}} h2{{margin-top:0}}
button,select,input{{font:inherit;padding:8px 12px;margin-top:8px;width:100%;box-sizing:border-box}}
button{{cursor:pointer;background:#1f5fbf;color:#fff;border:0;border-radius:8px}} .err{{color:#b00020}}
@media(max-width:560px){{.cards{{grid-template-columns:minmax(0,1fr)}} body{{margin:16px}}}} li{{margin:.45em 0}}
</style></head><body><h1>Агент по регламентам</h1><p>Ответы по рабочим регламентам со ссылками на основания.</p><div class="cards">
<form class="card" method="post" action="/start/employee"><h2>Сотрудник</h2>
<p>Пароль не нужен. Диалог и профиль видны только вам.</p>
<label>Подразделение (необязательно)<select name="department"><option value="">Без профиля</option>{options}</select></label>
<button type="submit">Войти как сотрудник</button></form>
<form class="card" method="post" action="/start/expert"><h2>Служба качества</h2>{availability}
<p>Обращения сотрудников, ответы экспертов, FAQ и документы.</p>{error}
<label>Пароль<input type="password" name="password" required maxlength="512" autocomplete="current-password" {disabled}></label>
<button type="submit" {disabled}>Войти как служба качества</button></form></div><section><h2>Примеры вопросов</h2><ul>{examples}</ul></section><p>Для одновременной проверки роли сотрудника и эксперта откройте вторую роль в другом браузере или приватном окне.</p></body></html>"""


def secret() -> str:
    return os.environ["CHAINLIT_AUTH_SECRET"]


def render_page(error: bool = False, expert_enabled: bool = True, *, departments: Sequence[str] = ()) -> str:
    opts = "".join(f'<option value="{html.escape(d)}">{html.escape(d)}</option>' for d in departments)
    err = '<p class="err">Неверный пароль.</p>' if error else ""
    questions = [*EXAMPLES, "Кто подписывает договор с субподрядчиком?", "Что такое ТКП?"]
    return PAGE.format(
        options=opts,
        error=err,
        availability="" if expert_enabled else "<p>Вход службы качества отключён.</p>",
        disabled="" if expert_enabled else "disabled",
        examples="".join(f"<li>{html.escape(q)}</li>" for q in questions),
    )


def _secure_cookie(request: Request) -> bool:
    return os.environ.get("CHAT_SECURE_COOKIES") == "true" or request.url.scheme == "https"


def chainlit_user(identity: S.Identity) -> User:
    from chainlit.user import User

    return User(identifier=identity.email, metadata={"role": identity.role, "department": identity.department})


def _redirect(request: Request, identity: S.Identity) -> RedirectResponse:
    from chainlit.auth import create_jwt, set_auth_cookie

    r = RedirectResponse("/", status_code=303)
    # Complete the role login before the browser starts Chainlit's router and /user request.
    set_auth_cookie(request, r, create_jwt(chainlit_user(identity)))
    r.set_cookie(
        S.COOKIE_NAME, S.sign_identity(identity, secret()), max_age=S.COOKIE_TTL,
        httponly=True, secure=_secure_cookie(request), samesite="lax",
    )
    return r


def install(app) -> None:
    """Add routes and a redirect middleware in front of Chainlit's catch-all route."""
    failed_attempts: deque[float] = deque()
    window_seconds = 60
    attempts_per_window = 5

    async def start(request: Request):
        if S.trusted_header():
            return RedirectResponse("/", status_code=303)
        departments = await available_departments()
        resp = HTMLResponse(render_page(
            request.query_params.get("error") == "1", bool(S.expert_password()), departments=departments,
        ))
        from chainlit.auth.cookie import clear_auth_cookie

        clear_auth_cookie(request, resp)
        resp.delete_cookie(S.COOKIE_NAME, secure=_secure_cookie(request), httponly=True)
        return resp

    async def employee(request: Request, department: str = Form("")):
        if S.trusted_header():
            return RedirectResponse("/", status_code=303)
        dep = department if department and department in await available_departments() else None
        return _redirect(request, S.Identity(S.demo_email(), "employee", dep))

    async def expert(request: Request, password: str = Form("", max_length=512)):
        if S.trusted_header() or not S.expert_password():
            return Response(status_code=403)
        now = monotonic()
        while failed_attempts and now - failed_attempts[0] >= window_seconds:
            failed_attempts.popleft()
        if len(failed_attempts) >= attempts_per_window:
            retry_after = ceil(failed_attempts[0] + window_seconds - now)
            return Response(
                "Слишком много попыток входа. Повторите через минуту.",
                status_code=429, headers={"Retry-After": str(retry_after)},
                media_type="text/plain",
            )
        if not S.check_expert_password(password):
            failed_attempts.append(now)
            return RedirectResponse("/start?error=1", status_code=303)
        return _redirect(request, S.Identity(S.demo_email().replace("demo-", "expert-"), "expert"))

    async def mail_session(request: Request):
        identity = S.identity_from_headers(request.headers, secret())
        allowed = identity is not None and identity.role == "expert"
        return Response(status_code=204 if allowed else 403)

    from fastapi.routing import APIRoute

    routes = [
        APIRoute("/start", start, methods=["GET"], response_class=HTMLResponse),
        APIRoute("/start/employee", employee, methods=["POST"]),
        APIRoute("/start/expert", expert, methods=["POST"]),
        APIRoute("/_ops/mail-session", mail_session, methods=["GET"], include_in_schema=False),
    ]
    for i, r in enumerate(routes):
        app.router.routes.insert(i, r)

    @app.middleware("http")
    async def need_start(request: Request, call_next):
        if (
            request.method == "GET"
            and (request.url.path == "/" or request.url.path.startswith("/thread/"))
            and not S.trusted_header()
            and S.identity_from_headers(request.headers, secret()) is None
        ):
            return RedirectResponse("/start", status_code=303)
        return await call_next(request)
