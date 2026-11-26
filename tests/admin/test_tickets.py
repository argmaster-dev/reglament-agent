import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from reglament_agent.admin import routes
from reglament_agent.answer import UserContext
from reglament_agent.chat import session as S
from reglament_agent.chat.notifications import pending_notifications
from reglament_agent.ingest.embedding import FakeEmbedder
from reglament_agent.ingest.parser import load_cards
from reglament_agent.quality import create_ticket, recheck_faq_after_publication, search_rows
from reglament_agent.storage.publish import publish
from tests.test_storage import CORPUS, new_revision

SECRET = "test-secret"
CARD = load_cards(CORPUS)[0]


@pytest.fixture
def env(isolated, monkeypatch):
    conn, dsn = isolated
    monkeypatch.setenv("DATABASE_URL", dsn)
    monkeypatch.setenv("CHAINLIT_AUTH_SECRET", SECRET)
    monkeypatch.setenv("EMBEDDER", "fake")
    monkeypatch.setenv("ADMIN_BACKGROUND", "0")
    monkeypatch.setenv("MAIL_UI_URL", "http://mail.local:8025")
    return conn


def client(role=None):
    app = FastAPI()
    routes.install(app)
    c = TestClient(app, follow_redirects=False)
    if role:
        c.cookies.set(S.COOKIE_NAME, S.sign_identity(S.Identity(f"{role}@x.local", role), SECRET))
    return c


def make_ticket(conn, email="ivan@corp.kz", mid="m1", candidates=()):
    return create_ticket(conn, user=UserContext(email, "employee", "d1", mid),
                         question="Можно ли выдать аванс подрядчику без договора?",
                         restated="Аванс подрядчику без договора", reason="no_basis",
                         profile={"department": "Техническая дирекция"}, clarifications=[],
                         candidates=list(candidates), service_email="quality@demo.local",
                         public_base_url="http://x").number


def vec(q):
    return FakeEmbedder().embed([q])[0]


def test_access(env):
    assert client().get("/admin/tickets").headers["location"] == "/start"
    assert client("employee").get("/admin/tickets").status_code == 403
    assert client("employee").get("/admin/faq").status_code == 403
    assert client("employee").post("/admin/tickets/2026-0001", data={"answer": "x"}).status_code == 403


def test_list_nav_and_filters(env):
    n = make_ticket(env)
    c = client("expert")
    page = c.get("/admin/tickets").text
    assert n in page and "ivan@corp.kz" in page and "Обращения (1 открытых)" in page
    assert "Почта службы" in page and "http://mail.local:8025" in page
    assert n in c.get("/admin/tickets?status=open&reason=no_basis").text
    assert n not in c.get("/admin/tickets?status=answered").text
    assert n in c.get(f"/admin/tickets/{n}").text
    assert c.get("/admin/tickets/2099-0001").status_code == 404


def test_outside_faq_through_ui(env):
    n = make_ticket(env)
    c = client("expert")
    r = c.post(f"/admin/tickets/{n}", data={
        "action": "answer", "answer": "Нельзя, нужен договор.", "basis_kind": "outside",
        "visibility": "faq", "faq_question": "Аванс подрядчику без договора",
        "alt_questions": "Выдать аванс без договора?"})
    assert r.status_code == 303
    page = c.get(f"/admin/tickets/{n}").text
    assert "Ответ отправлен" in page and "/admin/faq/1" in page and "Обращения (0 открытых)" in page
    assert env.execute("SELECT status FROM tickets").fetchone()[0] == "answered"
    assert env.execute("SELECT to_addr FROM mail_outbox WHERE kind='expert_answer'").fetchone()[0] == "ivan@corp.kz"
    assert len(pending_notifications(env, "ivan@corp.kz")) == 1
    assert pending_notifications(env, "petr@corp.kz") == []
    entry = c.get("/admin/faq/1").text
    assert "в регламентах не закреплено" in entry and "expert@x.local" in entry
    assert [h.faq_id for h in search_rows(env, "Аванс подрядчику без договора",
                                               vec("Аванс подрядчику без договора"))] == [1]
    again = c.post(f"/admin/tickets/{n}", data={"action": "answer", "answer": "ещё", "basis_kind": "outside",
                                                "visibility": "private"})
    assert again.status_code == 400 and "уже обработано" in again.text
    assert "Аванс" in c.get("/admin/faq").text


def test_private_and_validation(env):
    n = make_ticket(env)
    c = client("expert")
    bad = c.post(f"/admin/tickets/{n}", data={"action": "answer", "answer": "x", "basis_kind": "points",
                                              "extra_points": "мусор", "visibility": "private"})
    assert bad.status_code == 400 and "Не удалось разобрать" in bad.text
    miss = c.post(f"/admin/tickets/{n}", data={"action": "answer", "answer": "x", "basis_kind": "points",
                                               "extra_points": "td-project п. 1.1", "visibility": "private"})
    assert miss.status_code == 400
    ok = c.post(f"/admin/tickets/{n}", data={"action": "answer", "answer": "Только вам.", "basis_kind": "outside",
                                             "visibility": "private"})
    assert ok.status_code == 303
    assert env.execute("SELECT count(*) FROM faq_entries").fetchone()[0] == 0
    assert search_rows(env, "Только вам", vec("Только вам")) == []


def test_point_basis_review_flow(env):
    emb = FakeEmbedder()
    data = (CORPUS / CARD.file).read_bytes()
    publish(env, CARD, data, emb, "t")
    n = make_ticket(env, candidates=[{"doc_id": "td-project", "point": "14.1", "title": "ТД", "version_no": 1}])
    c = client("expert")
    page = c.get(f"/admin/tickets/{n}").text
    assert "td-project|14.1" in page and "За организацию размещения заказов" in page
    r = c.post(f"/admin/tickets/{n}", data={
        "action": "answer", "answer": "Заказ размещает аккаунт-менеджер.", "basis_kind": "points",
        "cand": ["td-project|14.1"], "extra_points": "td-project п. 16.1", "visibility": "faq",
        "faq_question": "Кто размещает заказы на HW?"})
    assert r.status_code == 303
    assert [b["number"] for b in env.execute("SELECT basis FROM faq_entries").fetchone()[0]] == ["14.1", "16.1"]
    publish(env, CARD, new_revision(data), emb, "t")
    assert recheck_faq_after_publication(env, "td-project") == 1
    entry = c.get("/admin/faq/1").text
    assert "на проверке" in entry and "пункт изменён" in entry
    assert search_rows(env, "Кто размещает заказы на HW?", vec("Кто размещает заказы на HW?")) == []
    assert "Кто размещает" in c.get("/admin/faq?status=review").text
    assert c.post("/admin/faq/1/confirm").status_code == 303
    assert env.execute("SELECT status FROM faq_entries").fetchone()[0] == "active"
    c.post("/admin/faq/1/delete")
    assert env.execute("SELECT status FROM faq_entries").fetchone()[0] == "deleted"


def test_close_as_duplicate(env):
    n = make_ticket(env)
    c = client("expert")
    r = c.post(f"/admin/tickets/{n}", data={"action": "duplicate", "dup_ref": "2026-0001"})
    assert r.status_code == 303
    assert env.execute("SELECT status FROM tickets").fetchone()[0] == "closed_duplicate"


def test_employee_ticket_owner_sees_delivery_but_not_expert_context(env):
    number = make_ticket(env, email="employee@x.local")
    env.execute("UPDATE mail_outbox SET status='error', attempts=1, last_error='internal SMTP detail'")
    url = f"/tickets/{number}"
    assert client().get(url).headers["location"] == "/start"
    stranger = client("employee")
    stranger.cookies.set(S.COOKIE_NAME, S.sign_identity(S.Identity("other@x.local", "employee"), SECRET))
    assert stranger.get(url).status_code == 404
    assert stranger.get(url + "?role=expert&email=employee@x.local").status_code == 404

    own = client("employee").get(url)
    assert own.status_code == 200
    assert own.headers["cache-control"] == "no-store"
    assert "не доставлено" in own.text and "попыток: 1" in own.text
    assert "internal SMTP detail" not in own.text
    assert "Найденные кандидаты" not in own.text and "Почта службы" not in own.text
    assert client("expert").get(url).status_code == 200
    detail = client("expert").get(f"/admin/tickets/{number}")
    assert detail.status_code == 200
    assert "internal SMTP detail" in detail.text and "попыток: 1" in detail.text
