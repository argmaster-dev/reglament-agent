import asyncio
import datetime as dt
import socket
from email import message_from_bytes
from email.header import decode_header, make_header

import psycopg
import pytest
from aiosmtpd.controller import Controller

from reglament_agent.answer import UserContext
from reglament_agent.quality import (MailSettings, OutboxSender, create_ticket, get_ticket,
                                     get_ticket_for_user, list_user_tickets)
from reglament_agent.quality import tickets as tickets_mod

YEAR = dt.datetime.now(dt.timezone.utc).year


class Collector:
    def __init__(self):
        self.messages = []

    async def handle_DATA(self, server, session, envelope):
        self.messages.append((envelope, message_from_bytes(envelope.content)))
        return "250 OK"


@pytest.fixture
def smtp():
    box = Collector()
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        box.port = sk.getsockname()[1]
    ctl = Controller(box, hostname="127.0.0.1", port=box.port)
    ctl.start()
    try:
        yield box
    finally:
        ctl.stop()


def make(conn, n=1, email="ivan@corp.kz", message_id=None, **kw):
    args = dict(user=UserContext(email, "employee", "d1", message_id), question="Как оформить отпуск?" + "x" * n,
                restated="Порядок оформления отпуска", reason="no_basis",
                profile={"department": "Техническая дирекция"},
                clarifications=[{"question": "Какой отпуск?", "answer": "Ежегодный"}],
                candidates=[{"doc_id": "D1", "point": "3.2", "version_no": 2, "title": "Отпуска", "score": 0.5}],
                service_email="quality@demo.local", public_base_url="http://x:8000")
    args.update(kw)
    return create_ticket(conn, **args)


def counts(conn):
    return (conn.execute("SELECT count(*) FROM tickets").fetchone()[0],
            conn.execute("SELECT count(*) FROM mail_outbox").fetchone()[0])


def sender(dsn, port, **kw):
    return OutboxSender(dsn, MailSettings(host="127.0.0.1", port=port, backoff_base=0, **kw))


def test_atomic_and_info(isolated, monkeypatch):
    conn, _ = isolated
    info = make(conn)
    assert info.number == f"{YEAR}-0001" and info.delivery == "pending"
    assert info.transferred == ["вопрос", "профиль (Техническая дирекция)", "1 уточнение"]
    assert counts(conn) == (1, 1)
    def boom(n):
        raise RuntimeError("boom")

    with monkeypatch.context() as m:
        m.setattr(tickets_mod, "message_id_for", boom)
        with pytest.raises(RuntimeError):
            make(conn, 2)
    assert counts(conn) == (1, 1)
    assert make(conn, 3).number == f"{YEAR}-0002"  # counter rolled back too


def test_idempotent(isolated):
    conn, _ = isolated
    a = make(conn, message_id="m1")
    b = make(conn, message_id="m1")
    c = make(conn, message_id="m1", email="other@corp.kz")
    assert a == b and a.number != c.number
    assert counts(conn) == (2, 2)


def test_sequential_numbers(isolated):
    conn, _ = isolated
    nums = [make(conn, i).number for i in range(3)]
    assert nums == [f"{YEAR}-000{i}" for i in (1, 2, 3)]


def test_access(isolated):
    conn, _ = isolated
    n = make(conn).number
    assert get_ticket_for_user(conn, n, "ivan@corp.kz")["number"] == n
    assert get_ticket_for_user(conn, n, "petr@corp.kz") is None
    assert get_ticket(conn, n)["reason"] == "no_basis"
    assert get_ticket(conn, "2000-0001") is None
    assert [t["number"] for t in list_user_tickets(conn, "ivan@corp.kz")] == [n]
    assert list_user_tickets(conn, "petr@corp.kz") == []


def test_smtp_down_then_up(isolated, smtp):
    conn, dsn = isolated
    info = make(conn)
    down = OutboxSender(dsn, MailSettings(host="127.0.0.1", port=1, backoff_base=3600))
    assert asyncio.run(down.run_once()) == 0
    row = conn.execute("SELECT status, attempts, next_attempt_at > now(), last_error FROM mail_outbox").fetchone()
    assert row[:3] == ("error", 1, True) and row[3]
    assert get_ticket(conn, info.number)["delivery"] == "error"
    assert asyncio.run(down.run_once()) == 0  # not due yet
    assert conn.execute("SELECT attempts FROM mail_outbox").fetchone()[0] == 1
    conn.execute("UPDATE mail_outbox SET next_attempt_at = now()")
    assert asyncio.run(sender(dsn, smtp.port).run_once()) == 1
    assert conn.execute("SELECT status, attempts, sent_at IS NOT NULL FROM mail_outbox").fetchone() == ("sent", 2, True)
    assert get_ticket(conn, info.number)["delivery"] == "sent"
    assert asyncio.run(sender(dsn, smtp.port).run_once()) == 0
    (env, msg), = smtp.messages
    assert msg["To"] == "quality@demo.local" and msg["Cc"] == "ivan@corp.kz"
    assert str(make_header(decode_header(msg["Subject"]))).startswith(f"[Обращение №{info.number}] Как оформить отпуск?")
    assert msg["Message-ID"] == f"<ticket-{info.number}@reglament-agent.local>"
    assert set(env.rcpt_tos) == {"quality@demo.local", "ivan@corp.kz"}
    body = msg.get_payload(decode=True).decode()
    assert f"http://x:8000/admin/tickets/{info.number}" in body and "п. 3.2" in body and "Ежегодный" in body


def test_max_attempts(isolated):
    conn, dsn = isolated
    make(conn)
    s = OutboxSender(dsn, MailSettings(host="127.0.0.1", port=1, backoff_base=0, max_attempts=2))
    for _ in range(4):
        asyncio.run(s.run_once())
    assert conn.execute("SELECT attempts FROM mail_outbox").fetchone()[0] == 2


def test_concurrent_senders(isolated, smtp):
    conn, dsn = isolated
    for i in range(6):
        make(conn, i)
    s = sender(dsn, smtp.port)

    async def both():
        return await asyncio.gather(s.run_once(), s.run_once(), s.run_once())

    assert sum(asyncio.run(both())) == 6
    assert len(smtp.messages) == 6
    assert len({m["Message-ID"] for _, m in smtp.messages}) == 6


def test_continuous_worker_drains_new_messages_and_stops_on_signal(isolated, smtp):
    import os
    import signal
    import subprocess
    import sys
    import time

    conn, dsn = isolated
    first = make(conn, message_id="m1")
    env = {
        **os.environ,
        "DATABASE_URL": dsn,
        "MAIL_HOST": "127.0.0.1",
        "MAIL_PORT": str(smtp.port),
    }
    with subprocess.Popen(
        [sys.executable, "-m", "reglament_agent.quality", "--interval", "0.05"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    ) as worker:
        def wait_for_sent(number):
            deadline = time.monotonic() + 5
            while get_ticket(conn, number)["delivery"] != "sent":
                if worker.poll() is not None:
                    pytest.fail(worker.stderr.read().decode())
                if time.monotonic() >= deadline:
                    pytest.fail(f"mail for {number} was not sent")
                time.sleep(0.02)

        try:
            wait_for_sent(first.number)
            second = make(conn, message_id="m2")
            wait_for_sent(second.number)
            assert len(smtp.messages) == 2
            worker.send_signal(signal.SIGTERM)
            assert worker.wait(3) == 0
        finally:
            if worker.poll() is None:
                worker.kill()


def test_mail_formats_candidate_version_from_dialog_trace():
    from reglament_agent.quality.mail import build_body

    body = build_body(
        number="2026-0001", question="Вопрос", restated=None, reason="no_basis",
        user_email="employee@demo.local", profile={}, clarifications=[],
        candidates=[
            {"doc_id": "rules", "point": "3.2", "version": 4},
            {"doc_id": "other-rules", "point": "1.1"},
        ],
        public_base_url="http://localhost:8000",
    )
    assert "rules, п. 3.2 (версия 4)" in body
    assert "версия None" not in body
