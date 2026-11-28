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
from reglament_agent.chat.release_footer import render_footer
from reglament_agent.chat.render import EXAMPLES
from reglament_agent.chat.services import available_departments

if TYPE_CHECKING:
    from chainlit.user import User

PAGE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Вход в Регламент-агент</title>
<link rel="stylesheet" href="/public/release-footer.css">
<style>
:root {{
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  background: #f4f6f9;
  color: #17212b;
  font-size: 15px;
  line-height: 1.5;
}}
*, *::before, *::after {{ box-sizing: border-box; }}
body {{ margin: 0; padding: 40px 24px 28px; }}
.shell {{ max-width: 1160px; min-height: calc(100vh - 68px); margin: auto; display: flex; flex-direction: column; gap: 48px; }}
header, .page-footer {{ display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px 24px; }}
.brand, .role-heading {{ display: flex; gap: 14px; align-items: flex-start; }}
.brand {{ align-items: center; gap: 12px; }}
.brand strong, h3 {{ font-size: 18px; }}
.brand small {{ display: block; }}
small, .note, .role-heading p, .example-context {{ color: #56616e; }}
small, .note, .example-context, .page-footer {{ font-size: 13px; }}
.icon {{ width: 40px; height: 40px; flex: none; display: grid; place-items: center; border-radius: 10px; }}
.brand .icon, .primary {{ background: #2a55a8; color: white; }}
.employee-icon, .source {{ background: #e9effa; color: #1f3f80; }}
.expert-icon {{ background: #eef1f5; color: #3e4955; }}
svg {{ width: 22px; height: 22px; fill: none; stroke: currentColor; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; }}
main {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 56px; align-items: start; }}
.intro, .roles, .card, figure {{ display: grid; gap: 16px; min-width: 0; }}
.intro {{ gap: 28px; padding-top: 8px; }}
h1, h2, h3, p, figure {{ margin: 0; }}
h1 {{ font-size: clamp(32px, 4vw, 44px); line-height: 1.12; letter-spacing: -.02em; text-wrap: balance; margin: 16px 0; }}
h2 {{ font-size: 20px; }}
.lead {{ max-width: 34em; font-size: 17px; color: #3e4955; }}
.eyebrow, figcaption {{ font-size: 12px; font-weight: 650; letter-spacing: .08em; text-transform: uppercase; }}
.eyebrow, summary {{ color: #2a55a8; }}
figcaption {{ color: #56616e; }}
figure, .card {{ background: white; border: 1px solid #dde3ea; border-radius: 14px; }}
figure {{ max-width: 520px; padding: 20px 22px; gap: 10px; }}
.question {{ justify-self: end; background: #eef1f5; border-radius: 12px 12px 4px 12px; padding: 10px 14px; margin: 4px 0; }}
.source {{ justify-self: start; border-radius: 6px; padding: 3px 8px; font-size: 13px; }}
.card {{ padding: 24px; }}
.role-heading p, label {{ font-size: 14px; }}
label {{ font-weight: 600; }}
label span {{ font-weight: 400; color: #56616e; }}
.field {{ display: grid; gap: 6px; }}
button, select, input {{ width: 100%; min-width: 0; font: inherit; border-radius: 10px; }}
select, input {{ height: 44px; border: 1px solid #8894a2; background: white; color: #17212b; padding: 0 12px; }}
button {{ min-height: 46px; padding: 8px 12px; cursor: pointer; border: 1px solid #2a55a8; font-weight: 600; }}
.secondary {{ background: white; color: #2a55a8; }}
.primary:hover {{ background: #1c3f82; }}
.secondary:hover:enabled {{ background: #e9effa; }}
button:disabled, input:disabled {{ cursor: not-allowed; color: #56616e; border-color: #8894a2; background: #eef1f5; }}
:focus-visible {{ outline: 3px solid #2a55a8; outline-offset: 3px; }}
.err {{ color: #a31d35; font-weight: 600; }}
summary {{ cursor: pointer; font-weight: 600; }}
ul {{ margin: 8px 0 0; padding-inline-start: 22px; }}
.page-footer {{ margin-top: auto; border-top: 1px solid #dde3ea; padding-top: 16px; color: #56616e; }}
.page-footer .release-footer {{ margin: 0; padding: 0; font-size: inherit; }}
@media (max-width: 800px) {{
  main {{ grid-template-columns: minmax(0, 1fr); gap: 28px; }}
  .shell {{ gap: 28px; }}
  body {{ padding: 24px 16px; }}
  .card, figure {{ padding: 18px; }}
}}
</style>
</head>
<body>
<div class="shell">
  <header>
    <div class="brand">
      <span class="icon"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 3h7l5 5v13H7z M14 3v5h5 M10 14l2 2 4-4"/></svg></span>
      <div><strong>Регламент-агент</strong><small>Ассистент по регламентам компании</small></div>
    </div>
    <p class="note">Модель работает локально, данные не покидают контур</p>
  </header>
  <main>
    <section class="intro" aria-labelledby="intro-title">
      <div>
        <span class="eyebrow">Ответы со ссылками на пункты</span>
        <h1 id="intro-title">Найдите ответ в регламентах компании</h1>
        <p class="lead">Спросите своими словами. Ассистент ответит коротко и покажет пункт, на котором основан ответ. Если ответа в регламентах нет, вопрос уйдёт в службу качества, а её ответ вернётся в ваш диалог.</p>
      </div>
      <figure>
        <figcaption>Пример ответа</figcaption>
        <p class="question">За сколько подавать заявку на пресейл?</p>
        <p class="example-context">Отвечаю для: Техническая дирекция</p>
        <p>Заявку на пресейл подают не позднее чем за две недели до подачи конкурса.</p>
        <span class="source">Техническая дирекция · п. 1.5</span>
      </figure>
    </section>
    <section class="roles" aria-labelledby="role-title">
      <h2 id="role-title">Выберите роль</h2>
      <form class="card" method="post" action="/start/employee" aria-labelledby="employee-title">
        <div class="role-heading">
          <span class="icon employee-icon"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 3.6-7 8-7s8 3 8 7"/></svg></span>
          <div><h3 id="employee-title">Сотрудник</h3><p>Вопросы по регламентам. Пароль не нужен, диалог и профиль видны только вам.</p></div>
        </div>
        <div class="field">
          <label for="department">Подразделение <span>(можно не указывать)</span></label>
          <select id="department" name="department"><option value="">Без профиля</option>{options}</select>
        </div>
        <button class="primary" type="submit">Войти как сотрудник</button>
      </form>
      <form class="card" method="post" action="/start/expert" aria-labelledby="expert-title">
        <div class="role-heading">
          <span class="icon expert-icon"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3l8 3v6c0 4.5-3.4 8.2-8 9-4.6-.8-8-4.5-8-9V6z M9 12l2 2 4-4"/></svg></span>
          <div><h3 id="expert-title">Служба качества</h3><p>Обращения сотрудников, ответы экспертов, FAQ, регламенты и аналитика.</p></div>
        </div>
        {availability}{error}
        <div class="field">
          <label for="password">Пароль</label>
          <input id="password" type="password" name="password" required maxlength="512" autocomplete="current-password" {disabled}>
        </div>
        <button class="secondary" type="submit" {disabled}>Войти как служба качества</button>
      </form>
      <p class="note">Чтобы проверить обе роли одновременно, откройте вторую в другом браузере или в приватном окне.</p>
      <details><summary>Примеры вопросов</summary><ul>{examples}</ul></details>
    </section>
  </main>
  <div class="page-footer">
    <p>Отвечает только по загруженным регламентам и подтверждённым ответам службы качества.</p>
    {release_footer}
  </div>
</div>
</body>
</html>"""


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
        release_footer=render_footer(),
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

    def release_footer():
        return HTMLResponse(render_footer(), headers={"Cache-Control": "no-store"})

    from fastapi.routing import APIRoute

    routes = [
        APIRoute("/start", start, methods=["GET"], response_class=HTMLResponse),
        APIRoute("/start/employee", employee, methods=["POST"]),
        APIRoute("/start/expert", expert, methods=["POST"]),
        APIRoute("/_ops/mail-session", mail_session, methods=["GET"], include_in_schema=False),
        APIRoute("/ui/release-footer", release_footer, methods=["GET"], include_in_schema=False),
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
