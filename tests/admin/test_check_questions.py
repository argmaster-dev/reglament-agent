"""admin-b04: check questions per regulation document (A10 steps 6 and 8, E2E-D12)."""
import asyncio
import os
import re
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from reglament_agent.admin import checks as C
from reglament_agent.admin import jobs, routes
from reglament_agent.answer import Answer, Outcome, SourceRef, Statement
from reglament_agent.chat import session as S
from reglament_agent.ingest.embedding import FakeEmbedder
from reglament_agent.ingest.model import DocumentCard
from reglament_agent.ingest.parser import parse_docx
from reglament_agent.storage.publish import publish

ROOT = Path(__file__).resolve().parents[2]
DOCX = ROOT / "docx"
DEMO = DOCX / "demo" / "ДБРЦТ v2 (демо-правка п. 1.5).docx"
CARDS = tomllib.loads((DOCX / "documents.toml").read_text(encoding="utf-8"))["document"]
SECRET = "test-secret"
EMB = FakeEmbedder()
DOC_IDS = [c["doc_id"] for c in CARDS]


def norm(text):
    return re.sub(r"\s+", " ", text).strip().lower()


@pytest.fixture
def env(isolated, monkeypatch):
    conn, dsn = isolated
    monkeypatch.setenv("DATABASE_URL", dsn)
    monkeypatch.setenv("CHAINLIT_AUTH_SECRET", SECRET)
    monkeypatch.setenv("EMBEDDER", "fake")
    monkeypatch.setenv("ADMIN_BACKGROUND", "0")
    monkeypatch.setattr(C, "spawn", lambda fn: fn())  # background work runs inline in tests
    return conn


def client(role=None):
    app = FastAPI()
    routes.install(app)
    c = TestClient(app, follow_redirects=False)
    if role:
        c.cookies.set(S.COOKIE_NAME, S.sign_identity(S.Identity(f"{role}@x.local", role), SECRET))
    return c


def load_corpus(conn):
    for card in CARDS:
        publish(conn, DocumentCard(card["doc_id"], card["title"], card["department"], tuple(card["stages"]),
                                   card["file"]), (DOCX / card["file"]).read_bytes(), EMB, "tester")


def point_text(conn, doc_id, number, version=None):
    return conn.execute(
        "SELECT p.text FROM points p JOIN document_versions v USING(version_id) "
        "WHERE v.doc_id=%s AND p.number=%s AND v.version_number=%s",
        (doc_id, number, version)).fetchone()[0]


class OverlapAnswerer:
    """Answers with the active point of the user's department that shares most words with the question."""

    def __init__(self, dsn):
        import psycopg
        self.dsn, self.dept, self.calls = dsn, {}, []
        self.psycopg = psycopg

    async def answer(self, user, message, on_status):
        self.calls.append((user, message))
        import psycopg
        with psycopg.connect(self.dsn) as conn:
            rows = conn.execute(
                "SELECT d.doc_id,d.title,d.department,v.version_number,p.number,p.text FROM documents d "
                "JOIN document_versions v ON v.version_id=d.active_version_id "
                "JOIN points p ON p.version_id=v.version_id WHERE d.status='active' AND d.department=%s",
                (self.dept.get(user.email),)).fetchall()
        stems = lambda t: {w[:5] for w in re.findall(r"\w{4,}", norm(t))}  # noqa: E731
        words = stems(message)
        best = max(rows, key=lambda r: len(words & stems(r[5])))
        if len(words & stems(best[5])) < 4:
            return Answer(Outcome.NO_BASIS, "В регламентах нет ответа.")
        ref = SourceRef(best[0], best[1], best[2], best[3], best[4], 0)
        return Answer(Outcome.ANSWER, "", [Statement(best[5].split("\n")[0], [ref])])


class FakeProfiles:
    def __init__(self, answerer):
        self.answerer = answerer

    async def set_department(self, email, department):
        self.answerer.dept[email] = department


def fake_services(dsn):
    answerer = OverlapAnswerer(dsn)
    return SimpleNamespace(answerer=answerer, profiles=FakeProfiles(answerer))


# --- task 1: check-question files vs the real parsed documents ------------------------------------

def test_every_document_has_check_file_with_5_to_7_questions():
    for doc_id in DOC_IDS:
        cs = C.load_questions(doc_id)
        assert 5 <= len(cs.questions) <= 7, doc_id
        assert cs.department == next(c["department"] for c in CARDS if c["doc_id"] == doc_id)
        text = (DOCX / "checks" / f"{doc_id}.toml").read_text(encoding="utf-8")
        assert "Проверочные вопросы службы качества; составлены по тексту регламента" in text.splitlines()[0]


@pytest.mark.parametrize("doc_id", DOC_IDS)
def test_expected_points_and_phrases_exist_in_parsed_document(doc_id):
    card = next(c for c in CARDS if c["doc_id"] == doc_id)
    parsed = parse_docx((DOCX / card["file"]).read_bytes())
    numbers = {p.number for p in parsed.points}
    cs = C.load_questions(doc_id)
    for q in cs.questions:
        assert q.outcome in {o.value for o in Outcome}
        if q.point is None:
            assert q.outcome != "answer" and q.phrase is None
            continue
        assert q.point in numbers, (doc_id, q.point)
        if q.phrase:
            assert norm(q.phrase) in norm(parsed.get(q.point).text), (doc_id, q.point, q.phrase)
    assert len({q.question for q in cs.questions}) == len(cs.questions)


def test_dbrct_has_presale_deadline_question_pointing_to_1_5():
    q = next(q for q in C.load_questions("dbrct-project").questions if q.point == "1.5")
    assert "заявк" in q.question.lower() and q.phrase == "3 рабочих дня"
    assert "3 рабочих дня" not in q.question  # not a copy of the point text


def test_unknown_document_has_no_questions():
    assert C.load_questions("nope") is None


# --- task 2: pass/fail computation ----------------------------------------------------------------

def mkq(point="1.5", outcome="answer", phrase="3 рабочих дня"):
    return C.Question("Вопрос?", point, outcome, phrase)


def ans(point, version=1, outcome=Outcome.ANSWER, doc="dbrct-project", text="Срок 3 рабочих дня."):
    sts = [Statement(text, [SourceRef(doc, "T", "Д", version, point, 0)])] if point else []
    return Answer(outcome, "", sts)


def test_evaluate_pass_and_each_failure_mode(env):
    load_corpus(env)
    ev = lambda q, a: C.evaluate(env, "dbrct-project", q, a, 0.5)  # noqa: E731
    ok = ev(mkq(), ans("1.5"))
    assert ok["passed"] and ok["found_points"] == ["1.5"] and ok["version_no"] == 1 and ok["phrase_found"] is True
    assert ok["outcome"] == "answer" and ok["seconds"] == 0.5
    wrong_point = ev(mkq(), ans("1.6", text="Срок."))
    assert not wrong_point["passed"] and wrong_point["found_points"] == ["1.6"]
    assert not ev(mkq(phrase=None), ans("1.6", text="Срок."))["passed"]  # point checked without a phrase too
    wrong_outcome = ev(mkq(), ans("1.5", outcome=Outcome.LOW_CONFIDENCE))
    assert not wrong_outcome["passed"]
    other_doc = ev(mkq(), ans("1.5", doc="td-project"))
    assert not other_doc["passed"] and other_doc["found_points"] == []
    # phrase absent from both the statement and the cited point text
    phrase = ev(mkq(phrase="7 рабочих дней"), ans("1.5", text="Срок указан."))
    assert not phrase["passed"] and phrase["phrase_found"] is False
    # phrase is taken from the cited point of the cited version
    fromdb = ev(mkq(), ans("1.5", text="Срок указан в регламенте."))
    assert fromdb["passed"] and fromdb["phrase_found"] is True
    nophrase = ev(mkq(phrase=None), ans("1.5", text="x"))
    assert nophrase["passed"] and nophrase["phrase_found"] is None


def test_evaluate_no_basis_expectation(env):
    load_corpus(env)
    q = C.Question("Про отпуск?", None, "no_basis", None)
    ev = lambda a: C.evaluate(env, "dbrct-project", q, a, 0)  # noqa: E731
    assert ev(Answer(Outcome.NO_BASIS))["passed"]
    assert not ev(ans("1.5"))["passed"]
    assert not ev(Answer(Outcome.ERROR, "модель недоступна"))["passed"]


# --- task 2/3: run, persistence, before/after --------------------------------------------------

def run(env, doc_id="dbrct-project", user="q@x.local"):
    services = fake_services(os.environ["DATABASE_URL"])
    run_id = C.start_run(doc_id, user)
    asyncio.run(C.execute_run(run_id, services))
    return run_id, services


def test_run_stores_results_and_two_last_runs(env):
    load_corpus(env)
    run_id, services = run(env)
    runs = C.last_runs(env, "dbrct-project", 2)
    assert [r["id"] for r in runs] == [run_id] and runs[0]["status"] == "done"
    res = runs[0]["results"]
    assert len(res) == len(C.load_questions("dbrct-project").questions)
    first = next(r for r in res if r["expected_point"] == "1.5")
    assert first["passed"] and first["found_points"] == ["1.5"] and first["version_no"] == 1
    assert runs[0]["version_no"] == 1
    # every call used a dedicated service identity, a fresh message id and the document's department
    users = [u for u, _ in services.answerer.calls]
    assert {u.role for u in users} == {"expert"} and len({u.message_id for u in users}) == len(users)
    assert len({u.email for u in users}) == 1 and users[0].email.startswith("quality-checks")
    assert set(services.answerer.dept.values()) == {"ДБРЦТ"}
    run2, _ = run(env)
    assert [r["id"] for r in C.last_runs(env, "dbrct-project", 2)] == [run2, run_id]


def test_failed_run_is_recorded(env, monkeypatch):
    load_corpus(env)
    run_id = C.start_run("dbrct-project", "q@x.local")

    class Boom:
        class answerer:  # noqa: N801
            @staticmethod
            async def answer(*a):
                raise RuntimeError("модель недоступна")

        profiles = FakeProfiles(SimpleNamespace(dept={}))

    asyncio.run(C.execute_run(run_id, Boom))
    r = C.last_runs(env, "dbrct-project", 1)[0]
    assert r["status"] == "done" and all(not x["passed"] and "модель недоступна" in x["error"] for x in r["results"])
    assert r["results"][0]["outcome"] == "error"


# --- routes -------------------------------------------------------------------------------------

def test_access_control_on_all_new_routes(env):
    load_corpus(env)
    for method, path in (("get", "/admin/documents/dbrct-project/checks"),
                         ("post", "/admin/documents/dbrct-project/checks/run")):
        assert getattr(client(), method)(path).status_code == 303
        assert getattr(client("employee"), method)(path).status_code == 403
    assert env.execute("SELECT count(*) FROM check_runs").fetchone()[0] == 0


def test_page_before_any_run_and_unknown_document(env, monkeypatch):
    load_corpus(env)
    html = client("expert").get("/admin/documents/dbrct-project/checks").text
    assert "Проверить" in html and "ещё не запускалась" in html
    assert client("expert").get("/admin/documents/nope/checks").status_code == 404


def test_check_button_runs_and_page_shows_table(env, monkeypatch):
    load_corpus(env)
    services = fake_services(os.environ["DATABASE_URL"])
    monkeypatch.setattr(C, "services_factory", lambda: services)
    c = client("expert")
    r = c.post("/admin/documents/dbrct-project/checks/run")
    assert r.status_code == 303 and r.headers["location"] == "/admin/documents/dbrct-project/checks"
    html = c.get("/admin/documents/dbrct-project/checks").text
    assert "п. 1.5" in html and "3 рабочих дня" in html and "Спросить в чате" in html
    assert "за сколько рабочих дней до отправки предложения" in html.lower()
    assert "версия 1" in html
    assert env.execute("SELECT started_by FROM check_runs").fetchone()[0] == "expert@x.local"


def test_running_run_polls_and_blocks_double_start(env, monkeypatch):
    load_corpus(env)
    monkeypatch.setattr(C, "spawn", lambda fn: None)  # worker never finishes
    c = client("expert")
    c.post("/admin/documents/dbrct-project/checks/run")
    c.post("/admin/documents/dbrct-project/checks/run")
    assert env.execute("SELECT count(*) FROM check_runs").fetchone()[0] == 1
    html = c.get("/admin/documents/dbrct-project/checks").text
    assert 'http-equiv="refresh"' in html and "Идёт проверка" in html


def test_links_from_documents_and_versions_pages(env):
    load_corpus(env)
    c = client("expert")
    assert "/admin/documents/dbrct-project/checks" in c.get("/admin/documents").text
    assert "/admin/documents/dbrct-project/checks" in c.get("/admin/documents/dbrct-project/versions").text


# --- E2E-D12 ------------------------------------------------------------------------------------

def test_d12_answer_changes_after_replacing_with_demo_v2(env, monkeypatch):
    load_corpus(env)
    services = fake_services(os.environ["DATABASE_URL"])
    monkeypatch.setattr(C, "services_factory", lambda: services)
    c = client("expert")
    c.post("/admin/documents/dbrct-project/checks/run")
    card = next(x for x in CARDS if x["doc_id"] == "dbrct-project")
    publish(env, DocumentCard(card["doc_id"], card["title"], card["department"], tuple(card["stages"]),
                              DEMO.name), DEMO.read_bytes(), EMB, "tester")
    c.post("/admin/documents/dbrct-project/checks/run")
    new, old = C.last_runs(env, "dbrct-project", 2)
    pick = lambda run: next(r for r in run["results"] if r["expected_point"] == "1.5")  # noqa: E731
    assert old["version_no"] == 1 and "3 рабочих дня" in pick(old)["answer_text"] and pick(old)["passed"]
    assert new["version_no"] == 2 and pick(new)["version_no"] == 2
    assert "5 рабочих дней" in pick(new)["answer_text"] and "3 рабочих дня" not in pick(new)["answer_text"]
    assert pick(new)["found_points"] == ["1.5"] and not pick(new)["passed"]  # expected phrase no longer holds
    others = [r for r in new["results"] if r["expected_point"] not in ("1.5", None)]
    assert all(r["version_no"] == 2 for r in others if r["found_points"])
    html = c.get("/admin/documents/dbrct-project/checks").text
    assert "Предыдущий прогон" in html and "версия 2" in html and "версия 1" in html
    assert "5 рабочих дней" in html and "3 рабочих дня" in html
