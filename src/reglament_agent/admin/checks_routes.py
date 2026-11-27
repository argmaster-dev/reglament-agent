"""Expert pages: check questions of a regulation document and their runs (admin-b04)."""
from __future__ import annotations

from functools import partial

from anyio import from_thread
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from reglament_agent.admin import check_proposals as P
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


def _page(request: Request, doc_id: str, message: str | None = None, status_code: int = 200):
    with jobs.connect() as conn:
        doc = V.get_document(conn, doc_id)
        if doc is None:
            return render("forbidden.html", request, 404, message="Документ не найден")
        runs = C.last_runs(conn, doc_id, 2)
        proposals = P.list_proposals(conn, doc_id)
    try:
        cs = C.load_all(doc_id)
    except C.CheckFileError as exc:
        return _invalid(request, doc, runs, exc)
    latest = runs[0] if runs else None
    previous = runs[1] if len(runs) > 1 else None
    old = {r["question"]: r for r in previous["results"]} if previous else {}
    rows = [{"r": r, "old": old.get(r["question"])} for r in (latest["results"] if latest else [])]
    return render("checks_page.html", request, status_code, nav="documents", doc=doc, cs=cs, latest=latest,
                  previous=previous, proposals=proposals, message=message, rows=rows, outcomes=OUTCOMES, refresh=bool(latest and latest["status"] == "running"),
                  passed=sum(1 for r in rows if r["r"]["passed"]),
                  changed=sum(1 for r in rows if r["r"].get("status") == "changed"))


def _invalid(request, doc, runs, exc):
    return render("checks_page.html", request, 400, nav="documents", doc=doc, cs=None, latest=None, previous=None,
                  rows=[], outcomes=OUTCOMES, refresh=False, passed=0, changed=0, proposals=[], message=None,
                  error=f"Набор проверочных вопросов не принят: {exc}")


@router.get("/documents/{doc_id}/checks", response_class=HTMLResponse)
def checks_page(request: Request, doc_id: str):
    return _page(request, doc_id)


@router.post("/documents/{doc_id}/checks/run")
def checks_run(request: Request, doc_id: str):
    actor = S.identity_from_headers(request.headers, _secret()).email
    with jobs.connect() as conn:
        doc = V.get_document(conn, doc_id)
        if doc is None:
            return render("forbidden.html", request, 404, message="Документ не найден")
        try:
            cs = C.load_all(doc_id)
        except C.CheckFileError as exc:
            return _invalid(request, doc, [], exc)
        if cs is None:
            return render("forbidden.html", request, 404, message="Для документа нет проверочных вопросов")
        busy = C.running_run(conn, doc_id)
    if busy is None:
        C.launch(C.start_run(doc_id, actor))
    return RedirectResponse(f"/admin/documents/{doc_id}/checks", status_code=303)


def _expert_action(request: Request, doc_id: str):
    actor = S.identity_from_headers(request.headers, _secret()).email
    with jobs.connect() as conn:
        doc = V.get_document(conn, doc_id)
    return actor, doc


async def _ask(app, messages):
    # FastAPI runs the sync DB route in a worker. Use the chat event loop for its client and FIFO.
    model = app.state.proposal_model
    if model is None:  # standalone admin / custom chat factory without a model
        model = app.state.proposal_model = P.model_factory()
    result = await model.chat_json(messages, P.SCHEMA, step="check_proposals",
                                   options={"num_predict": 900, "temperature": 0})
    return result.data


@router.post("/documents/{doc_id}/checks/propose")
def checks_propose(request: Request, doc_id: str):
    actor, doc = _expert_action(request, doc_id)
    if doc is None:
        return render("forbidden.html", request, 404, message="Документ не найден")
    with jobs.connect() as conn:
        _, message = P.propose(conn, doc_id, actor, partial(from_thread.run, _ask, request.app))
    return _page(request, doc_id, message=message)


@router.post("/documents/{doc_id}/checks/proposals/{proposal_id}/accept")
def checks_accept(request: Request, doc_id: str, proposal_id: int):
    actor, doc = _expert_action(request, doc_id)
    if doc is None:
        return render("forbidden.html", request, 404, message="Документ не найден")
    with jobs.connect() as conn:
        ok = P.accept(conn, doc_id, proposal_id, actor)
    if not ok:
        return _page(request, doc_id, message="Предложение не принято: изменилась версия документа, "
                     "пункт недоступен или предложение уже обработано.")
    return _page(request, doc_id)


@router.post("/documents/{doc_id}/checks/proposals/{proposal_id}/discard")
def checks_discard(request: Request, doc_id: str, proposal_id: int):
    actor, doc = _expert_action(request, doc_id)
    if doc is None:
        return render("forbidden.html", request, 404, message="Документ не найден")
    with jobs.connect() as conn:
        P.discard(conn, doc_id, proposal_id)
    return RedirectResponse(f"/admin/documents/{doc_id}/checks", status_code=303)
