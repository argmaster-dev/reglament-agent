import csv
import io
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from psycopg.types.json import Jsonb

from reglament_agent.admin import analytics as A
from tests.admin.test_tickets import client, env  # noqa: F401  (fixtures)

URLS = ["/admin/analytics", "/admin/analytics?period=all", "/admin/analytics/requests.csv",
        "/admin/analytics/economics.csv", "/admin/analytics/journal",
        "/admin/analytics/journal/00000000-0000-0000-0000-000000000000"]


def seed_docs(conn):
    conn.execute("INSERT INTO documents(doc_id,title,department,stages) VALUES ('td','Регламент ТД','ТД','{project}')")
    vid = conn.execute("INSERT INTO document_versions(doc_id,version_number,sha256,filename,uploaded_by,source_docx,stats)"
                       " VALUES ('td',1,'h','f.docx','e',''::bytea,'{}') RETURNING version_id").fetchone()[0]
    conn.execute("UPDATE documents SET active_version_id=%s WHERE doc_id='td'", (vid,))
    for pos, (num, sec, title) in enumerate([("14.1", "14", "Закупки"), ("14.2", "14", "Закупки"), ("3.1", "3", "Сроки")]):
        conn.execute("INSERT INTO points(doc_id,version_id,number,level,section_number,section_title,heading,text,blocks,"
                     "sentences,position,sha256,embedding) VALUES ('td',%s,%s,1,%s,%s,'h','t','[]','{}',%s,'s',"
                     "array_fill(0,ARRAY[1024])::vector)", (vid, num, sec, title, pos))


def src(point="14.1"):
    return {"doc_id": "td", "doc_title": "Регламент ТД", "department": "ТД", "version_no": 1, "point": point}


def add(conn, question, out="answer", *, days=0.0, rating=None, user="u@x.local", sources=(), cands=(),
        ms=1000, calls=(), ticket=None, restated=None, cache=False, dropped=()):
    answer = {"outcome": out, "summary": "итог", "from_cache": cache,
              "statements": [{"text": "t", "sources": [src(p) for p in sources]}] if sources else [],
              "variants": [], "ticket": {"number": ticket, "reason": "r", "delivery": "sent", "transferred": []} if ticket else None}
    trace = {"steps": ["cache_hit"] if cache else ["retrieve"], "calls": list(calls), "dropped": list(dropped),
             "elapsed_ms": ms, "candidates": [{"doc_id": "td", "title": "Регламент ТД", "point": p, "department": "ТД",
                                               "version": 1, "score": 0.5} for p in cands]}
    rid = str(uuid.uuid4())
    conn.execute("INSERT INTO dialog_requests(id,user_email,dialog_id,message_id,question,restated,answer,trace,created_at,rating)"
                 " VALUES (%s,%s,'d',%s,%s,%s,%s,%s,%s,%s)",
                 (rid, user, rid, question, restated or question, Jsonb(answer), Jsonb(trace),
                  datetime.now(timezone.utc) - timedelta(days=days), rating))
    return rid


def call(p, c, ms=100):
    return {"step": "extract", "kind": "chat", "model": "m", "prompt_tokens": p, "completion_tokens": c,
            "total_ms": ms, "queue_ms": 0, "ok": True}


def rows(text):
    return list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))


@pytest.fixture
def data(env):  # noqa: F811
    seed_docs(env)
    return env


def test_employee_refused_everywhere(data):
    emp, anon = client("employee"), client()
    for url in URLS:
        assert emp.get(url).status_code == 403, url
        assert anon.get(url).status_code == 303, url
    assert client("expert").get("/admin/analytics").status_code == 200


def test_counters_and_outcomes(data):
    for ms in (1000, 3000, 2000):
        add(data, f"q{ms}", "answer", ms=ms, rating=True if ms == 1000 else None)
    add(data, "нет", "no_basis", rating=False, ticket="2026-0001")
    add(data, "оффтоп", "off_topic")
    add(data, "сбой", "error")
    add(data, "уточни", "clarify")
    s = A.summary(data, "all")
    assert s["total"] == 7 and s["counts"]["answer"] == 3 and s["counts"]["no_basis"] == 1
    assert s["ticket_outcomes"] == 1 and (s["up"], s["down"]) == (1, 1)
    assert s["median_ms"] == 1000
    page = client("expert").get("/admin/analytics?period=all").text
    assert "Вопросов:</b> 7" in page and "Нет в регламентах" in page and "Рекомендации" in page


def test_period_filter(data):
    add(data, "свежий", days=1)
    add(data, "месяц", days=20)
    add(data, "давний", days=100)
    c = client("expert")
    assert A.summary(data, "7")["total"] == 1
    assert A.summary(data, "30")["total"] == 2
    assert A.summary(data, "all")["total"] == 3
    assert [r["question"] for r in rows(c.get("/admin/analytics/requests.csv?period=30").text)] == ["свежий", "месяц"]
    assert len(rows(c.get("/admin/analytics/requests.csv?period=all").text)) == 3
    assert len(rows(c.get("/admin/analytics/requests.csv?period=bogus").text)) == 1  # falls back to 7 days


def test_repeated_questions_grouped(data):
    add(data, "Сколько дней на согласование договора?")
    add(data, "согласование договора: сколько дней", restated="Сколько дней на согласование договора")
    add(data, "Сколько дней на согласование договора???", user="v@x.local")
    add(data, "Совсем другой вопрос")
    g = A.summary(data, "all")["repeated"]
    assert len(g) == 1 and g[0]["count"] == 3 and g[0]["users"] == 2
    assert A.normalize("Договор, согласование!") == A.normalize("согласование ДОГОВОР")
    assert "формулировки" in client("expert").get("/admin/analytics?period=all").text


def test_unanswered_by_reason(data):
    add(data, "а", "no_basis", ticket="2026-0001")
    add(data, "б", "external_doc")
    add(data, "в", "external_doc")
    add(data, "г", "answer")
    u = A.summary(data, "all")["unanswered"]
    assert list(u) == ["no_basis", "external_doc"] and len(u["external_doc"]) == 2
    assert "answer" not in u


def test_problem_sections_and_demand(data):
    add(data, "а", "no_basis", ticket="2026-0001", cands=["14.1", "3.1"])
    add(data, "б", "clarify", cands=["14.2"])
    add(data, "в", "answer", rating=False, sources=["14.1", "14.2"])
    add(data, "г", "answer", sources=["3.1"])
    s = A.summary(data, "all")
    p = {x["section"]: x for x in s["problems"]}
    assert (p["14"]["tickets"], p["14"]["clarify"], p["14"]["down"]) == (1, 1, 1)  # 14.1 and 14.2 share a section
    assert (p["3"]["tickets"], p["3"]["down"]) == (1, 0)
    assert p["14"]["title"] == "Закупки" and p["14"]["doc_title"] == "Регламент ТД"
    d = {x["section"]: x["count"] for x in s["demand"]}
    assert d == {"14": 1, "3": 1}
    assert "Закупки" in client("expert").get("/admin/analytics?period=all").text


def test_recommendation_threshold(data):
    for i in range(A.REC_MIN_SECTION_EVENTS - 1):
        add(data, f"вопрос {i}", "no_basis", cands=["14.1"], ticket=f"2026-000{i}", days=1)
    assert A.summary(data, "7")["recommendations"] == []
    assert "Порог рекомендаций не достигнут" in client("expert").get("/admin/analytics").text
    add(data, "ещё один вопрос", "no_basis", cands=["14.1"], ticket="2026-0099", days=1)
    rec = A.summary(data, "7")["recommendations"]
    assert rec == [f"Раздел 14 «Закупки» документа «Регламент ТД»: {A.REC_MIN_SECTION_EVENTS} обращений за 7 дней "
                   f"(тикеты {A.REC_MIN_SECTION_EVENTS}, 👎 0, уточнения 0) — дополнить регламент или FAQ"]
    assert "дополнить регламент или FAQ" in client("expert").get("/admin/analytics").text
    assert A.summary(data, "all")["recommendations"][0].count("за всё время") == 1


def test_recommendation_ignores_old_events_in_short_period(data):
    for i in range(A.REC_MIN_SECTION_EVENTS):
        add(data, f"в{i}", "no_basis", cands=["14.1"], days=40)
    assert A.summary(data, "7")["recommendations"] == []
    assert len(A.summary(data, "all")["recommendations"]) == 1


def test_economics(data):
    add(data, "a", "answer", ms=1000, calls=[call(100, 10), call(300, 30)])
    add(data, "b", "answer", ms=3000, calls=[call(200, 20)], cache=True)
    add(data, "c", "no_basis", ms=500, calls=[call(50, 5)])
    e = {x["outcome"]: x for x in A.summary(data, "all")["economics"]}
    a = e["answer"]
    assert (a["avg_prompt"], a["avg_completion"], a["avg_calls"]) == (300, 30, 1.5)
    assert a["cache_share"] == 0.5 and a["median_ms"] == 2000 and a["p90_ms"] == 3000
    ec = {r["outcome"]: r for r in rows(client("expert").get("/admin/analytics/economics.csv?period=all").text)}
    assert ec["answer"]["avg_prompt_tokens"] == "300.0" and ec["answer"]["cache_hit_share"] == "0.5"
    assert ec["no_basis"]["requests"] == "1" and ec["no_basis"]["p90_ms"] == "500"


def test_requests_csv_contents(data):
    add(data, "=SUM(A1)", "answer", calls=[call(10, 2), call(5, 1)], rating=False, ticket="2026-0007", ms=1500)
    r = client("expert").get("/admin/analytics/requests.csv?period=all")
    assert r.headers["content-type"].startswith("text/csv") and "attachment" in r.headers["content-disposition"]
    row = rows(r.text)[0]
    assert row["question"] == "'=SUM(A1)"  # no formula injection
    assert (row["outcome"], row["ticket"], row["rating"], row["elapsed_ms"]) == ("answer", "2026-0007", "down", "1500")
    assert (row["prompt_tokens"], row["completion_tokens"], row["model_calls"]) == ("15", "3", "2")


def test_journal_list_and_detail(data):
    env = data
    rid = add(env, "Как оформить аванс?", "no_basis", ticket="2026-0003", sources=["14.1"], cands=["14.1"],
              calls=[call(120, 12, 345)], dropped=["topic_mismatch"], ms=2500)
    env.execute("INSERT INTO tickets(number,user_email,dialog_id,question,reason,status) "
                "VALUES ('2026-0003','u@x.local','d','Как оформить аванс?','no_basis','open')")
    c = client("expert")
    listing = c.get("/admin/analytics/journal?period=all").text
    assert f"/admin/analytics/journal/{rid}" in listing and "Как оформить аванс?" in listing
    page = c.get(f"/admin/analytics/journal/{rid}").text
    for needle in ("retrieve", "Регламент ТД", "14.1", "extract", "120", "12", "345", "topic_mismatch",
                   "Нет в регламентах", "2026-0003", "2.5 с"):
        assert needle in page, needle
    assert c.get("/admin/analytics/journal/not-a-uuid").status_code == 404
    assert "Журнал запросов" in c.get("/admin/analytics?period=all").text


def test_nav_link(data):
    assert 'href="/admin/analytics"' in client("expert").get("/admin/tickets").text


# --- round 2 ---------------------------------------------------------------------------------

def _group_hash(conn):
    return A.summary(conn, "all")["repeated"][0]["hash"]


def test_create_faq_from_repeated_group(data):
    add(data, "Сколько дней на согласование договора?")
    add(data, "согласование договора: сколько дней", user="v@x.local")
    h = _group_hash(data)
    page = client("expert").get("/admin/analytics?period=all").text
    assert f'name="group" value="{h}"' in page and "Создать FAQ" in page
    c = client("expert")
    assert c.get("/admin/analytics/faq").status_code in (404, 405)
    r = c.post("/admin/analytics/faq", data={"group": h, "period": "all"})
    assert r.status_code == 303 and r.headers["location"].startswith("/admin/tickets/")
    again = c.post("/admin/analytics/faq", data={"group": h, "period": "all"})
    assert again.headers["location"] == r.headers["location"]
    assert data.execute("SELECT count(*) FROM tickets").fetchone()[0] == 1
    q, mid = data.execute("SELECT question, message_id FROM tickets").fetchone()
    assert mid == f"analytics:{h}" and "договор" in q
    assert c.post("/admin/analytics/faq", data={"group": "nope", "period": "all"}).status_code == 404
    assert client("employee").post("/admin/analytics/faq", data={"group": h}).status_code == 403
    assert client().post("/admin/analytics/faq", data={"group": h}).status_code == 303
    assert data.execute("SELECT count(*) FROM tickets").fetchone()[0] == 1


def test_journal_by_ticket(data):
    rid = add(data, "Вопрос", "no_basis", ticket="2026-0005")
    c = client("expert")
    r = c.get("/admin/analytics/journal/by-ticket/2026-0005")
    assert r.status_code == 303 and r.headers["location"] == f"/admin/analytics/journal/{rid}"
    assert c.get("/admin/analytics/journal/by-ticket/2026-9999").status_code == 404
    assert c.get("/admin/analytics/journal/by-ticket/x' OR '1'='1").status_code == 404
    assert client("employee").get("/admin/analytics/journal/by-ticket/2026-0005").status_code == 403


def test_ticket_card_links_to_journal(data):
    from tests.admin.test_tickets import make_ticket
    n = make_ticket(data)
    assert f"/admin/analytics/journal/by-ticket/{n}" in client("expert").get(f"/admin/tickets/{n}").text


def test_dropped_codes_in_russian(data):
    codes = ["unknown_ref", "actor_missing", "action_missing", "actor_not_bound", "negation_mismatch",
             "column_mismatch", "number_missing", "topic_mismatch"]
    assert all(A.DROPPED[c] and A.DROPPED[c] != c for c in codes)
    rid = add(data, "в", "answer", dropped=[*codes, "brand_new_code"], calls=[call(1, 1), {**call(1, 0), "kind": "embed"}])
    page = client("expert").get(f"/admin/analytics/journal/{rid}").text
    for c in codes:
        assert A.DROPPED[c] in page
    assert "brand_new_code" in page
    assert "включая embed" in page and "Чат-вызовов: 1" in page
    eco = client("expert").get("/admin/analytics?period=all").text
    assert "включают embed" in eco and "Чат-вызовов (ср.)" in eco
    assert rows(client("expert").get("/admin/analytics/economics.csv?period=all").text)[0]["avg_chat_calls"] == "1.0"


# --- round 3 ---------------------------------------------------------------------------------

def test_feedback_ticket_links_to_journal(data):
    import asyncio
    import os

    from reglament_agent.answer import UserContext
    from reglament_agent.dialog.route import Dialog
    rid = add(data, "Правило оплаты", sources=["14.1"], calls=[call(41, 7)])
    n = asyncio.run(Dialog(os.environ["DATABASE_URL"]).rate(
        UserContext("u@x.local", "employee", "d", "feedback-ui"), rid, False, "Неполно", True)).number
    c = client("expert")
    card = c.get("/admin/tickets/" + n)
    assert "/admin/analytics/journal/by-ticket/" + n in card.text
    r = c.get("/admin/analytics/journal/by-ticket/" + n)
    assert r.status_code == 303 and r.headers["location"] == f"/admin/analytics/journal/{rid}"


def test_negation_keeps_groups_apart(data):
    add(data, "Договор подписан?")
    add(data, "Договор не подписан?")
    assert A.summary(data, "all")["repeated"] == []
    for q in ("Кто подписывает договор?", "договор кто подписывает", "Кто подписывает договор!!"):
        add(data, q)
    g = A.summary(data, "all")["repeated"]
    assert len(g) == 1 and g[0]["count"] == 3
    assert "не" not in A._STOP and "нет" not in A._STOP and "без" not in A._STOP and "ни" not in A._STOP
