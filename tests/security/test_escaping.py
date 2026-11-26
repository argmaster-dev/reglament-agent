"""Угроза: XSS через текст вопроса в чате, истории, карточке обращения, письме эксперту."""

from html.parser import HTMLParser

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from reglament_agent.admin import routes
from reglament_agent.chat import render as R
from reglament_agent.chat import session as S
from reglament_agent.chat.history import load, render, router as history_router
from reglament_agent.quality.mail import to_message

from .conftest import Q_DEADLINE

SECRET = "test-secret"
PAYLOADS = [
    "<script>alert(1)</script> Кто подписывает договор?",
    "<img src=x onerror=alert(1)>",
    '"><svg/onload=alert(1)> ' + Q_DEADLINE,
]
RAW = ("<script", "<img", "<svg")
STATIC_TAGS = {"script", "img", "svg", "iframe", "object"}


class Tags(HTMLParser):
    """Собирает опасные теги и обработчики on* из настоящей разметки (экранированный текст не считается)."""

    def __init__(self):
        super().__init__()
        self.bad = []

    def handle_starttag(self, tag, attrs):
        if tag in STATIC_TAGS or any(k.startswith("on") for k, _ in attrs):
            self.bad.append((tag, attrs))


def clean(text):
    parser = Tags()
    parser.feed(text)
    assert not parser.bad, parser.bad
    assert not any(tag in text for tag in RAW), text[:400]


@pytest.fixture
def web(env, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", env.dsn)
    monkeypatch.setenv("CHAINLIT_AUTH_SECRET", SECRET)
    monkeypatch.setenv("EMBEDDER", "fake")
    monkeypatch.setenv("ADMIN_BACKGROUND", "0")
    app = FastAPI()
    routes.install(app)
    app.include_router(history_router)

    def client(email, role):
        c = TestClient(app, follow_redirects=False)
        c.cookies.set(S.COOKIE_NAME, S.sign_identity(S.Identity(email, role), SECRET))
        return c

    return client


@pytest.mark.parametrize("payload", PAYLOADS)
def test_question_is_escaped_everywhere(env, web, payload):
    """Вопрос с тегами не должен попасть в разметку страниц сырым ни в одном из мест, где он показывается."""
    answer, _, _ = env.ask(payload, lambda s: {"outcome": "no_basis", "claims": []}, dialog="xss")
    assert answer.ticket
    clean(R.answer_markdown(answer))
    clean(R.ticket_card(answer.ticket))

    rows = load(env.conn, "victim@example.org", "xss")
    page = render(rows, "xss").body.decode()
    clean(page)
    assert "&lt;" in page

    number = answer.ticket.number
    expert = web("expert@x.local", "expert")
    ticket = expert.get(f"/admin/tickets/{number}")
    assert ticket.status_code == 200
    clean(ticket.text)
    assert "&lt;" in ticket.text or "&#34;&gt;&lt;" in ticket.text
    clean(expert.get("/admin/tickets").text)

    own = web("victim@example.org", "employee").get(f"/tickets/{number}")
    assert own.status_code == 200
    clean(own.text)


@pytest.mark.parametrize("payload", PAYLOADS)
def test_expert_mail_is_plain_text_without_header_injection(env, payload):
    """Письмо эксперту уходит как text/plain одной частью (HTML не интерпретируется), тема без переводов строк."""
    attack = payload + "\r\nBcc: evil@example.org\r\nSubject: взлом"
    env.ask(attack, lambda s: {"outcome": "no_basis", "claims": []})
    from psycopg.rows import dict_row

    row = env.conn.cursor(row_factory=dict_row).execute("SELECT * FROM mail_outbox").fetchone()
    msg = to_message(row, "from@x")
    assert msg.get_content_type() == "text/plain" and not msg.is_multipart()
    assert "\n" not in row["subject"] and "\r" not in row["subject"]
    assert msg["Bcc"] is None and "evil@example.org" not in str(msg["To"]) + str(msg["Cc"])
    assert msg["Subject"].count("Subject") <= 1
    assert "evil@example.org" in row["body"]  # остаётся текстом тела, а не заголовком
