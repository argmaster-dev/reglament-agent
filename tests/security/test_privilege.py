"""Угроза: сотрудник получает права эксперта или чужие данные (обращения, история, FAQ) прямыми HTTP-запросами."""

import base64
import json
import re
import time

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from reglament_agent.admin import routes
from reglament_agent.chat import session as S
from reglament_agent.chat.history import router as history_router

SECRET = "test-secret"
QUESTION = "Секретный вопрос Ивана про премии"
IVAN, PETR = "ivan@corp.kz", "petr@corp.kz"


@pytest.fixture
def world(env, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", env.dsn)
    monkeypatch.setenv("CHAINLIT_AUTH_SECRET", SECRET)
    monkeypatch.setenv("EMBEDDER", "fake")
    monkeypatch.setenv("ADMIN_BACKGROUND", "0")
    monkeypatch.setenv("CHAT_EXPERT_PASSWORD", "expert-password")  # без него cookie эксперта не принимается
    monkeypatch.delenv("CHAT_TRUSTED_HEADER", raising=False)
    app = FastAPI()
    routes.install(app)
    app.include_router(history_router)
    answer, _, _ = env.ask(QUESTION, lambda s: {"outcome": "no_basis", "claims": []}, email=IVAN, dialog="ivan-dialog")
    env.conn.execute(
        "INSERT INTO faq_entries(question,answer,basis_kind,author_email,status,embedding) "
        "VALUES ('Вопрос','Ответ','outside','quality@x.local','review',%s::vector)",
        (str([0.01] * 1024),),
    )

    def client(email=None, role=None, **kw):
        c = TestClient(app, follow_redirects=False, **kw)
        if email:
            c.cookies.set(S.COOKIE_NAME, S.sign_identity(S.Identity(email, role), SECRET))
        return c

    return SimpleWorld(app, client, answer.ticket.number, env)


class SimpleWorld:
    def __init__(self, app, client, number, env):
        self.app, self.client, self.number, self.env = app, client, number, env


def admin_requests(world):
    """Каждый маршрут /admin/* из схемы приложения с подставленными параметрами: (метод, путь)."""
    found = {("GET", "/admin")}
    for template, methods in world.app.openapi()["paths"].items():
        if template.startswith("/admin"):
            path = template.replace("{number}", world.number).replace("{faq_id}", "1").replace("{action}", "confirm")
            path = path.replace("{job_id}", "1")
            found |= {(m.upper(), path) for m in methods}
    assert len(found) > 12
    return sorted(found)


def test_employee_is_forbidden_on_every_admin_route(world):
    """С валидной cookie сотрудника каждый /admin/* (GET и POST) отвечает 403, без cookie уводит на /start."""
    employee, anonymous = world.client(PETR, "employee"), world.client()
    for method, path in admin_requests(world):
        assert employee.request(method, path).status_code == 403, (method, path)
        r = anonymous.request(method, path)
        assert r.status_code == 303 and r.headers["location"] == "/start", (method, path)
    for path in ("/admin/tickets/", "//admin/tickets", "/%61dmin/tickets", "/admin/./tickets", "/ADMIN/tickets"):
        assert employee.get(path).status_code in (403, 404, 307, 308, 303), path
        assert QUESTION not in employee.get(path).text


def faq_confirm_fields(world):
    """Токены проверки, которые эксперт получает вместе с текстом записи FAQ (admin-b01)."""
    page = world.client("quality@x.local", "expert").get("/admin/faq/1")
    assert page.status_code == 200
    fields = dict(re.findall(r'name="(expected_\w+)" value="([^"]*)"', page.text))
    assert fields
    return fields


def test_employee_cannot_answer_ticket_or_publish_faq(world):
    """Сотрудник не может ответить на обращение, подтвердить или удалить FAQ, даже с настоящими токенами проверки."""
    employee = world.client(PETR, "employee")
    fields = faq_confirm_fields(world)
    assert employee.post(f"/admin/tickets/{world.number}", data={"answer": "ok", "basis_kind": "outside"}).status_code == 403
    assert employee.post("/admin/faq/1/confirm", data=fields).status_code == 403
    assert employee.post("/admin/faq/1/confirm").status_code == 403
    assert employee.post("/admin/faq/1/delete").status_code == 403
    conn = world.env.conn
    assert conn.execute("SELECT status FROM tickets").fetchone()[0] == "open"
    assert conn.execute("SELECT count(*) FROM ticket_answers").fetchone()[0] == 0
    assert conn.execute("SELECT status FROM faq_entries").fetchone()[0] == "review"
    expert = world.client("quality@x.local", "expert")
    assert expert.post("/admin/faq/1/confirm", data=fields).status_code == 303  # контроль: эксперт с токенами может
    assert conn.execute("SELECT status FROM faq_entries").fetchone()[0] == "active"


def test_expert_cookie_is_ignored_without_expert_password(world, monkeypatch):
    """Если пароль эксперта не задан, даже верно подписанная cookie эксперта не даёт доступа."""
    monkeypatch.delenv("CHAT_EXPERT_PASSWORD")
    expert = world.client("quality@x.local", "expert")
    r = expert.get("/admin/tickets")
    assert r.status_code == 303 and r.headers["location"] == "/start"


def test_other_employee_cannot_read_ticket_or_history(world):
    """Чужой сотрудник не видит обращение и историю Ивана; владелец видит."""
    petr, ivan = world.client(PETR, "employee"), world.client(IVAN, "employee")
    assert ivan.get(f"/tickets/{world.number}").status_code == 200
    r = petr.get(f"/tickets/{world.number}")
    assert r.status_code == 404 and QUESTION not in r.text and IVAN not in r.text
    assert QUESTION in ivan.get("/history/ivan-dialog").text
    for dialog in ("ivan-dialog", "ivan%2Ddialog", "' OR '1'='1", "%25", "../ivan-dialog"):
        r = petr.get(f"/history/{dialog}")
        assert QUESTION not in r.text, dialog
    assert petr.get("/history/ivan-dialog").status_code == 403
    assert QUESTION not in petr.get("/history").text


def test_forged_cookies_are_rejected(world):
    """Подделанная, переподписанная чужим ключом, просроченная и обрезанная cookie не дают роли эксперта."""
    good = S.sign_identity(S.Identity(PETR, "employee"), SECRET)
    payload, sig = good.split(".")
    edited = json.loads(base64.urlsafe_b64decode(payload + "=="))
    edited["r"] = "expert"
    forged_payload = base64.urlsafe_b64encode(json.dumps(edited).encode()).decode().rstrip("=")
    wrong_key = S.sign_identity(S.Identity(PETR, "expert"), "attacker-key")
    expired = S.sign_identity(S.Identity(PETR, "expert"), SECRET, now=time.time() - 10 * S.COOKIE_TTL)
    for token in (f"{forged_payload}.{sig}", wrong_key, expired, forged_payload + ".", "." + sig, "", "expert", f"{payload}.{sig}x"):
        c = TestClient(world.app, follow_redirects=False)
        c.cookies.set(S.COOKIE_NAME, token)
        r = c.get("/admin/tickets")
        assert r.status_code == 303 and r.headers["location"] == "/start", token[:30]
        assert c.get(f"/tickets/{world.number}").status_code == 303


def test_query_params_and_headers_do_not_grant_role(world):
    """?role=expert, ?email=..., X-User-Email/X-User-Role игнорируются, пока не настроен доверенный заголовок."""
    employee = world.client(PETR, "employee")
    r = employee.get("/admin/tickets?role=expert&email=quality@x.local&expert=1")
    assert r.status_code == 403
    forged = {"X-User-Email": "quality@x.local", "X-User-Role": "expert", "X-Forwarded-User": "quality@x.local"}
    assert employee.get("/admin/tickets", headers=forged).status_code == 403
    assert world.client().get("/admin/tickets", headers=forged).status_code == 303
    assert world.client().get(f"/tickets/{world.number}?email={IVAN}", headers=forged).status_code == 303
    assert employee.get(f"/tickets/{world.number}?email={IVAN}").status_code == 404


def test_trusted_header_mode_ignores_cookie_and_unlisted_experts(world, monkeypatch):
    """Если задан доверенный заголовок, cookie игнорируется, а экспертом становится только адрес из списка."""
    monkeypatch.setenv("CHAT_TRUSTED_HEADER", "X-User-Email")
    monkeypatch.setenv("CHAT_EXPERT_EMAILS", "boss@corp.kz")
    cookie_expert = world.client("quality@x.local", "expert")
    assert cookie_expert.get("/admin/tickets").status_code == 303
    anyone = world.client()
    assert anyone.get("/admin/tickets", headers={"X-User-Email": PETR}).status_code == 403
    assert anyone.get("/admin/tickets", headers={"X-User-Email": PETR, "X-User-Role": "expert"}).status_code == 403
    assert anyone.get("/admin/tickets", headers={"X-User-Email": "boss@corp.kz"}).status_code == 200
    assert anyone.get("/admin/tickets", headers={"X-User-Email": "bad address"}).status_code == 303
