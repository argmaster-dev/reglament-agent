"""admin-b01: versions, withdraw, rollback, diff and FAQ review (E2E-D03..D09, D12 data)."""
import importlib.util
import re
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from reglament_agent.admin import jobs, routes
from reglament_agent.admin import versions as V
from reglament_agent.answer import UserContext
from reglament_agent.chat import session as S
from reglament_agent.ingest.embedding import FakeEmbedder
from reglament_agent.ingest.model import DocumentCard
from reglament_agent.ingest.parser import parse_docx
from reglament_agent.quality import (answer_ticket, create_ticket, recheck_faq_after_publication,
                                     search_rows)
from reglament_agent.storage.publish import publish
from reglament_agent.storage.search import search

ROOT = Path(__file__).resolve().parents[2]
DBRCT = ROOT / "docx" / "Проектная деятельность ДБРЦТ.docx"
DEMO = ROOT / "docx" / "demo" / "ДБРЦТ v2 (демо-правка п. 1.5).docx"
SECRET = "test-secret"
EMB = FakeEmbedder()
CARD = DocumentCard("doc-x", "Тестовый регламент", "Отдел", ("project",), "x.docx")


@pytest.fixture
def env(isolated, monkeypatch):
    conn, dsn = isolated
    monkeypatch.setenv("DATABASE_URL", dsn)
    monkeypatch.setenv("CHAINLIT_AUTH_SECRET", SECRET)
    monkeypatch.setenv("EMBEDDER", "fake")
    monkeypatch.setenv("ADMIN_BACKGROUND", "0")
    return conn


def client(role=None):
    app = FastAPI()
    routes.install(app)
    c = TestClient(app, follow_redirects=False)
    if role:
        c.cookies.set(S.COOKIE_NAME, S.sign_identity(S.Identity(f"{role}@x.local", role), SECRET))
    return c


def docx(*items):
    """Minimal DOCX: strings are paragraphs, (headers, rows) tuples are tables."""
    xml = '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
    p = lambda t: f"<w:p><w:r><w:t>{t}</w:t></w:r></w:p>"  # noqa: E731
    for item in items:
        if isinstance(item, str):
            xml += p(item)
        else:
            headers, rows = item
            xml += "<w:tbl>" + "".join("<w:tr>" + "".join(f"<w:tc>{p(c)}</w:tc>" for c in row) + "</w:tr>"
                                       for row in [headers, *rows]) + "</w:tbl>"
    out = BytesIO()
    with ZipFile(out, "w") as z:
        z.writestr("word/document.xml", xml + "</w:body></w:document>")
    return out.getvalue()


def pub(conn, data, card=CARD):
    result = publish(conn, card, data, EMB, "tester")
    recheck_faq_after_publication(conn, card.doc_id)
    return result


def faq(conn, question, kind="points", points=(("doc-x", "1.2"),), mid=None):
    n = create_ticket(conn, user=UserContext("ivan@corp.kz", "employee", "d1", mid or question),
                      question=question, restated=question, reason="no_basis", profile={},
                      clarifications=[], candidates=[], service_email="q@demo.local",
                      public_base_url="http://x").number
    return answer_ticket(conn, number=n, expert_email="exp@corp.kz", answer="Ответ эксперта.",
                         basis_kind=kind, basis_points=list(points), visibility="faq",
                         faq_question=question, alt_questions=[], embedder=EMB)["faq_id"]


def expected(conn):
    return {"expected_version_id": conn.execute("SELECT active_version_id FROM documents").fetchone()[0]}


def status(conn, faq_id):
    return conn.execute("SELECT status FROM faq_entries WHERE id=%s", (faq_id,)).fetchone()[0]


def found(conn, question):
    return [r.faq_id for r in search_rows(conn, question, EMB.embed([question])[0])]


def hits(conn, text):
    return search(conn, text, EMB.embed([text])[0])


BASE = docx("Регламент", "1. Раздел", "1.1. Срок подачи заявки составляет три дня.",
            "1.2. Согласование выполняет руководитель отдела.", "1.3. Архив хранится пять лет.")


# --- demo file (requirement 6) ------------------------------------------------------------------

def test_demo_v2_changes_only_point_1_5(tmp_path):
    spec = importlib.util.spec_from_file_location("make_demo_v2", ROOT / "scripts" / "make_demo_v2.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    out = mod.make(mod.source_path(), tmp_path / "v2.docx")
    for path in (out, DEMO):
        old, new = parse_docx(DBRCT.read_bytes()), parse_docx(path.read_bytes())
        assert [p.number for p in old.points] == [p.number for p in new.points]
        assert [a.number for a, b in zip(old.points, new.points) if a.text != b.text] == ["1.5"]
        assert "5 рабочих дней" in new.get("1.5").text and "3 рабочих дня" not in new.get("1.5").text
    with ZipFile(DBRCT) as a, ZipFile(out) as b:
        assert a.namelist() == b.namelist()
        assert [n for n in a.namelist() if a.read(n) != b.read(n)] == ["word/document.xml"]
    assert "демонстрационная правка" in (ROOT / "docx" / "demo" / "README.md").read_text(encoding="utf-8").lower()


# --- diff (requirement 4) -----------------------------------------------------------------------

def test_word_diff_is_escaped_and_word_level():
    left, right = V.word_diff("срок 3 рабочих дня <b>", "срок 5 рабочих дней <b>")
    assert "<del>3</del>" in left and "<ins>5</ins>" in right and "<del>дня</del>" in left
    assert "&lt;b&gt;" in left and "<b>" not in str(left) and "<b>" not in str(right)


def test_d04_renumbering_is_not_a_change(env):
    pub(env, BASE)
    f = faq(env, "Кто согласует?")
    swapped = docx("Регламент", "1. Раздел", "1.1. Согласование выполняет руководитель отдела.",
                   "1.2. Срок подачи заявки составляет три дня.", "1.3. Архив хранится пять лет.")
    pub(env, swapped)
    diff = V.diff_versions(env, "doc-x", 1, 2)
    assert diff["changed"] == [] and diff["added"] == [] and diff["removed"] == []
    assert {(r["old"], r["new"]) for r in diff["renumbered"]} == {("1.1", "1.2"), ("1.2", "1.1")}
    assert status(env, f) == "active" and found(env, "Кто согласует?") == [f]
    basis = env.execute("SELECT basis FROM faq_entries").fetchone()[0]
    assert basis[0]["number"] == "1.1"
    page = client("expert").get("/admin/documents/doc-x/versions/diff?old=1&new=2").text
    assert "перенумерован" in page and "п. 1.2 → п. 1.1" in page


def test_d05_table_cell_moved_between_columns(env):
    h = ["Обязательно", "При наличии"]
    pub(env, docx("Регламент", "1. Раздел", "1.1. Документы:", (h, [["Договор", ""], ["", "Смета"]])))
    f = faq(env, "Какие документы нужны?", points=[("doc-x", "1.1")])
    pub(env, docx("Регламент", "1. Раздел", "1.1. Документы:", (h, [["", "Договор"], ["", "Смета"]])))
    assert status(env, f) == "review" and found(env, "Какие документы нужны?") == []
    diff = V.diff_versions(env, "doc-x", 1, 2)
    assert [c["number"] for c in diff["changed"]] == ["1.1"] and diff["renumbered"] == []
    assert "<ins>" in str(diff["changed"][0]["new_html"])


def test_diff_added_removed_changed(env):
    pub(env, BASE)
    pub(env, docx("Регламент", "1. Раздел", "1.1. Срок подачи заявки составляет пять дней.",
                  "1.2. Согласование выполняет руководитель отдела.", "1.4. Новый пункт."))
    diff = V.diff_versions(env, "doc-x", 1, 2)
    assert [c["number"] for c in diff["changed"]] == ["1.1"]
    assert [a["number"] for a in diff["added"]] == ["1.4"] and [a["number"] for a in diff["removed"]] == ["1.3"]
    page = client("expert").get("/admin/documents/doc-x/versions/diff?old=1&new=2").text
    assert "<del>три</del>" in page and "<ins>пять</ins>" in page


# --- replace with FAQ (D03, D06, job preview) ----------------------------------------------------

def upload(c, path, **extra):
    data = {"target": "new", "department_select": "__new__", "department_new": "ДБРЦТ",
            "stages": ["presale", "project"], **extra}
    return c.post("/admin/documents/upload", data=data,
                  files={"file": (path.name, path.read_bytes(), "application/octet-stream")})


def job_of(response):
    return int(re.search(r"/admin/jobs/(\d+)/publish", response.text).group(1))


def publish_via_job(c, response):
    job = job_of(response)
    c.post(f"/admin/jobs/{job}/publish", data={})
    jobs.run_job(job)
    return job


def test_d03_replace_dbrct_with_demo_v2(env):
    c = client("expert")
    publish_via_job(c, upload(c, DBRCT))
    doc_id = env.execute("SELECT doc_id FROM documents").fetchone()[0]
    question = "За сколько рабочих дней нужно инициировать заявку?"
    f = faq(env, question, points=[(doc_id, "1.5")])
    assert found(env, question) == [f]
    term = "заявка инициирована не менее чем за рабочих дней до подачи предложения"
    assert "3 рабочих дня" in hits(env, term)[0].text

    second = upload(c, DEMO, target=doc_id)
    preview = c.get(f"/admin/jobs/{job_of(second)}/changes").text
    assert "п. 1.5" in preview and question in preview and "<ins>5</ins>" in preview
    assert status(env, f) == "active"  # preview publishes nothing
    assert env.execute("SELECT count(*) FROM document_versions").fetchone()[0] == 1
    publish_via_job(c, second)

    assert status(env, f) == "review" and found(env, question) == []
    top = hits(env, term)[0]
    assert "5 рабочих дней" in top.text and top.version_no == 2 and top.number == "1.5"
    page = c.get(f"/admin/faq/{f}/review").text
    assert "<del>3</del>" in page and "<ins>5</ins>" in page and "на проверке" in page


def test_d06_document_level_faq_goes_to_review_on_replace(env):
    pub(env, BASE)
    f = faq(env, "Что регулирует документ?", kind="document", points=[("doc-x", None)])
    assert found(env, "Что регулирует документ?") == [f]
    pub(env, docx("Регламент", "1. Раздел", "1.1. Совсем другой текст."))
    assert status(env, f) == "review" and found(env, "Что регулирует документ?") == []


# --- withdraw / rollback (D07, D09) ---------------------------------------------------------------

def test_d09_withdraw_document(env):
    pub(env, BASE)
    f = faq(env, "Кто согласует?")
    c = client("expert")
    page = c.get("/admin/documents/doc-x/withdraw").text
    assert "Снять с публикации" in page and "Уйдут на проверку и пропадут из поиска: 1" in page and "Кто согласует?" in page
    assert status(env, f) == "active" and env.execute("SELECT status FROM documents").fetchone()[0] == "active"
    assert c.post("/admin/documents/doc-x/withdraw", data=expected(env)).status_code == 303
    assert env.execute("SELECT status FROM documents").fetchone()[0] == "inactive"
    assert hits(env, "Согласование выполняет руководитель отдела") == []
    assert status(env, f) == "review" and found(env, "Кто согласует?") == []
    assert c.post("/admin/documents/doc-x/withdraw", data=expected(env)).status_code == 400  # already withdrawn
    # reversible: put it back
    assert "Вернуть в публикацию" in c.get("/admin/documents/doc-x/versions").text
    assert c.post("/admin/documents/doc-x/versions/1/activate", data=expected(env)).status_code == 303
    assert hits(env, "Согласование выполняет руководитель отдела")[0].number == "1.2"
    assert status(env, f) == "active"


def test_d07_rollback_restores_previous_answers(env):
    pub(env, BASE)
    f = faq(env, "Кто согласует?")
    pub(env, docx("Регламент", "1. Раздел", "1.1. Срок подачи заявки составляет три дня.",
                  "1.2. Согласование выполняет директор департамента.", "1.3. Архив хранится пять лет."))
    assert status(env, f) == "review"
    c = client("expert")
    versions = c.get("/admin/documents/doc-x/versions").text
    assert "Откатить к этой версии" in versions and "действующая" in versions
    confirm = c.get("/admin/documents/doc-x/versions/1/activate").text
    assert "Откатить к версии 1" in confirm and "Вернутся в поиск автоматически: 1" in confirm
    assert env.execute("SELECT version_number FROM document_versions v JOIN documents d "
                       "ON d.active_version_id=v.version_id").fetchone()[0] == 2  # GET changes nothing
    assert c.post("/admin/documents/doc-x/versions/1/activate", data=expected(env)).status_code == 303
    assert status(env, f) == "active" and found(env, "Кто согласует?") == [f]
    top = hits(env, "Согласование выполняет руководитель отдела")[0]
    assert top.version_no == 1 and "руководитель отдела" in top.text
    assert not any("директор" in h.text for h in hits(env, "Согласование выполняет директор департамента"))
    assert c.post("/admin/documents/doc-x/versions/1/activate", data=expected(env)).status_code == 400  # already active
    assert [h["action"] for h in V.history(env, f)] == ["to_review", "auto_restore"]


def test_rollback_recheck_runs_against_restored_version(env):
    pub(env, BASE)
    v2 = docx("Регламент", "1. Раздел", "1.1. Срок подачи заявки составляет три дня.",
              "1.2. Согласование выполняет директор департамента.", "1.3. Архив хранится пять лет.")
    pub(env, v2)
    f = faq(env, "Кто согласует?")  # pinned to version 2
    c = client("expert")
    assert "Уйдут на проверку и пропадут из поиска: 1" in c.get("/admin/documents/doc-x/versions/1/activate").text
    c.post("/admin/documents/doc-x/versions/1/activate", data=expected(env))
    assert status(env, f) == "review" and found(env, "Кто согласует?") == []


# --- FAQ review (requirement 5) -------------------------------------------------------------------

def review_setup(env):
    pub(env, BASE)
    f = faq(env, "Кто согласует?")
    pub(env, docx("Регламент", "1. Раздел", "1.1. Срок подачи заявки составляет три дня.",
                  "1.2. Согласование выполняет директор департамента.", "1.3. Архив хранится пять лет."))
    assert status(env, f) == "review"
    return f


def test_review_page_shows_old_and_new_text_side_by_side(env):
    f = review_setup(env)
    page = client("expert").get(f"/admin/faq/{f}/review").text
    assert "Основание при ответе" in page and "Текст сейчас" in page
    assert "<del>руководитель</del>" in page and "<ins>директор</ins>" in page


def test_review_confirm_repins_and_logs(env):
    f = review_setup(env)
    c = client("expert")
    assert c.post(f"/admin/faq/{f}/review/confirm").status_code == 303
    assert status(env, f) == "active" and found(env, "Кто согласует?") == [f]
    sha = env.execute("SELECT sha256 FROM points p JOIN documents d ON d.active_version_id=p.version_id "
                      "WHERE p.number='1.2'").fetchone()[0]
    assert env.execute("SELECT basis FROM faq_entries").fetchone()[0][0]["sha256"] == sha
    page = c.get(f"/admin/faq/{f}/review").text
    assert "expert@x.local" in page and "подтверждено" in page and "отправлено на проверку" in page and "система" in page and "изменён" in page
    assert c.post(f"/admin/faq/{f}/review/confirm").status_code == 400  # no longer under review


def test_review_edit_answer(env):
    f = review_setup(env)
    c = client("expert")
    assert c.post(f"/admin/faq/{f}/review/edit", data={"answer": "  "}).status_code == 400
    assert c.post(f"/admin/faq/{f}/review/edit", data={"answer": "Согласует директор."}).status_code == 303
    assert env.execute("SELECT answer, status FROM faq_entries").fetchone() == ("Согласует директор.", "active")
    assert [h["action"] for h in V.history(env, f)][-1] == "edit"


def test_review_repin_and_delete(env):
    f = review_setup(env)
    c = client("expert")
    assert c.post(f"/admin/faq/{f}/review/repin", data={"doc_id": "doc-x", "number": "9.9"}).status_code == 400
    assert status(env, f) == "review"
    assert c.post(f"/admin/faq/{f}/review/repin", data={"doc_id": "doc-x", "number": "1.3"}).status_code == 303
    assert status(env, f) == "active"
    assert env.execute("SELECT basis FROM faq_entries").fetchone()[0][0]["number"] == "1.3"
    assert c.post(f"/admin/faq/{f}/review/delete").status_code == 303
    assert status(env, f) == "deleted" and found(env, "Кто согласует?") == []
    assert [h["action"] for h in V.history(env, f)][-2:] == ["repin", "delete"]


def test_confirm_blocked_when_point_removed(env):
    pub(env, BASE)
    f = faq(env, "Сколько хранится архив?", points=[("doc-x", "1.3")])
    pub(env, docx("Регламент", "1. Раздел", "1.1. Срок подачи заявки составляет три дня."))
    assert status(env, f) == "review"
    r = client("expert").post(f"/admin/faq/{f}/review/confirm")
    assert r.status_code == 400 and "удалён" in r.text and status(env, f) == "review"


# --- access ---------------------------------------------------------------------------------------

ROUTES = [("get", "/admin/documents/doc-x/versions"), ("get", "/admin/documents/doc-x/versions/diff?old=1&new=1"),
          ("get", "/admin/documents/doc-x/withdraw"), ("post", "/admin/documents/doc-x/withdraw"),
          ("get", "/admin/documents/doc-x/versions/1/activate"), ("post", "/admin/documents/doc-x/versions/1/activate"),
          ("get", "/admin/jobs/1/changes"), ("get", "/admin/faq/1/review"), ("post", "/admin/faq/1/review/confirm"),
          ("post", "/admin/faq/1/review/edit"), ("post", "/admin/faq/1/review/repin"),
          ("post", "/admin/faq/1/review/delete")]


@pytest.mark.parametrize("method,path", ROUTES)
def test_employee_forbidden_and_anonymous_redirected(env, method, path):
    pub(env, BASE)
    faq(env, "Кто согласует?")
    before = env.execute("SELECT status FROM documents").fetchone()[0], env.execute(
        "SELECT status FROM faq_entries").fetchone()[0]
    r = getattr(client("employee"), method)(path)
    assert r.status_code == 403 and "Доступно только службе качества" in r.text
    r = getattr(client(), method)(path)
    assert r.status_code == 303 and r.headers["location"] == "/start"
    assert before == (env.execute("SELECT status FROM documents").fetchone()[0],
                      env.execute("SELECT status FROM faq_entries").fetchone()[0])


def test_documents_page_links_to_versions(env):
    pub(env, BASE)
    page = client("expert").get("/admin/documents").text
    assert "/admin/documents/doc-x/versions" in page and "Снять с публикации" in page
