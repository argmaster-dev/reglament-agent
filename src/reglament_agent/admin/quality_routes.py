"""Expert pages: tickets and FAQ."""
from __future__ import annotations

import re

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from psycopg.rows import dict_row

from reglament_agent.admin import jobs
from reglament_agent.admin.routes import render
from reglament_agent.answer import SourceRef
from reglament_agent.chat import session as S
from reglament_agent.ingest.embedding import make_embedder
from reglament_agent.quality import faq as F
from reglament_agent.quality.mail import REASONS
from reglament_agent.quality.tickets import get_ticket
from reglament_agent.storage.sources import point_view

router = APIRouter(prefix="/admin")
STATUS = {"open": "открыто", "answered": "отвечено", "closed_duplicate": "закрыто: дубликат",
          "closed_offtopic": "закрыто: не по регламентам"}
MAIL = {"pending": "в очереди", "sent": "отправлено", "error": "ошибка отправки"}
FAQ_STATUS = {"active": "опубликовано", "review": "на проверке", "deleted": "удалено"}
_POINT = re.compile(r"([\w][\w.-]*)\s+п(?:ункт)?\.?\s*(\d+(?:\.\d+)*)", re.IGNORECASE)


def parse_points(text: str) -> list[tuple[str, str]]:
    """«td-project п. 16.1, ib-project п. 3» -> [(doc, number)]; raises TicketError on junk."""
    items = [t.strip() for t in re.split(r"[,;\n]+", text or "") if t.strip()]
    out = []
    for item in items:
        m = _POINT.fullmatch(item)
        if not m:
            raise F.TicketError(f"Не удалось разобрать «{item}». Формат: doc_id п. N, doc_id п. N")
        out.append((m.group(1), m.group(2)))
    return out


def view_text(conn, doc_id: str, number: str, version_no: int | None = None) -> dict | None:
    """Point text via `point_view`; without `version_no` (or if it is gone) the active version."""
    active = conn.execute("SELECT v.version_number FROM documents d JOIN document_versions v "
                          "ON v.version_id=d.active_version_id WHERE d.doc_id=%s", (doc_id,)).fetchone()
    for vno in dict.fromkeys(v for v in (version_no, active[0] if active else None) if v):
        try:
            pv = point_view(conn, SourceRef(doc_id, "", "", vno, number))
        except (LookupError, ValueError):
            continue
        return {"heading": " › ".join(pv.path), "text": "\n".join(pv.sentences), "version_no": pv.version_no}
    return None


def _ticket_context(conn, number: str) -> dict | None:
    t = get_ticket(conn, number)
    if t is None:
        return None
    cur = conn.cursor(row_factory=dict_row)
    mails = cur.execute("SELECT kind, status, last_error FROM mail_outbox WHERE ticket_id=%s ORDER BY id",
                        (t["id"],)).fetchall()
    ans = cur.execute("SELECT * FROM ticket_answers WHERE ticket_id=%s", (t["id"],)).fetchone()
    cands = []
    for c in t["candidates"]:
        view = view_text(conn, c.get("doc_id"), c.get("point"), c.get("version_no")) if c.get("point") else None
        cands.append({**c, "view": view})
    ctx = {"t": t, "mails": mails, "answer": ans, "candidates": cands, "reason_text": REASONS.get(t["reason"], t["reason"]),
           "status_text": STATUS.get(t["status"], t["status"]), "mail_text": MAIL,
           "basis_text": F.basis_label(ans["basis_kind"], ans["basis"]) if ans else None}
    return ctx


def _ticket_page(request, conn, number, *, status_code=200, error=None, form=None):
    ctx = _ticket_context(conn, number)
    if ctx is None:
        return render("forbidden.html", request, 404, message="Обращение не найдено")
    form = form or {}
    form.setdefault("faq_question", ctx["t"]["restated_question"] or ctx["t"]["question"])
    return render("ticket.html", request, status_code, nav="tickets", error=error, form=form, **ctx)


@router.get("/tickets", response_class=HTMLResponse)
def tickets(request: Request, status: str = "", reason: str = ""):
    where, params = [], []
    if status:
        where.append("t.status=%s")
        params.append(status)
    if reason:
        where.append("t.reason=%s")
        params.append(reason)
    with jobs.connect() as conn:
        rows = conn.cursor(row_factory=dict_row).execute(
            "SELECT t.number, t.created_at, t.user_email, t.reason, t.question, t.status, "
            "(SELECT o.status FROM mail_outbox o WHERE o.ticket_id=t.id AND o.kind='ticket_created') AS mail "
            "FROM tickets t " + ("WHERE " + " AND ".join(where) if where else "")
            + " ORDER BY t.created_at DESC, t.id DESC LIMIT 500", params).fetchall()
    return render("tickets.html", request, nav="tickets", rows=rows, status=status, reason=reason,
                  statuses=STATUS, reasons=REASONS, mail_text=MAIL)


@router.get("/tickets/{number}", response_class=HTMLResponse)
def ticket_page(request: Request, number: str):
    with jobs.connect() as conn:
        return _ticket_page(request, conn, number)


@router.post("/tickets/{number}", response_class=HTMLResponse)
async def ticket_submit(request: Request, number: str):
    f = await request.form()
    ident = S.identity_from_headers(request.headers, _import_secret())
    action = f.get("action", "answer")
    form = {"answer": f.get("answer", ""), "basis_kind": f.get("basis_kind", "points"),
            "extra_points": f.get("extra_points", ""), "visibility": f.get("visibility", "faq"),
            "faq_question": f.get("faq_question", ""), "alt_questions": f.get("alt_questions", ""),
            "dup_ref": f.get("dup_ref", ""), "cand": f.getlist("cand")}
    with jobs.connect() as conn:
        try:
            if action in ("duplicate", "offtopic"):
                F.close_ticket(conn, number, ident.email, action, form["dup_ref"])
                return RedirectResponse(f"/admin/tickets/{number}", status_code=303)
            points: list[tuple[str, str]] = []
            if form["basis_kind"] == "points":
                points = [tuple(c.split("|", 1)) for c in form["cand"] if "|" in c] + parse_points(form["extra_points"])
                points = list(dict.fromkeys(points))
            elif form["basis_kind"] != "outside":
                raise F.TicketError("Выберите основание ответа.")
            F.answer_ticket(
                conn, number=number, expert_email=ident.email, answer=form["answer"],
                basis_kind=form["basis_kind"], basis_points=points, visibility=form["visibility"],
                faq_question=form["faq_question"],
                alt_questions=form["alt_questions"].splitlines(), embedder=make_embedder())
        except F.TicketError as exc:
            return _ticket_page(request, conn, number, status_code=400, error=str(exc), form=form)
    return RedirectResponse(f"/admin/tickets/{number}", status_code=303)


def _import_secret() -> str:
    from reglament_agent.admin.routes import _secret
    return _secret()


@router.get("/faq", response_class=HTMLResponse)
def faq_list(request: Request, status: str = ""):
    with jobs.connect() as conn:
        rows = F.list_faq(conn, status or None)
    return render("faq.html", request, nav="faq", rows=rows, status=status, statuses=FAQ_STATUS)


@router.get("/faq/{faq_id}", response_class=HTMLResponse)
def faq_page(request: Request, faq_id: int, error: str = ""):
    with jobs.connect() as conn:
        e = F.get_faq(conn, faq_id)
        if e is None:
            return render("forbidden.html", request, 404, message="Запись FAQ не найдена")
        items = []
        for b in e["basis"]:
            st = F.point_state(conn, b) if b.get("number") else None
            if st and st["state"] != "removed":
                st["text"] = (view_text(conn, b["doc_id"], st["number"]) or {}).get("text", st["text"])
            items.append({**b, "now": st})
    return render("faq_entry.html", request, nav="faq", e=e, items=items, error=error,
                  status_text=FAQ_STATUS.get(e["status"], e["status"]),
                  label=F.basis_label(e["basis_kind"], e["basis"]))


@router.post("/faq/{faq_id}/{action}")
def faq_action(faq_id: int, action: str):
    status = {"confirm": "active", "delete": "deleted"}.get(action)
    if status is None:
        return RedirectResponse(f"/admin/faq/{faq_id}", status_code=303)
    with jobs.connect() as conn:
        try:
            F.set_faq_status(conn, faq_id, status)
        except F.TicketError as exc:
            return RedirectResponse(f"/admin/faq/{faq_id}?error={exc}", status_code=303)
    return RedirectResponse(f"/admin/faq/{faq_id}", status_code=303)
