"""Expert pages: check questions of a regulation document and their runs (admin-b04)."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from reglament_agent.admin import checks as C
from reglament_agent.admin import jobs
from reglament_agent.admin import versions as V
from reglament_agent.admin.routes import _secret, render
from reglament_agent.chat import session as S

router = APIRouter(prefix="/admin")
OUTCOMES = {"answer": "ответ", "clarify": "нужно уточнение", "variants": "варианты по подразделениям",
            "low_confidence": "требует подтверждения", "no_basis": "нет в регламентах",
            "external_doc": "регламент вне базы", "conflict": "положения расходятся",
            "expert_answer": "ответ службы качества", "off_topic": "не по регламентам", "error": "ошибка агента"}


def _page(request: Request, doc_id: str):
    with jobs.connect() as conn:
        doc = V.get_document(conn, doc_id)
        cs = C.load_questions(doc_id)
        if doc is None:
            return render("forbidden.html", request, 404, message="Документ не найден")
        runs = C.last_runs(conn, doc_id, 2)
    latest = runs[0] if runs else None
    previous = runs[1] if len(runs) > 1 else None
    old = {r["question"]: r for r in previous["results"]} if previous else {}
    rows = [{"r": r, "old": old.get(r["question"])} for r in (latest["results"] if latest else [])]
    return render("checks_page.html", request, nav="documents", doc=doc, cs=cs, latest=latest, previous=previous,
                  rows=rows, outcomes=OUTCOMES, refresh=bool(latest and latest["status"] == "running"),
                  passed=sum(1 for r in rows if r["r"]["passed"]))


@router.get("/documents/{doc_id}/checks", response_class=HTMLResponse)
def checks_page(request: Request, doc_id: str):
    return _page(request, doc_id)


@router.post("/documents/{doc_id}/checks/run")
def checks_run(request: Request, doc_id: str):
    actor = S.identity_from_headers(request.headers, _secret()).email
    with jobs.connect() as conn:
        if V.get_document(conn, doc_id) is None or C.load_questions(doc_id) is None:
            return render("forbidden.html", request, 404, message="Для документа нет проверочных вопросов")
        busy = C.running_run(conn, doc_id)
    if busy is None:
        C.launch(C.start_run(doc_id, actor))
    return RedirectResponse(f"/admin/documents/{doc_id}/checks", status_code=303)
