"""admin-p03: model-proposed check questions for a document without a gold set (fake model)."""
import asyncio
import json
import tomllib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from reglament_agent.admin import check_proposals as P
from reglament_agent.admin import checks as C
from reglament_agent.admin import jobs, routes
from reglament_agent.chat import session as S
from reglament_agent.ingest.embedding import FakeEmbedder
from reglament_agent.ingest.model import DocumentCard
from reglament_agent.model.ollama import OllamaClient
from reglament_agent.model.queue import GenerationSlot
from reglament_agent.storage.publish import publish

DOCX = Path(__file__).resolve().parents[2] / "docx"
CARDS = tomllib.loads((DOCX / "documents.toml").read_text(encoding="utf-8"))["document"]
SECRET = "test-secret"
DOC = "dbrct-project"


class FakeModel:
    def __init__(self, data=None, exc=None):
        self.data, self.exc, self.calls = data, exc, []

    def __call__(self):
        return self

    async def chat_json(self, messages, schema, **kw):
        self.calls.append((messages, schema, kw))
        if self.exc:
            raise self.exc
        return SimpleNamespace(data=self.data)


@pytest.fixture
def env(isolated, monkeypatch, tmp_path):
    conn, dsn = isolated
    monkeypatch.setenv("DATABASE_URL", dsn)
    monkeypatch.setenv("CHAINLIT_AUTH_SECRET", SECRET)
    monkeypatch.setenv("EMBEDDER", "fake")
    monkeypatch.setenv("ADMIN_BACKGROUND", "0")
    monkeypatch.setenv("CHECKS_DIR", str(tmp_path))  # no gold set for any document
    monkeypatch.setattr(C, "spawn", lambda fn: fn())
    for card in CARDS:
        publish(conn, DocumentCard(card["doc_id"], card["title"], card["department"], tuple(card["stages"]),
                                   card["file"]), (DOCX / card["file"]).read_bytes(), FakeEmbedder(), "tester")
    return conn


def client(**install_kwargs):
    app = FastAPI()
    routes.install(app, **install_kwargs)
    c = TestClient(app, follow_redirects=False)
    c.cookies.set(S.COOKIE_NAME, S.sign_identity(S.Identity("expert@x.local", "expert"), SECRET))
    return c


def picked(conn):
    _, points = P.active_points(conn, DOC)
    return P.pick_points(points)


def q(point, n=0, text=None):
    return {"question": text or f"Как сотруднику поступить по пункту {point}? ({n})", "expected_point": point}


def propose(env, monkeypatch, data=None, exc=None):
    fake = FakeModel(data, exc)
    monkeypatch.setattr(P, "model_factory", fake)
    r = client().post(f"/admin/documents/{DOC}/checks/propose")
    return r, fake


def stored(conn):
    return conn.execute("SELECT question, expected_point, status FROM check_proposals ORDER BY id").fetchall()


def test_valid_proposals_are_shown(env, monkeypatch):
    pts = picked(env)
    r, fake = propose(env, monkeypatch, {"questions": [q(p["number"], i) for i, p in enumerate(pts)]})
    assert r.status_code == 200
    assert 3 <= len(pts) <= 5
    for i, p in enumerate(pts):
        assert q(p["number"], i)["question"] in r.text
        assert f"п. {p['number']}" in r.text
    assert "Предложить проверочные вопросы" in r.text and len(stored(env)) == len(pts)
    # the prompt states the key constraint and lists the numbered points
    system, user = fake.calls[0][0][0]["content"], fake.calls[0][0][1]["content"]
    assert "только в этом пункте" in system
    assert all(f"Пункт {p['number']}:" in user for p in pts)


def test_points_come_from_different_top_level_sections(env):
    pts = picked(env)
    tops = [p["number"].split(".")[0] for p in pts]
    assert len(set(tops)) == len(tops) == len(pts) and len(pts) >= 3


def test_pick_is_limited_to_five_and_deterministic():
    points = [{"number": f"{s}.1", "text": "Достаточно длинный текст пункта номер " + str(s) * 3, "position": s}
              for s in range(1, 12)]
    a = P.pick_points(points)
    assert len(a) == 5 and a == P.pick_points(points)
    assert [p["number"] for p in a][0] == "1.1" and [p["number"] for p in a][-1] == "11.1"


def test_unknown_or_not_offered_point_is_dropped(env, monkeypatch):
    pts = picked(env)
    _, all_points = P.active_points(env, DOC)
    offered = {p["number"] for p in pts}
    not_offered = next(p["number"] for p in all_points if p["number"] not in offered)
    data = {"questions": [q(pts[0]["number"], 0), q("99.99", 1), q(not_offered, 2), q("", 3)]}
    response, _ = propose(env, monkeypatch, data)
    assert "неполный набор" in response.text
    assert [r[:2] for r in stored(env)] == [(q(pts[0]["number"], 0)["question"], pts[0]["number"])]


def test_duplicates_empty_and_long_are_dropped(env, monkeypatch):
    a, b = (p["number"] for p in picked(env)[:2])
    data = {"questions": [q(a, 0, "  Сколько дней на согласование?  "), q(b, 1, "сколько дней на согласование"),
                          q(b, 2, "   "), {"question": None, "expected_point": b}, "мусор",
                          q(b, 3, "я" * 301), q(b, 4, "Кто утверждает?")]}
    propose(env, monkeypatch, data)
    assert [r[0] for r in stored(env)] == ["Сколько дней на согласование?", "Кто утверждает?"]


def test_limit_is_five():
    offered = {str(i) for i in range(1, 9)}
    data = {"questions": [q(str(i), i) for i in range(1, 9)]}
    assert len(P.validate(data, offered, offered)) == 5


@pytest.mark.parametrize("data", [{}, {"questions": "x"}, {"questions": []}, None, {"questions": [q("77.7")]}])
def test_nothing_valid_gives_message(env, monkeypatch, data):
    r, _ = propose(env, monkeypatch, data)
    assert r.status_code == 200 and "не предложила ни одного" in r.text and stored(env) == []


def test_model_failure_is_a_message_not_a_500(env, monkeypatch):
    r, _ = propose(env, monkeypatch, exc=ConnectionError("ollama down"))
    assert r.status_code == 200 and "недоступна" in r.text and stored(env) == []


def test_accept_stores_question_and_runs_with_checks(env, monkeypatch):
    p = picked(env)[0]["number"]
    propose(env, monkeypatch, {"questions": [q(p, 0, "Как быстро нужно ответить на заявку?")]})
    pid = env.execute("SELECT id FROM check_proposals").fetchone()[0]
    r = client().post(f"/admin/documents/{DOC}/checks/proposals/{pid}/accept")
    assert r.status_code == 200
    assert stored(env) == [("Как быстро нужно ответить на заявку?", p, "accepted")]
    cs = C.load_all(DOC)
    assert [(x.question, x.point, x.outcome) for x in cs.questions] == [
        ("Как быстро нужно ответить на заявку?", p, "answer")]
    assert cs.department == next(c["department"] for c in CARDS if c["doc_id"] == DOC)
    assert "Как быстро нужно ответить на заявку?" not in P.list_proposals(env, DOC).__str__()
    # the existing «run» mechanism takes it
    seen = []

    class Answerer:
        async def answer(self, user, message, on_status):
            seen.append(message)
            from reglament_agent.answer import Answer, Outcome
            return Answer(Outcome.NO_BASIS, "-")

    class Profiles:
        async def set_department(self, *a):
            pass

    monkeypatch.setattr(C, "services_factory", lambda: SimpleNamespace(answerer=Answerer(), profiles=Profiles()))
    assert client().post(f"/admin/documents/{DOC}/checks/run").status_code == 303
    assert seen == ["Как быстро нужно ответить на заявку?"]


def test_discard_removes_proposal(env, monkeypatch):
    p = picked(env)[0]["number"]
    propose(env, monkeypatch, {"questions": [q(p, 0)]})
    pid = env.execute("SELECT id FROM check_proposals").fetchone()[0]
    assert client().post(f"/admin/documents/{DOC}/checks/proposals/{pid}/discard").status_code == 303
    assert stored(env) == [] and C.load_all(DOC) is None


def test_accepted_question_is_not_proposed_again(env, monkeypatch):
    p = picked(env)[0]["number"]
    propose(env, monkeypatch, {"questions": [q(p, 0, "Кто утверждает?")]})
    pid = env.execute("SELECT id FROM check_proposals").fetchone()[0]
    client().post(f"/admin/documents/{DOC}/checks/proposals/{pid}/accept")
    propose(env, monkeypatch, {"questions": [q(p, 1, "кто утверждает")]})
    assert [r[2] for r in stored(env)] == ["accepted"]


def test_escaping(env, monkeypatch):
    p = picked(env)[0]["number"]
    r, _ = propose(env, monkeypatch, {"questions": [q(p, 0, "<script>alert(1)</script> Что делать?")]})
    assert "<script>alert(1)</script>" not in r.text and "&lt;script&gt;alert(1)&lt;/script&gt;" in r.text


def test_requires_expert_and_known_document(env, monkeypatch):
    anon = TestClient(FastAPI(), follow_redirects=False)
    app = FastAPI()
    routes.install(app)
    anon = TestClient(app, follow_redirects=False)
    assert anon.post(f"/admin/documents/{DOC}/checks/propose").status_code in (302, 303, 401, 403)
    monkeypatch.setattr(P, "model_factory", FakeModel({"questions": []}))
    assert client().post("/admin/documents/nope/checks/propose").status_code == 404


def test_only_one_question_per_offered_point():
    offered = {"1.1", "2.1", "3.1"}
    repeated = [q("1.1", i) for i in range(P.MAX_QUESTIONS)]
    rest = [q("2.1"), q("3.1")]
    assert P.validate({"questions": repeated + rest}, offered, offered) == [repeated[0], *rest]
    assert P.validate({"questions": repeated}, {"1.1"}, {"1.1"}) == [repeated[0]]


def test_single_eligible_section_is_complete(env, monkeypatch):
    version, points = P.active_points(env, DOC)
    point = P.pick_points(points)[0]
    monkeypatch.setattr(P, "active_points", lambda conn, doc_id: (version, [point]))
    proposals, message = P.propose(env, DOC, "expert", lambda messages: {"questions": [q(point["number"])]})
    assert message is None and [item["expected_point"] for item in proposals] == [point["number"]]


def publish_new_revision(conn):
    card = next(c for c in CARDS if c["doc_id"] == DOC)
    data = next((DOCX / "demo").glob("*v2*.docx")).read_bytes()
    return publish(conn, DocumentCard(DOC, card["title"], card["department"], tuple(card["stages"]),
                                      card["file"]), data, FakeEmbedder(), "tester")


def test_old_proposal_cannot_be_accepted_for_reused_point_number(env):
    pid = env.execute(
        "INSERT INTO check_proposals (doc_id, version_no, question, expected_point, created_by) "
        "VALUES (%s, 1, 'Какой срок?', '1.5', 'expert') RETURNING id", (DOC,)
    ).fetchone()[0]
    publish_new_revision(env)
    ver, points = P.active_points(env, DOC)
    assert ver == 2 and any(p["number"] == "1.5" and "5 рабочих дней" in p["text"] for p in points)
    response = client().post(f"/admin/documents/{DOC}/checks/proposals/{pid}/accept")
    assert stored(env) == [("Какой срок?", "1.5", "proposed")]
    assert response.status_code == 200 and "Предложение не принято" in response.text
    assert P.list_proposals(env, DOC) == []
    assert C.load_all(DOC) is None


def test_late_generation_preserves_newer_proposals(env):
    old_point = picked(env)[0]["number"]

    def slow_reply(messages):
        publish_new_revision(env)
        env.execute(
            "INSERT INTO check_proposals (doc_id, version_no, question, expected_point, created_by) "
            "VALUES (%s, 2, 'Вопрос новой редакции?', '1.5', 'expert')", (DOC,)
        )
        return {"questions": [q(old_point)]}

    _, message = P.propose(env, DOC, "expert", slow_reply)
    assert message and stored(env) == [("Вопрос новой редакции?", "1.5", "proposed")]


@pytest.mark.parametrize("injected", [True, False], ids=["chat-model", "standalone"])
def test_proposals_share_model_loop_slot_and_shutdown(env, monkeypatch, injected):
    question = q(picked(env)[0]["number"])
    loops = []

    async def respond(request):
        loops.append(asyncio.get_running_loop())
        if len(loops) == 2:
            return httpx.Response(503)
        return httpx.Response(200, json={"message": {"content": json.dumps({"questions": [question]})}})

    model = OllamaClient(slot=GenerationSlot(1, max_waiting=0), transport=httpx.MockTransport(respond))
    close = AsyncMock(wraps=model.aclose)
    monkeypatch.setattr(model, "aclose", close)
    creations = []

    def factory():
        creations.append(True)
        return model

    monkeypatch.setattr(P, "model_factory", factory)
    kwargs = {"proposal_model": model} if injected else {}
    url = f"/admin/documents/{DOC}/checks/propose"
    with client(**kwargs) as c:
        loop = c.portal.call(asyncio.get_running_loop)
        with c.portal.wrap_async_context_manager(model.slot.acquire()):
            busy = c.post(url)
            assert busy.status_code == 200 and "Очередь запросов к модели заполнена" in busy.text and loops == []
        assert question["question"] in c.post(url).text
        assert "ModelUnavailable" in c.post(url).text
        assert loops == [loop, loop] and model.slot.active == 0
        assert creations == ([] if injected else [True])
        close.assert_not_awaited()
    close.assert_awaited_once_with()
