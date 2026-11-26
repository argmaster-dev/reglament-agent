"""Expert pages: document versions, withdraw, rollback, diff and FAQ review after a change."""
from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from reglament_agent.admin import jobs
from reglament_agent.admin import versions as V
from reglament_agent.admin.routes import _secret, render
from reglament_agent.chat import session as S
from reglament_agent.ingest.parser import parse_docx

router = APIRouter(prefix="/admin")


def _int(value: str) -> int | None:
    return int(value) if str(value).strip().isdigit() else None


def _actor(request: Request) -> str:
    return S.identity_from_headers(request.headers, _secret()).email


def _not_found(request: Request, message: str):
    return render("forbidden.html", request, 404, message=message)


def _versions_page(request, conn, doc_id, *, error=None, status_code=200):
    doc = V.get_document(conn, doc_id)
    if doc is None:
        return _not_found(request, "Документ не найден")
    versions = V.list_versions(conn, doc_id)
    pairs = [(v["number"], versions[i + 1]["number"]) for i, v in enumerate(versions[:-1])]
    return render("versions_list.html", request, status_code, nav="documents", doc=doc, versions=versions,
                  pairs=pairs, error=error)


@router.get("/documents/{doc_id}/versions", response_class=HTMLResponse)
def versions_list(request: Request, doc_id: str, error: str = ""):
    with jobs.connect() as conn:
        return _versions_page(request, conn, doc_id, error=error or None)


@router.get("/documents/{doc_id}/versions/diff", response_class=HTMLResponse)
def versions_diff(request: Request, doc_id: str, old: int, new: int):
    with jobs.connect() as conn:
        doc = V.get_document(conn, doc_id)
        if doc is None:
            return _not_found(request, "Документ не найден")
        try:
            diff = V.diff_versions(conn, doc_id, old, new)
        except V.VersionError as exc:
            return _not_found(request, str(exc))
    return render("versions_diff.html", request, nav="documents", doc=doc, old=old, new=new, diff=diff,
                  impact=None, job_id=None)


@router.get("/documents/{doc_id}/withdraw", response_class=HTMLResponse)
def withdraw_confirm(request: Request, doc_id: str, notice: str = "", status_code: int = 200):
    with jobs.connect() as conn:
        doc = V.get_document(conn, doc_id)
        if doc is None:
            return _not_found(request, "Документ не найден")
        try:
            impact = V.preview_withdraw(conn, doc_id)
        except V.VersionError as exc:
            return _versions_page(request, conn, doc_id, error=str(exc), status_code=400)
    return render("versions_confirm.html", request, status_code, nav="documents", doc=doc, impact=impact,
                  kind="withdraw", target=None, diff=None, notice=notice)


def _stale(request, conn, doc_id, kind, number=None):
    """Re-render the confirmation with fresh consequences; nothing was changed."""
    return (withdraw_confirm if kind == "withdraw" else activate_confirm)(request, doc_id, *(
        [] if number is None else [number]), notice="Версия изменилась, проверьте последствия заново.",
        status_code=409)


@router.post("/documents/{doc_id}/withdraw")
def withdraw_do(request: Request, doc_id: str, expected_version_id: str = Form("")):
    with jobs.connect() as conn:
        try:
            V.withdraw(conn, doc_id, _actor(request), _int(expected_version_id))
        except V.StaleVersion:
            return _stale(request, conn, doc_id, "withdraw")
        except V.VersionError as exc:
            return _versions_page(request, conn, doc_id, error=str(exc), status_code=400)
    return RedirectResponse(f"/admin/documents/{doc_id}/versions", status_code=303)


def _activation(request, conn, doc_id, number):
    doc = V.get_document(conn, doc_id)
    if doc is None:
        return None, None, _not_found(request, "Документ не найден")
    try:
        return doc, V.version_id_of(conn, doc_id, number), None
    except V.VersionError as exc:
        return doc, None, _not_found(request, str(exc))


@router.get("/documents/{doc_id}/versions/{number}/activate", response_class=HTMLResponse)
def activate_confirm(request: Request, doc_id: str, number: int, notice: str = "", status_code: int = 200):
    with jobs.connect() as conn:
        doc, vid, failure = _activation(request, conn, doc_id, number)
        if failure is not None:
            return failure
        try:
            impact = V.preview_activate(conn, doc_id, vid)
        except V.VersionError as exc:
            return _versions_page(request, conn, doc_id, error=str(exc), status_code=400)
        current = conn.execute("SELECT v.version_number FROM documents d JOIN document_versions v "
                               "ON v.version_id=d.active_version_id WHERE d.doc_id=%s", (doc_id,)).fetchone()
        diff = V.diff_versions(conn, doc_id, current[0], number) if current and current[0] != number else None
    return render("versions_confirm.html", request, status_code, nav="documents", doc=doc, impact=impact,
                  kind="activate", target=number, current=current[0] if current else None, diff=diff,
                  notice=notice)


@router.post("/documents/{doc_id}/versions/{number}/activate")
def activate_do(request: Request, doc_id: str, number: int, expected_version_id: str = Form("")):
    with jobs.connect() as conn:
        doc, vid, failure = _activation(request, conn, doc_id, number)
        if failure is not None:
            return failure
        try:
            V.activate_version(conn, doc_id, vid, _actor(request), _int(expected_version_id))
        except V.StaleVersion:
            return _stale(request, conn, doc_id, "activate", number)
        except V.VersionError as exc:
            return _versions_page(request, conn, doc_id, error=str(exc), status_code=400)
    return RedirectResponse(f"/admin/documents/{doc_id}/versions", status_code=303)


@router.get("/jobs/{job_id}/changes", response_class=HTMLResponse)
def job_changes(request: Request, job_id: int):
    """What a parsed upload will change compared with the active version (before publishing)."""
    with jobs.connect() as conn:
        row = conn.execute("SELECT doc_id, data, sha256 FROM ingest_jobs WHERE id=%s", (job_id,)).fetchone()
        if row is None:
            return _not_found(request, "Задача не найдена")
        doc_id, data, digest = row
        doc = V.get_document(conn, doc_id)
        if doc is None or doc["active_version_id"] is None:
            return render("versions_diff.html", request, nav="documents", doc=doc, old=None, new=None,
                          diff=None, impact=None, job_id=job_id, doc_id=doc_id)
        parsed = parse_docx(bytes(data))
        diff = V.diff_points(V.load_points(conn, doc["active_version_id"]), V.parsed_points(parsed))
        impact = V.preview_replacement(conn, doc_id, parsed, digest)
        old = conn.execute("SELECT version_number FROM document_versions WHERE version_id=%s",
                           (doc["active_version_id"],)).fetchone()[0]
    return render("versions_diff.html", request, nav="documents", doc=doc, old=old, new="новая", diff=diff,
                  impact=impact, job_id=job_id, doc_id=doc_id)


# --- FAQ review --------------------------------------------------------------------------------

def _review_page(request, conn, faq_id, *, error=None, status_code=200):
    ctx = V.review_context(conn, faq_id)
    if ctx is None:
        return _not_found(request, "Запись FAQ не найдена")
    return render("versions_review.html", request, status_code, nav="faq", error=error, **ctx)


@router.get("/faq/{faq_id}/review", response_class=HTMLResponse)
def review_page(request: Request, faq_id: int):
    with jobs.connect() as conn:
        return _review_page(request, conn, faq_id)


@router.post("/faq/{faq_id}/review/{action}", response_class=HTMLResponse)
def review_action(request: Request, faq_id: int, action: str, answer: str = Form(""), doc_id: str = Form(""),
                  number: str = Form(""), expected_version_id: str = Form(""), expected_hashes: str = Form("")):
    actor = _actor(request)
    shown = V.make_shown(expected_version_id, expected_hashes)
    with jobs.connect() as conn:
        try:
            if action == "confirm":
                V.confirm_entry(conn, faq_id, actor, shown)
            elif action == "edit":
                V.edit_answer(conn, faq_id, actor, answer, shown)
            elif action == "repin":
                V.repin_entry(conn, faq_id, actor, doc_id, number, shown)
            elif action == "delete":
                V.delete_entry(conn, faq_id, actor)
            else:
                return _not_found(request, "Неизвестное действие")
        except V.StaleBasis as exc:
            return _review_page(request, conn, faq_id, error=str(exc), status_code=409)
        except V.VersionError as exc:
            return _review_page(request, conn, faq_id, error=str(exc), status_code=400)
    return RedirectResponse(f"/admin/faq/{faq_id}/review", status_code=303)
