"""Admin pages: documents, upload with preview, publication job status."""
from __future__ import annotations

import hashlib
import os
import time
from collections import Counter
from pathlib import Path

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from psycopg.types.json import Jsonb

from reglament_agent.admin import jobs
from reglament_agent.chat import session as S
from reglament_agent.ingest.model import BlockKind
from reglament_agent.ingest.parser import parse_docx
from reglament_agent.ingest.text import content_hash

STAGES = {"presale": "пресейл", "project": "реализация"}
MAX_UPLOAD = 20 * 1024 * 1024
env = Environment(loader=FileSystemLoader(Path(__file__).parent / "templates"),
                  autoescape=select_autoescape(["html"]))
router = APIRouter(prefix="/admin")


def render(template: str, request: Request, status_code: int = 200, **ctx) -> HTMLResponse:
    ident = S.identity_from_headers(request.headers, _secret())
    html = env.get_template(template).render(
        identity=ident, stages=STAGES, open_tickets=_open_tickets(ident),
        mail_ui_url=os.environ.get("MAIL_UI_URL") if ident and ident.role == "expert" else None, **ctx)
    return HTMLResponse(html, status_code=status_code)


def _open_tickets(ident) -> int | None:
    if ident is None or ident.role != "expert":
        return None
    try:
        with jobs.connect() as conn:
            return conn.execute("SELECT count(*) FROM tickets WHERE status='open'").fetchone()[0]
    except Exception:  # noqa: BLE001 - a nav badge must never break a page
        return None


def _secret() -> str:
    return os.environ["CHAINLIT_AUTH_SECRET"]


def install(app) -> None:
    """Mount routes and the role guard (expert only) in front of Chainlit's catch-all route."""
    from reglament_agent.admin.quality_routes import router as quality_router

    for i, route in enumerate([*router.routes, *quality_router.routes]):
        app.router.routes.insert(i, route)

    @app.middleware("http")
    async def expert_only(request: Request, call_next):
        if request.url.path == "/admin" or request.url.path.startswith("/admin/"):
            ident = S.identity_from_headers(request.headers, _secret())
            if ident is None:
                return RedirectResponse("/start", status_code=303)
            if ident.role != "expert":
                return render("forbidden.html", request, 403)
            jobs.ensure_runner()
        return await call_next(request)

    try:  # make sure ingest_jobs exists; a down DB must not stop the chat from starting
        jobs.ensure_schema()
    except Exception:  # noqa: BLE001
        pass


# --- helpers ---------------------------------------------------------------------------------

def summarize(parsed) -> dict:
    kinds = Counter(b.kind for p in parsed.points for b in p.blocks)
    sections = []
    for p in parsed.points:
        if p.level == 1:
            sections.append({"point": p, "children": []})
        elif sections:
            sections[-1]["children"].append(p)
    return {
        "sections": sum(1 for p in parsed.points if p.level == 1), "points": sum(1 for p in parsed.points if p.level > 1),
        "levels": len({p.level for p in parsed.points}), "notes": kinds[BlockKind.NOTE],
        "lists": kinds[BlockKind.LIST_ITEM], "tables": kinds[BlockKind.TABLE],
        "warnings": parsed.warnings, "tree": sections,
    }


def diff_with_active(conn, doc_id: str, parsed) -> dict | None:
    row = conn.execute("SELECT active_version_id FROM documents WHERE doc_id=%s", (doc_id,)).fetchone()
    if not row or row[0] is None:
        return None
    old = dict(conn.execute("SELECT number, sha256 FROM points WHERE version_id=%s", (row[0],)).fetchall())
    new = {p.number: content_hash(p.text) for p in parsed.points}
    return {"added": len(new.keys() - old.keys()), "removed": len(old.keys() - new.keys()),
            "changed": sum(1 for n in new.keys() & old.keys() if new[n] != old[n]),
            "same": sum(1 for n in new.keys() & old.keys() if new[n] == old[n])}


def _document_options(conn):
    docs = conn.execute("SELECT doc_id, title, department FROM documents ORDER BY title").fetchall()
    departments = sorted({d[2] for d in docs})
    return docs, departments


def _upload_form(request, conn, error=None, status_code=200):
    docs, departments = _document_options(conn)
    return render("upload.html", request, status_code, docs=docs, departments=departments, error=error)


def _new_doc_id(digest: str) -> str:
    return "doc-" + digest[:10]


# --- pages -----------------------------------------------------------------------------------

@router.get("", include_in_schema=False)
def root():
    return RedirectResponse("/admin/documents", status_code=303)


@router.get("/documents", response_class=HTMLResponse)
def documents(request: Request):
    with jobs.connect() as conn:
        rows = conn.execute("""
            SELECT d.doc_id, d.title, d.department, d.stages, d.status, v.version_number, v.created_at,
                   (v.stats->>'points')::int
            FROM documents d LEFT JOIN document_versions v ON v.version_id=d.active_version_id
            ORDER BY d.title""").fetchall()
        running = conn.execute("SELECT id, doc_id FROM ingest_jobs WHERE status IN ('queued','running') "
                               "ORDER BY id").fetchall()
    return render("documents.html", request, rows=rows, running=running)


@router.get("/documents/upload", response_class=HTMLResponse)
def upload_form(request: Request):
    with jobs.connect() as conn:
        return _upload_form(request, conn)


@router.post("/documents/upload", response_class=HTMLResponse)
async def upload(request: Request, file: UploadFile = File(...), target: str = Form("new"),
                 department_select: str = Form(""), department_new: str = Form(""),
                 stages: list[str] = Form(default=[]), title: str = Form("")):
    data = await file.read(MAX_UPLOAD + 1)
    ident = S.identity_from_headers(request.headers, _secret())
    with jobs.connect() as conn:
        def fail(msg):
            return _upload_form(request, conn, msg, 400)

        if not data or len(data) > MAX_UPLOAD:
            return fail("Файл пустой или больше 20 МБ.")
        t0 = time.time()
        try:
            parsed = parse_docx(data)
        except Exception:  # noqa: BLE001 - any parser failure means "not a usable DOCX"
            return fail("Не удалось прочитать файл: нужен документ в формате DOCX.")
        parse_sec = round(time.time() - t0, 2)
        if not parsed.points:
            return fail("В файле не найдено ни одного пронумерованного пункта.")
        digest = hashlib.sha256(data).hexdigest()
        if target == "new":
            department = department_new.strip() if department_select in ("", "__new__") else department_select
            chosen = [s for s in stages if s in STAGES]
            if not department or not chosen:
                return fail("Укажите подразделение и хотя бы один этап.")
            doc_id, doc_title = _new_doc_id(digest), (title.strip() or parsed.title.strip() or file.filename)
            existing = conn.execute("SELECT 1 FROM documents WHERE doc_id=%s", (doc_id,)).fetchone()
            if existing:
                target = doc_id
        if target != "new":
            row = conn.execute("SELECT doc_id, title, department, stages FROM documents WHERE doc_id=%s",
                               (target,)).fetchone()
            if not row:
                return fail("Документ не найден.")
            doc_id, doc_title, department, chosen = row[0], row[1], row[2], list(row[3])
            dup = conn.execute("SELECT version_number FROM document_versions WHERE doc_id=%s AND sha256=%s",
                               (doc_id, digest)).fetchone()
            if dup:
                return render("duplicate.html", request, number=dup[0], title=doc_title, doc_id=doc_id)
        summary = summarize(parsed)
        diff = diff_with_active(conn, doc_id, parsed)
        card = {"doc_id": doc_id, "title": doc_title, "department": department, "stages": chosen,
                "mode": "version" if diff is not None else "new"}
        job_id = conn.execute("""INSERT INTO ingest_jobs(doc_id, card, filename, data, sha256, status, stage,
            stats, created_by) VALUES (%s,%s,%s,%s,%s,'parsed','parse',%s,%s) RETURNING id""",
            (doc_id, Jsonb(card), Path(file.filename or "upload.docx").name, data, digest,
             Jsonb({"parse_sec": parse_sec}), ident.email)).fetchone()[0]
    return render("preview.html", request, job_id=job_id, card=card, summary=summary, diff=diff,
                  parse_sec=parse_sec, filename=file.filename)


@router.post("/jobs/{job_id}/publish")
def publish_job(job_id: int, title: str = Form("")):
    with jobs.connect() as conn:
        if title.strip():
            conn.execute("UPDATE ingest_jobs SET card = jsonb_set(card, '{title}', to_jsonb(%s::text)) "
                         "WHERE id=%s AND status='parsed'", (title.strip(), job_id))
        conn.execute("UPDATE ingest_jobs SET status='queued', stage='embed', updated_at=now() "
                     "WHERE id=%s AND status='parsed'", (job_id,))
    return RedirectResponse(f"/admin/jobs/{job_id}", status_code=303)


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: int):
    with jobs.connect() as conn:
        conn.execute("DELETE FROM ingest_jobs WHERE id=%s AND status='parsed'", (job_id,))
    return RedirectResponse("/admin/documents", status_code=303)


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_page(request: Request, job_id: int):
    with jobs.connect() as conn:
        row = conn.execute("""SELECT doc_id, card, filename, status, stage, done, total, stats, selfcheck, error,
            created_at, updated_at, extract(epoch FROM created_at) FROM ingest_jobs WHERE id=%s""",
                           (job_id,)).fetchone()
    if not row:
        return render("forbidden.html", request, 404, message="Задача не найдена")
    doc_id, card, filename, status, stage, done, total, stats, check, error, created, updated, created_ts = row
    now = time.time()
    t = {}
    started = stats.get("started")
    embed_end = stats.get("published", None)
    if started:
        publish_sec = stats.get("publish_sec")
        t["embed"] = (stats["published"] - started - (publish_sec or 0)) if embed_end else now - started
        t["publish"] = publish_sec
        t["selfcheck"] = stats.get("selfcheck_sec") if "selfcheck_sec" in stats else (
            now - stats["published"] if embed_end and status == "running" else None)
        t["total"] = (stats.get("finished") or now) - started
    t["parse"] = stats.get("parse_sec")
    order = ["embed", "publish", "selfcheck"]
    cur = {"queued": -1, "embed": 0, "publish": 1, "selfcheck": 2, "ready": 3}.get(stage, -1)
    if status == "running" and stage == "embed" and total and done >= total:
        cur = 1  # all batches done, transaction being written
    steps = []
    for i, key in enumerate(order):
        state = "done" if (cur > i or status in ("ready", "duplicate")) else "active" if cur == i else "todo"
        if status == "failed" and cur == i:
            state = "failed"
        steps.append({"key": key, "state": state, "sec": t.get(key)})
    return render("job.html", request, job_id=job_id, doc_id=doc_id, card=card, filename=filename,
                  status=status, stage=stage, done=done, total=total, stats=stats, check=check, error=error,
                  steps=steps, t=t, refresh=status in ("queued", "running"))
