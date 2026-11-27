"""Regression: forwarded 👎 tickets are counted in analytics (peer case)."""
import asyncio
import os

from reglament_agent.answer import UserContext
from reglament_agent.dialog.route import Dialog
from reglament_agent.quality.tickets import create_ticket
from tests.admin.test_analytics import A, add, client, data, env, rows
from tests.conftest import db_uri, isolated


def test_a7_includes_one_forwarded_ticket_in_its_problem_section(data):
    rid = add(data, "Как согласовать договор?", sources=["14.1"], cands=["14.1"])
    # An older request and its ticket stay outside the seven-day period.
    old_rid = add(data, "Как согласовать прошлый договор?", sources=["14.1"], cands=["14.1"], days=10)
    user = UserContext("u@x.local", "employee", "d", "feedback-ui")
    dialog = Dialog(os.environ["DATABASE_URL"])

    async def forward_twice():
        first = await dialog.rate(user, rid, False, "Не учтено правило", True)
        second = await dialog.rate(user, rid, False, "Не учтено правило", True)
        old = await dialog.rate(user, old_rid, False, "Не учтено правило", True)
        return first, second, old

    first, second, old = asyncio.run(forward_twice())
    assert first.number == second.number and old.number != first.number
    data.execute("UPDATE tickets SET created_at=now() - interval '10 days' WHERE number=%s", (old.number,))
    assert data.execute(
        "SELECT count(*) FROM tickets WHERE reason='user_dispute' AND created_at > now() - interval '7 days'"
    ).fetchone() == (1,)

    expert = client("expert")
    a8 = expert.get("/admin/analytics/journal/by-ticket/" + first.number)
    assert a8.status_code == 303 and a8.headers["location"].endswith(rid)
    journal = expert.get(a8.headers["location"])
    assert journal.status_code == 200 and "Как согласовать договор?" in journal.text

    a7 = A.summary(data, "7")
    section = next(row for row in a7["problems"] if row["section"] == "14")
    assert (a7["ticket_outcomes"], section["tickets"], section["down"]) == (1, 1, 1)


def test_a8_legacy_ticket_reaches_request_by_original_message_id(data):
    rid = add(data, "Старый вопрос о договоре", sources=["14.1"], days=10)
    legacy = create_ticket(
        data, user=UserContext("u@x.local", "employee", "d", rid),
        question="Старый вопрос о договоре", restated=None, reason="no_basis",
        profile={}, clarifications=[], candidates=[],
        service_email="quality@demo.local", public_base_url="http://localhost:8000",
    )
    assert data.execute(
        "SELECT answer->'ticket'->>'number' FROM dialog_requests WHERE id=%s", (rid,)
    ).fetchone() == (None,)

    url = "/admin/analytics/journal/by-ticket/" + legacy.number
    response = client("expert").get(url)
    assert response.status_code == 303 and response.headers["location"].endswith(rid)
    assert client("employee").get(url).status_code == 403


def test_same_ticket_not_counted_twice_but_dispute_is_separate(data):
    rid = add(data, "Вопрос", "no_basis", ticket="2026-0001", sources=["14.1"], cands=["14.1"])
    data.execute("UPDATE dialog_requests SET message_id='m1' WHERE id=%s", (rid,))
    # the ticket named in the answer, also linked by the same message_id: still one ticket
    data.execute("INSERT INTO tickets(number,user_email,dialog_id,message_id,question,reason) "
                 "VALUES ('2026-0001','u@x.local','d','m1','Вопрос','no_basis')")
    s = A.summary(data, "7")
    assert s["ticket_outcomes"] == 1 and s["problems"][0]["tickets"] == 1
    data.execute("INSERT INTO tickets(number,user_email,dialog_id,message_id,question,reason) "
                 "VALUES ('2026-0002','u@x.local','d','feedback:'||%s,'Вопрос','user_dispute')", (rid,))
    s = A.summary(data, "7")
    assert s["ticket_outcomes"] == 2 and s["problems"][0]["tickets"] == 2
    csv_row = rows(client("expert").get("/admin/analytics/requests.csv").text)[0]
    assert csv_row["ticket"] == "2026-0001;2026-0002"
    assert "2026-0002" in client("expert").get("/admin/analytics?period=7").text
