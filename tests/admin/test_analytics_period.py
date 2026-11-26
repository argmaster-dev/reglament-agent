"""Independent acceptance check where request age and ticket age differ."""
import asyncio
import os

from reglament_agent.answer import UserContext
from reglament_agent.dialog.route import Dialog
from tests.admin.test_analytics import A, add, call, client, data, env, rows
from tests.conftest import db_uri, isolated


def test_recent_forwarded_ticket_counts_even_if_original_request_is_older(data):
    rid = add(
        data, "Как согласовать договор по старому проекту?", sources=["14.1"], cands=["14.1"], days=10
    )
    ticket = asyncio.run(
        Dialog(os.environ["DATABASE_URL"]).rate(
            UserContext("u@x.local", "employee", "d", "feedback-now"),
            rid, False, "Не учтено правило", True,
        )
    )
    assert data.execute(
        """SELECT t.number FROM tickets t JOIN dialog_requests r
               ON r.id::text=substring(t.message_id from 10)
            WHERE t.created_at > now() - interval '7 days'
              AND r.created_at < now() - interval '7 days'"""
    ).fetchone() == (ticket.number,)
    expert = client("expert")
    a8 = expert.get("/admin/analytics/journal/by-ticket/" + ticket.number)
    assert a8.status_code == 303 and a8.headers["location"].endswith(rid)
    assert rows(expert.get("/admin/analytics/economics.csv?period=7").text) == []

    full = A.summary(data, "30")
    assert full["total"] == 1 and full["ticket_outcomes"] == 1
    week = A.summary(data, "7")
    section_tickets = sum(row["tickets"] for row in week["problems"] if row["section"] == "14")
    assert (week["total"], week["ticket_outcomes"], section_tickets) == (0, 1, 1)


def test_old_request_ticket_in_every_report_place(data):
    rid = add(data, "Старый вопрос", "no_basis", ticket="2026-0001", sources=["14.1"], cands=["14.1"], days=10)
    data.execute("INSERT INTO tickets(number,user_email,dialog_id,message_id,question,reason) "
                 "VALUES ('2026-0002','u@x.local','d','feedback:'||%s,'Старый вопрос','user_dispute')", (rid,))
    s = A.summary(data, "7")
    assert (s["total"], s["ticket_outcomes"], s["down"]) == (0, 1, 0)  # only the new dispute, once
    assert s["problems"][0]["tickets"] == 1 and s["unanswered"] == {}
    assert A.summary(data, "all")["ticket_outcomes"] == 2


def test_service_requests_excluded_everywhere(data):
    add(data, "Проверочный вопрос", "no_basis", user="quality-checks@service.local", ticket="2026-0003",
        sources=["14.1"], cands=["14.1"], rating=False, calls=[call(9, 9)])
    add(data, "Проверочный вопрос", "no_basis", user="quality-checks@service.local")
    add(data, "Настоящий вопрос", "answer", user="real@x.local")
    s = A.summary(data, "all")
    assert s["total"] == 1 and s["ticket_outcomes"] == 0 and s["down"] == 0 and s["repeated"] == []
    assert s["problems"] == [] and s["demand"] == [] and s["unanswered"] == {}
    assert [e["outcome"] for e in s["economics"]] == ["answer"]
    c = client("expert")
    assert [r["question"] for r in rows(c.get("/admin/analytics/requests.csv?period=all").text)] == ["Настоящий вопрос"]
    assert "Проверочный" not in c.get("/admin/analytics/journal?period=all").text
