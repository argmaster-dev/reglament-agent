import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from reglament_agent.admin import jobs, routes
from reglament_agent.chat import session as S
from reglament_agent.storage.catalog import active_departments

DOCX = Path(__file__).resolve().parents[2] / "docx" / "Организация проектной деятельности Тех дирекции.docx"
SECRET = "test-secret"


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


def upload(c, **extra):
    data = {"target": "new", "department_select": "__new__", "department_new": "Техническая дирекция",
            "stages": ["presale", "project"], **extra}
    return c.post("/admin/documents/upload", data=data,
                  files={"file": (DOCX.name, DOCX.read_bytes(), "application/octet-stream")})


def job_id_of(html):
    return int(re.search(r"/admin/jobs/(\d+)/publish", html).group(1))


def test_auth(env):
    assert client().get("/admin/documents").headers["location"] == "/start"
    r = client("employee").get("/admin/documents")
    assert r.status_code == 403 and "Доступно только службе качества" in r.text
    assert client("expert").get("/admin/documents").status_code == 200


def test_upload_publish_selfcheck_and_duplicate(env):
    c = client("expert")
    r = upload(c)
    assert r.status_code == 200
    assert "<b>24</b> разделов" in r.text and "<b>171</b> пунктов" in r.text
    assert "предупреждений: 0" in r.text
    assert "Уровень 1 · 24" in r.text and "Уровень 2 ·" in r.text
    job = job_id_of(r.text)
    assert c.post(f"/admin/jobs/{job}/publish", data={"title": "Тех регламент"}).status_code == 303
    jobs.run_job(job)
    row = env.execute("SELECT status, done, total, selfcheck FROM ingest_jobs WHERE id=%s", (job,)).fetchone()
    assert row[0] == "ready", env.execute("SELECT error FROM ingest_jobs").fetchone()
    check = row[3]
    assert check["total"] == 171  # level>1 points of the TD regulation; 24 section headings excluded
    assert check["top1"] == check["found"] == check["total"]
    assert len(check["top1_misses"]) == check["total"] - check["top1"]
    assert {m["number"] for m in check["misses"]} <= {m["number"] for m in check["top1_misses"]}
    page = c.get(f"/admin/jobs/{job}").text
    assert "Готов к поиску" in page and f"Поиск находит {check['top1']} из 171" in page
    assert active_departments(env) == ["Техническая дирекция"]
    assert "Тех регламент" in c.get("/admin/documents").text
    doc_id = env.execute("SELECT doc_id FROM documents").fetchone()[0]
    again = upload(c, target=doc_id)
    assert "уже загружена" in again.text and "версия 1" in again.text


def test_failure_keeps_active_version(env):
    class Boom:
        def embed(self, texts):
            raise RuntimeError("embedder down")

    c = client("expert")
    job = job_id_of(upload(c).text)
    c.post(f"/admin/jobs/{job}/publish", data={})
    jobs.run_job(job, embedder=Boom())
    status, error = env.execute("SELECT status, error FROM ingest_jobs WHERE id=%s", (job,)).fetchone()
    assert status == "failed" and "embedder down" in error
    assert env.execute("SELECT count(*) FROM document_versions").fetchone()[0] == 0
    assert active_departments(env) == []
    assert "embedder down" in c.get(f"/admin/jobs/{job}").text


@pytest.mark.parametrize("fail_recheck", [False, True])
def test_replacement_commits_document_and_faq_together(env, monkeypatch, fail_recheck):
    import psycopg

    from reglament_agent.ingest.embedding import FakeEmbedder
    from reglament_agent.quality.faq import recheck_faq_after_publication
    from tests.quality.test_faq import ans, publish_td, ticket
    from tests.test_storage import new_revision

    first = publish_td(env)
    entry = ans(env, ticket(env), basis_kind="points", basis_points=[("td-project", "14.1")])
    target = client("expert")
    preview = target.post("/admin/documents/upload", data={"target": "td-project"},
                          files={"file": (DOCX.name, new_revision(DOCX.read_bytes()))})
    job = job_id_of(preview.text)
    target.post(f"/admin/jobs/{job}/publish", data={})

    def check_faq(conn, doc_id):
        from os import environ

        moved = recheck_faq_after_publication(conn, doc_id)
        assert moved == 1
        with psycopg.connect(environ["DATABASE_URL"]) as observer:
            assert observer.execute("SELECT active_version_id FROM documents").fetchone() == (first.version_id,)
            assert observer.execute("SELECT status FROM faq_entries").fetchone() == ("active",)
        if fail_recheck:
            raise RuntimeError("ошибка внутри транзакции после проверки FAQ")
        return moved

    monkeypatch.setattr(jobs, "recheck_faq_after_publication", check_faq)
    jobs.run_job(job, FakeEmbedder())
    if fail_recheck:
        assert env.execute("SELECT count(*) FROM document_versions").fetchone() == (1,)
        assert env.execute("SELECT status FROM faq_entries WHERE id=%s", (entry["faq_id"],)).fetchone() == ("active",)
        assert env.execute("SELECT status FROM ingest_jobs").fetchone() == ("failed",)
    else:
        assert env.execute("SELECT count(*) FROM document_versions").fetchone() == (2,)
        assert env.execute("SELECT status FROM faq_entries WHERE id=%s", (entry["faq_id"],)).fetchone() == ("review",)
        assert env.execute("SELECT status, stats->>'faq_review' FROM ingest_jobs").fetchone() == ("ready", "1")


def test_failed_selfcheck_reports_committed_version(env, monkeypatch):
    def unavailable(*args):
        raise RuntimeError("самопроверка недоступна")

    c = client("expert")
    job = job_id_of(upload(c).text)
    c.post(f"/admin/jobs/{job}/publish", data={})
    monkeypatch.setattr(jobs, "selfcheck", unavailable)
    jobs.run_job(job)
    assert env.execute("SELECT status, stage FROM ingest_jobs").fetchone() == ("failed", "selfcheck")
    assert active_departments(env) == ["Техническая дирекция"]
    assert "Версия 1 опубликована и доступна поиску" in c.get(f"/admin/jobs/{job}").text


def test_unusable_uploads_and_direct_employee_post(env):
    employee = client("employee")
    assert upload(employee).status_code == 403
    assert employee.post("/admin/jobs/1/publish").status_code == 403
    expert = client("expert")
    for filename in ("broken.docx", "sample.txt"):
        response = expert.post("/admin/documents/upload", files={"file": (filename, b"broken")})
        assert response.status_code == 400
        assert "формате DOCX" in response.text
    assert env.execute("SELECT count(*) FROM ingest_jobs").fetchone() == (0,)


def test_faq_publication_waits_for_replacement_and_pins_committed_version(env):
    import os
    from concurrent.futures import ThreadPoolExecutor

    import psycopg
    from psycopg.errors import LockNotAvailable

    from reglament_agent.ingest.embedding import FakeEmbedder
    from reglament_agent.quality.faq import recheck_faq_after_publication
    from reglament_agent.storage.publish import publish
    from tests.quality.test_faq import CARD, ans, publish_td, ticket
    from tests.test_storage import new_revision

    publish_td(env)
    number = ticket(env)
    dsn = os.environ["DATABASE_URL"]
    revised = new_revision(DOCX.read_bytes())

    def publish_faq():
        with psycopg.connect(dsn, autocommit=True) as writer:
            writer.execute("SET lock_timeout = '150ms'")
            return ans(writer, number, basis_kind="points", basis_points=[("td-project", "14.1")])

    def check_faq(conn, _publication):
        assert recheck_faq_after_publication(conn, CARD.doc_id) == 0
        with ThreadPoolExecutor(max_workers=1) as pool:
            with pytest.raises(LockNotAvailable):
                pool.submit(publish_faq).result(timeout=3)

    latest = publish(env, CARD, revised, FakeEmbedder(), "reviewer", before_commit=check_faq)
    faq = publish_faq()
    assert env.execute("SELECT status, version_id FROM faq_entries WHERE id=%s", (faq["faq_id"],)).fetchone() == (
        "active", latest.version_id,
    )
