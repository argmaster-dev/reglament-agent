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
    job = job_id_of(r.text)
    assert c.post(f"/admin/jobs/{job}/publish", data={"title": "Тех регламент"}).status_code == 303
    jobs.run_job(job)
    row = env.execute("SELECT status, done, total, selfcheck FROM ingest_jobs WHERE id=%s", (job,)).fetchone()
    assert row[0] == "ready", env.execute("SELECT error FROM ingest_jobs").fetchone()
    assert row[3]["found"] == row[3]["total"] == 195 and row[3]["misses"] == []
    page = c.get(f"/admin/jobs/{job}").text
    assert "Готов к поиску" in page and "Поиск находит 195 из 195" in page
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
