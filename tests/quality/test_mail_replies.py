"""Expert answers by e-mail: matching, extraction, delivery, latest-wins, idempotency (no model)."""
import asyncio
from email.message import EmailMessage
from email.utils import format_datetime
import datetime as dt

import httpx
import pytest

from reglament_agent.answer import UserContext
from reglament_agent.chat.notifications import pending_notifications
from reglament_agent.ingest.embedding import FakeEmbedder
from reglament_agent.quality import (ImapReader, MailSettings, MailpitReader, ReplyPoller, create_ticket,
                                     list_faq, search_rows)
from reglament_agent.quality.mail import message_id_for, subject_for
from reglament_agent.quality.replies import ParsedMail, parse_raw, process_reply
from reglament_agent.quality.reply_text import extract_answer, html_to_text

EMB = FakeEmbedder()
USER = "ivan@corp.kz"
EXPERT = "quality@corp.kz"
QUESTION = "Можно ли выдать аванс подрядчику без договора?"
T0 = dt.datetime(2026, 11, 27, 10, 0, tzinfo=dt.timezone.utc)


def new_ticket(conn, mid="m1"):
    info = create_ticket(conn, user=UserContext(USER, "employee", "d1", mid), question=QUESTION,
                         restated="Аванс подрядчику без договора", reason="no_basis",
                         profile={"department": "Техническая дирекция"}, clarifications=[], candidates=[],
                         service_email="quality@demo.local", public_base_url="http://x")
    return info.number


def quoted(number):
    return (f"\n\nВ пт, 27 нояб. 2026 г. в 10:00, Регламент-агент <reglament-agent@demo.local> пишет:\n"
            f"> Обращение №{number}\n>\n> Вопрос:\n> {QUESTION}\n")


def mail(number=None, *, body="Нельзя, нужен договор.", sender=EXPERT, mid="<r1@corp.kz>", minutes=5,
         reply_to_ticket=True, subject=None, html=None, text=True):
    return ParsedMail(
        message_id=mid, from_addr=sender,
        subject=subject if subject is not None else (f"Re: {subject_for(number, QUESTION)}" if number else "Re: что-то"),
        text=body if text else "", html=html or "",
        in_reply_to=[message_id_for(number)] if number and reply_to_ticket else [],
        references=[message_id_for(number)] if number and reply_to_ticket else [],
        date=T0 + dt.timedelta(minutes=minutes))


@pytest.fixture
def conn(isolated):
    return isolated[0]


def run(conn, m, **kw):
    return process_reply(conn, m, MailSettings(**kw), EMB)


def answer_of(conn, number):
    return conn.execute("SELECT a.answer, a.expert_email, a.source, a.faq_id FROM ticket_answers a "
                        "JOIN tickets t ON t.id=a.ticket_id WHERE t.number=%s", (number,)).fetchone()


def test_expert_reply_with_quote_is_published(conn):
    n = new_ticket(conn)
    assert run(conn, mail(n, body="Нельзя, нужен договор." + quoted(n))) == "answered"
    answer, expert, source, faq_id = answer_of(conn, n)
    assert answer == "Нельзя, нужен договор." and QUESTION not in answer and "Вопрос" not in answer
    assert (expert, source) == (EXPERT, "email") and faq_id
    assert conn.execute("SELECT status FROM tickets WHERE number=%s", (n,)).fetchone()[0] == "answered"
    [row] = list_faq(conn, "active")
    assert row["answer"] == "Нельзя, нужен договор." and row["ticket_number"] == n
    assert conn.execute("SELECT department FROM faq_entries").fetchone()[0] == "Техническая дирекция"
    [note] = pending_notifications(conn, USER)
    assert note["ticket_number"] == n and note["payload"]["answer"] == "Нельзя, нужен договор."
    # the employee already got the expert's letter as a copy: no second letter from the outbox
    assert conn.execute("SELECT count(*) FROM mail_outbox WHERE kind='expert_answer'").fetchone()[0] == 0
    hits = search_rows(conn, "Аванс подрядчику без договора", EMB.embed(["Аванс подрядчику без договора"])[0],
                       department="Техническая дирекция")
    assert [h.faq_id for h in hits] == [faq_id]
    assert not search_rows(conn, "Аванс подрядчику без договора", EMB.embed(["Аванс подрядчику без договора"])[0],
                           department="Другое подразделение")


def test_match_by_in_reply_to_beats_subject_and_subject_only_works(conn):
    a, b = new_ticket(conn, "m1"), new_ticket(conn, "m2")
    # header points to ticket a, the subject names ticket b: headers win
    m = mail(a, mid="<h@x>")
    m.subject = f"Re: [Обращение №{b}] вопрос"
    assert run(conn, m) == "answered" and answer_of(conn, a) and answer_of(conn, b) is None
    # no headers at all: the ticket number in the subject is enough
    assert run(conn, mail(b, mid="<s@x>", reply_to_ticket=False)) == "answered"
    assert answer_of(conn, b)[0] == "Нельзя, нужен договор."


def test_reply_to_expert_answer_letter_matches_by_references(conn):
    n = new_ticket(conn)
    conn.execute("INSERT INTO mail_outbox (ticket_id, kind, to_addr, subject, body, message_id_header) "
                 "SELECT id,'expert_answer','a@b','s','b',%s FROM tickets WHERE number=%s",
                 (message_id_for(n, "expert_answer"), n))
    m = mail(None, subject="Re: без номера")
    m.references = [message_id_for(n, "expert_answer")]
    assert run(conn, m) == "answered"


def test_user_reply_is_not_an_answer(conn):
    n = new_ticket(conn)
    assert run(conn, mail(n, sender=USER.upper(), body="Спасибо, жду.")) == "user_reply"
    assert answer_of(conn, n) is None and list_faq(conn) == [] and pending_notifications(conn, USER) == []
    assert conn.execute("SELECT status FROM tickets").fetchone()[0] == "open"


def test_unknown_ticket_and_own_letters_ignored(conn):
    n = new_ticket(conn)
    assert run(conn, mail(None, subject="Re: [Обращение №2026-9999] что-то", mid="<u@x>")) == "unmatched"
    assert run(conn, mail(None, subject="Привет", mid="<v@x>")) == "unmatched"
    # our own ticket letter sits in the same mailbox and names the ticket in the subject
    own = mail(n, mid=message_id_for(n), reply_to_ticket=False, sender="reglament-agent@demo.local")
    assert run(conn, own) == "own_mail"
    assert answer_of(conn, n) is None and list_faq(conn) == []


def test_quality_address_list_restricts_senders(conn):
    n = new_ticket(conn)
    assert run(conn, mail(n, sender="stranger@evil.kz"), quality_addresses="quality@corp.kz") == "sender_rejected"
    assert run(conn, mail(n, mid="<ok@x>"), quality_addresses="Quality@corp.kz, other@corp.kz") == "answered"


def test_second_reply_replaces_first(conn):
    n = new_ticket(conn)
    assert run(conn, mail(n, body="Нельзя.", mid="<a@x>", minutes=1)) == "answered"
    faq_id = answer_of(conn, n)[3]
    assert run(conn, mail(n, body="Можно по гарантийному письму." + quoted(n), mid="<b@x>", minutes=9,
                          sender="head@corp.kz")) == "updated"
    answer, expert, _, same_faq = answer_of(conn, n)
    assert answer == "Можно по гарантийному письму." and expert == "head@corp.kz" and same_faq == faq_id
    [row] = list_faq(conn)
    assert row["answer"] == "Можно по гарантийному письму." and row["author_email"] == "head@corp.kz"
    assert conn.execute("SELECT count(*) FROM faq_entries").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM ticket_answers").fetchone()[0] == 1
    assert [x["payload"]["answer"] for x in pending_notifications(conn, USER)] == [
        "Нельзя.", "Можно по гарантийному письму."]


def test_late_arriving_older_reply_does_not_overwrite_newer(conn):
    n = new_ticket(conn)
    assert run(conn, mail(n, body="Новый ответ.", mid="<new@x>", minutes=30)) == "answered"
    assert run(conn, mail(n, body="Старый ответ.", mid="<old@x>", minutes=2)) == "superseded"
    assert answer_of(conn, n)[0] == "Новый ответ."


def test_same_message_twice_processed_once(conn):
    n = new_ticket(conn)
    m = mail(n, body="Нельзя." + quoted(n))
    assert run(conn, m) == "answered" and run(conn, m) == "duplicate"
    assert conn.execute("SELECT count(*) FROM mail_replies").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM faq_entries").fetchone()[0] == 1
    assert len(pending_notifications(conn, USER)) == 1


def test_empty_reply_and_closed_ticket(conn):
    n = new_ticket(conn)
    assert run(conn, mail(n, body="> только цитата\n> ещё", mid="<e@x>")) == "empty"
    assert answer_of(conn, n) is None
    conn.execute("UPDATE tickets SET status='closed_offtopic'")
    assert run(conn, mail(n, mid="<c@x>")) == "closed_ticket"


def test_html_only_reply(conn):
    n = new_ticket(conn)
    html = ("<html><head><style>p{color:red}</style></head><body><div>Нельзя,&nbsp;нужен <b>договор</b>.<br>"
            "Обратитесь в &laquo;ДБРЦТ&raquo;.</div><div class=\"gmail_quote\"><div>В пт пишет:</div>"
            f"<blockquote>Обращение №{n} {QUESTION}</blockquote></div></body></html>")
    assert run(conn, mail(n, text=False, html=html)) == "answered"
    answer = answer_of(conn, n)[0]
    assert answer == "Нельзя, нужен договор.\nОбратитесь в «ДБРЦТ»." and "<" not in answer and QUESTION not in answer


def test_signature_stripped(conn):
    n = new_ticket(conn)
    body = "Нельзя, нужен договор.\n\n-- \nПетрова А.\nСлужба качества\n+7 700 000 00 00" + quoted(n)
    assert run(conn, mail(n, body=body)) == "answered"
    assert answer_of(conn, n)[0] == "Нельзя, нужен договор."


@pytest.mark.parametrize("raw,expected", [
    ("Ответ.\n\n-----Original Message-----\nFrom: a\nSent: b\n\nвопрос", "Ответ."),
    ("Ответ.\n\nОт: Регламент-агент\nОтправлено: 27 ноября\nКому: quality\nТема: x\n\nвопрос", "Ответ."),
    ("Ответ.\n\nOn Fri, 27 Nov 2026, Bot <b@x> wrote:\n> q", "Ответ."),
    ("Ответ.\n\nВ пт, 27 нояб. 2026 г. в 10:00, Бот\n<b@x> пишет:\n> q", "Ответ."),
    ("Ответ.\n> вставка цитаты\nПродолжение.", "Ответ.\nПродолжение."),
    ("Ответ.\nОтправлено с iPhone\n\n> q", "Ответ."),
    ("Ответ. Дата: пятница.", "Ответ. Дата: пятница."),
    ("Ответ.\n\nОбращение №2026-0001\n\nВопрос:\nчто-то\n\nПричина обращения: x", "Ответ."),
])
def test_extract_answer_variants(raw, expected):
    assert extract_answer(raw) == expected


def test_html_to_text_drops_tags_and_quotes():
    assert html_to_text("<p>Да</p><blockquote>цитата<blockquote>вложенная</blockquote></blockquote><p>Нет</p>").split() == ["Да", "Нет"]


def test_parse_raw_prefers_plain_and_reads_headers():
    msg = EmailMessage()
    msg["From"] = "Эксперт <Quality@Corp.kz>"
    msg["Subject"] = "Re: [Обращение №2026-0001] x"
    msg["Message-ID"] = "<z@corp.kz>"
    msg["In-Reply-To"] = "<ticket-2026-0001@reglament-agent.local>"
    msg["References"] = "<a@b> <ticket-2026-0001@reglament-agent.local>"
    msg["Date"] = format_datetime(T0)
    msg.set_content("простой текст")
    msg.add_alternative("<p>html</p>", subtype="html")
    p = parse_raw(msg.as_bytes())
    assert (p.text.strip(), p.from_addr, p.message_id) == ("простой текст", "quality@corp.kz", "<z@corp.kz>")
    assert p.in_reply_to == ["<ticket-2026-0001@reglament-agent.local>"] and len(p.references) == 2
    assert extract_answer(p.text, p.html) == "простой текст" and p.date == T0


def raw_reply(n, mid, body, *, date=T0):
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Cc"] = EXPERT, "reglament-agent@demo.local", USER
    msg["Subject"] = f"Re: [Обращение №{n}] вопрос"
    msg["Message-ID"], msg["In-Reply-To"] = mid, message_id_for(n)
    msg["Date"] = format_datetime(date)
    msg.set_content(body)
    return msg.as_bytes()


def mailpit_transport(store):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/messages":
            return httpx.Response(200, json={"messages": [{"ID": i, "MessageID": m.decode().split("Message-ID: <")[1].split(">")[0]}
                                                          for i, m in store.items()]})
        i = path.split("/")[4]
        return httpx.Response(200, content=store[i])
    return httpx.MockTransport(handler)


def test_mailpit_reader_and_poller_end_to_end(isolated):
    conn, dsn = isolated
    n = new_ticket(conn)
    store = {"id1": raw_reply(n, "<p1@corp.kz>", "Нельзя, нужен договор.\n\n> Вопрос: " + QUESTION)}
    client = httpx.Client(transport=mailpit_transport(store))
    reader = MailpitReader("http://mailpit:8025", client=client)
    poller = ReplyPoller(dsn, MailSettings(), reader, embedder_factory=FakeEmbedder)
    assert asyncio.run(poller.run_once()) == {"answered": 1}
    assert asyncio.run(poller.run_once()) == {}  # known Message-ID: not even fetched again
    assert answer_of(conn, n)[0] == "Нельзя, нужен договор."
    store["id2"] = raw_reply(n, "<p2@corp.kz>", "Передумали: можно.", date=T0 + dt.timedelta(hours=1))
    assert asyncio.run(poller.run_once()) == {"updated": 1}
    assert answer_of(conn, n)[0] == "Передумали: можно."
    assert conn.execute("SELECT count(*) FROM faq_entries").fetchone()[0] == 1


def test_poller_retries_letter_when_model_is_down(isolated):
    conn, dsn = isolated
    n = new_ticket(conn)
    reader = MailpitReader("http://m", client=httpx.Client(
        transport=mailpit_transport({"i": raw_reply(n, "<q@x>", "Ответ.")})))

    def broken():
        raise RuntimeError("model down")
    assert asyncio.run(ReplyPoller(dsn, MailSettings(), reader, embedder_factory=broken).run_once()) == {}
    assert conn.execute("SELECT count(*) FROM mail_replies").fetchone()[0] == 0  # not marked processed
    assert asyncio.run(ReplyPoller(dsn, MailSettings(), reader, embedder_factory=FakeEmbedder).run_once()) == {"answered": 1}


def test_imap_reader_with_fake_server():
    raw = raw_reply("2026-0001", "<i1@corp.kz>", "Ответ по IMAP.")

    class FakeImap:
        def login(self, u, p): self.creds = (u, p)
        def select(self, folder, readonly=False): return "OK", [b"1"]
        def uid(self, cmd, *a):
            if cmd == "SEARCH":
                return "OK", [b"7 8"]
            return "OK", [(b"1 (BODY[] {10}", raw), b")"]
        def logout(self): pass

    reader = ImapReader("h", "u", "p", connect=FakeImap)
    mails = reader.fetch_new()
    assert len(mails) == 2 and mails[0].message_id == "<i1@corp.kz>"
    assert reader.fetch_new(lambda mid: True) == []


def test_default_poll_interval_is_five_seconds():
    assert MailSettings().reply_interval == 5.0


def test_trust_policy_logging(caplog):
    import logging
    caplog.set_level(logging.INFO, logger="reglament_agent.quality.replies")
    reader = MailpitReader("http://m", client=httpx.Client(transport=mailpit_transport({})))
    ReplyPoller("x", MailSettings(), reader).log_trust_policy()
    [rec] = caplog.records
    assert rec.levelno == logging.WARNING and "allow-list OFF" in rec.message
    caplog.clear()
    ReplyPoller("x", MailSettings(quality_addresses="quality@corp.kz, boss@corp.kz"), reader).log_trust_policy()
    [rec] = caplog.records
    assert rec.levelno == logging.INFO and "allow-list ON" in rec.message and "boss@corp.kz" in rec.message


def test_processing_latency_with_fake_transport(isolated):
    """Poll + processing (without the sleep between polls) stays far below the interval budget."""
    import time
    conn, dsn = isolated
    n = new_ticket(conn)
    reader = MailpitReader("http://m", client=httpx.Client(
        transport=mailpit_transport({"i": raw_reply(n, "<lat@x>", "Ответ.")})))
    t = time.monotonic()
    assert asyncio.run(ReplyPoller(dsn, MailSettings(), reader, embedder_factory=FakeEmbedder).run_once()) == {"answered": 1}
    assert time.monotonic() - t < 2.0
