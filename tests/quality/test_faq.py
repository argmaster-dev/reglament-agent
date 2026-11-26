import pytest

from reglament_agent.answer import UserContext
from reglament_agent.chat.notifications import (mark_delivered, notification_markdown,
                                                pending_notifications)
from reglament_agent.ingest.embedding import FakeEmbedder
from reglament_agent.ingest.parser import load_cards
from reglament_agent.quality import (TicketError, answer_ticket, close_ticket, create_ticket, list_faq,
                                     recheck_faq_after_publication, search_rows, set_faq_status)
from reglament_agent.storage.publish import publish

from pathlib import Path

from tests.test_storage import CORPUS, new_revision

CARD = load_cards(CORPUS)[0]  # td-project
EMB = FakeEmbedder()


def ticket(conn, email="ivan@corp.kz", q="Можно ли выдать аванс подрядчику без договора?", mid="m1"):
    return create_ticket(conn, user=UserContext(email, "employee", "d1", mid), question=q,
                         restated="Аванс подрядчику без договора", reason="no_basis",
                         profile={"department": "Техническая дирекция"}, clarifications=[], candidates=[],
                         service_email="quality@demo.local", public_base_url="http://x").number


def ans(conn, number, **kw):
    args = dict(number=number, expert_email="exp@corp.kz", answer="Нельзя, нужен договор.",
                basis_kind="outside", basis_points=[], visibility="faq",
                faq_question="Аванс подрядчику без договора", alt_questions=["Выдать аванс без договора?"],
                embedder=EMB)
    args.update(kw)
    return answer_ticket(conn, **args)


def search(conn, q, **kw):
    return search_rows(conn, q, EMB.embed([q])[0], **kw)


@pytest.fixture
def conn(isolated):
    return isolated[0]


def publish_td(conn, revised=False):
    data = (CORPUS / CARD.file).read_bytes()
    return publish(conn, CARD, new_revision(data) if revised else data, EMB, "t")


def test_outside_faq_flow(conn):
    n = ticket(conn)
    r = ans(conn, n)
    assert r["faq_id"]
    assert conn.execute("SELECT status FROM tickets WHERE number=%s", (n,)).fetchone()[0] == "answered"
    mail = conn.execute("SELECT kind, to_addr, subject, body FROM mail_outbox WHERE kind='expert_answer'").fetchall()
    assert len(mail) == 1 and mail[0][1] == "ivan@corp.kz"
    assert mail[0][2] == f"[Обращение №{n}] Ответ службы качества" and "в регламентах не закреплено" in mail[0][3]
    assert [x["ticket_number"] for x in pending_notifications(conn, "ivan@corp.kz")] == [n]
    assert pending_notifications(conn, "other@corp.kz") == []
    hits = search(conn, "Аванс подрядчику без договора")
    assert [h.faq_id for h in hits] == [r["faq_id"]] and hits[0].department is None
    assert search(conn, "Выдать аванс без договора?", department="ДБРЦТ")  # outside: any department
    with pytest.raises(TicketError):
        ans(conn, n)
    assert len(list_faq(conn, "active")) == 1


def test_notification_helpers(conn):
    n = ticket(conn)
    ans(conn, n, visibility="private")
    [note] = pending_notifications(conn, "ivan@corp.kz")
    text = notification_markdown(note)
    assert f"Ответ службы качества · обращение №{n}" in text and "в регламентах не закреплено" in text
    assert mark_delivered(conn, [note["id"]]) == 1 and mark_delivered(conn, [note["id"]]) == 0
    assert pending_notifications(conn, "ivan@corp.kz") == []


def test_private_answer_not_indexed(conn):
    n = ticket(conn)
    r = ans(conn, n, visibility="private")
    assert r["faq_id"] is None
    assert conn.execute("SELECT count(*) FROM faq_entries").fetchone()[0] == 0
    assert search(conn, "Аванс подрядчику без договора") == []
    assert conn.execute("SELECT answer FROM ticket_answers").fetchone()[0].startswith("Нельзя")


def test_invalid_basis_and_atomicity(conn):
    publish_td(conn)
    n = ticket(conn)
    with pytest.raises(TicketError):
        ans(conn, n, basis_kind="points", basis_points=[("td-project", "999.9")])
    with pytest.raises(TicketError):
        ans(conn, n, basis_kind="points", basis_points=[])
    with pytest.raises(TicketError):
        ans(conn, n, answer="  ")
    assert conn.execute("SELECT status FROM tickets").fetchone()[0] == "open"
    assert conn.execute("SELECT count(*) FROM ticket_answers").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM mail_outbox WHERE kind='expert_answer'").fetchone()[0] == 0


def test_close_ticket(conn):
    n = ticket(conn)
    with pytest.raises(TicketError):
        close_ticket(conn, n, "exp@corp.kz", "duplicate", "")
    close_ticket(conn, n, "exp@corp.kz", "duplicate", "2026-0001")
    assert conn.execute("SELECT status FROM tickets").fetchone()[0] == "closed_duplicate"
    with pytest.raises(TicketError):
        ans(conn, n)
    assert pending_notifications(conn, "ivan@corp.kz")[0]["kind"] == "closed"


def test_point_basis_review_after_change(conn):
    publish_td(conn)
    n = ticket(conn)
    r = ans(conn, n, basis_kind="points", basis_points=[("td-project", "14.1")],
            faq_question="Кто выполняет логистику?", alt_questions=[])
    sha = conn.execute("SELECT sha256 FROM points WHERE number='14.1'").fetchone()[0]
    [item] = conn.execute("SELECT basis FROM faq_entries").fetchone()[0]
    assert item["sha256"] == sha and item["number"] == "14.1" and item["doc_id"] == "td-project"
    assert item["section_path"] and conn.execute("SELECT doc_id FROM faq_entries").fetchone()[0] == "td-project"
    assert [h.faq_id for h in search(conn, "Кто выполняет логистику?", department="Техническая дирекция")] == [r["faq_id"]]
    assert search(conn, "Кто выполняет логистику?", department="ДБРЦТ") == []
    publish_td(conn, revised=True)
    # fail-closed even before the re-check ran
    assert search(conn, "Кто выполняет логистику?") == []
    assert recheck_faq_after_publication(conn, "td-project") == 1
    assert conn.execute("SELECT status FROM faq_entries").fetchone()[0] == "review"
    assert search(conn, "Кто выполняет логистику?") == []
    with pytest.raises(ValueError):
        set_faq_status(conn, r["faq_id"], "bad")
    set_faq_status(conn, r["faq_id"], "active")  # expert confirmed against the new text
    assert [h.faq_id for h in search(conn, "Кто выполняет логистику?")] == [r["faq_id"]]
    set_faq_status(conn, r["faq_id"], "deleted")
    assert search(conn, "Кто выполняет логистику?") == []


def test_unchanged_renumbered_point_stays_active(conn):
    publish_td(conn)
    row = ("16.1",)  # the revision changes only 14.1
    n = ticket(conn)
    r = ans(conn, n, basis_kind="points", basis_points=[("td-project", row[0])],
            faq_question="Вопрос про другой пункт", alt_questions=[])
    assert conn.execute("SELECT basis->0->>'sha256' = (SELECT sha256 FROM points WHERE number='16.1') "
                        "FROM faq_entries").fetchone()[0]
    publish_td(conn, revised=True)
    conn.execute("UPDATE points SET number='99.9' WHERE number=%s AND version_id="
                 "(SELECT active_version_id FROM documents)", (row[0],))
    assert recheck_faq_after_publication(conn, "td-project") == 0
    assert conn.execute("SELECT status FROM faq_entries").fetchone()[0] == "active"
    assert conn.execute("SELECT basis->0->>'number' FROM faq_entries").fetchone()[0] == "99.9"
    assert [h.faq_id for h in search(conn, "Вопрос про другой пункт")] == [r["faq_id"]]


def test_document_basis_goes_to_review_on_replacement(conn):
    publish_td(conn)
    r = ans(conn, ticket(conn), basis_kind="document", basis_points=[("td-project", None)])
    assert recheck_faq_after_publication(conn, "td-project") == 0
    publish_td(conn, revised=True)
    assert recheck_faq_after_publication(conn, "td-project") == 1
    assert list_faq(conn, "review")[0]["id"] == r["faq_id"]


def test_retrieve_with_published_store(conn):
    from reglament_agent.quality import PublishedFaqStore
    from reglament_agent.storage.retrieve import retrieve
    publish_td(conn)
    q = "Кто размещает заказы на HW?"
    ans(conn, ticket(conn, mid="a"), faq_question=q, alt_questions=[])
    ans(conn, ticket(conn, mid="b", q="Секретный вопрос?"), visibility="private", answer="Приватно",
        faq_question=None)
    n3 = ticket(conn, mid="c")
    ans(conn, n3, basis_kind="points", basis_points=[("td-project", "14.1")],
        faq_question="Порядок размещения заказов", alt_questions=[])
    for question in (q, "Секретный вопрос?", "Порядок размещения заказов"):
        res = retrieve(conn, question, EMB.embed([question])[0], faq=PublishedFaqStore())
        assert all(h.source.faq_id for h in res.faq)
        assert not any("Приватно" in h.answer for h in res.faq)
    res = retrieve(conn, q, EMB.embed([q])[0], faq=PublishedFaqStore())
    assert res.faq[0].basis is None and res.faq[0].department is None
    res = retrieve(conn, "Порядок размещения заказов", EMB.embed(["Порядок размещения заказов"])[0],
                   faq=PublishedFaqStore(), department="Техническая дирекция")
    point_hit = next(h for h in res.faq if h.basis)
    assert point_hit.basis.point == "14.1" and point_hit.department == "Техническая дирекция"
    publish_td(conn, revised=True)
    recheck_faq_after_publication(conn, "td-project")
    res = retrieve(conn, "Порядок размещения заказов", EMB.embed(["Порядок размещения заказов"])[0],
                   faq=PublishedFaqStore())
    assert not any(h.basis for h in res.faq)  # review entry is gone
    assert len(res.faq) == 1  # only the outside entry remains
