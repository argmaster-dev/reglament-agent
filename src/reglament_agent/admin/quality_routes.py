"""Expert pages: tickets and FAQ."""
from __future__ import annotations

import asyncio
import re

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from psycopg.rows import dict_row
from starlette.routing import NoMatchFound

from reglament_agent.admin import jobs
from reglament_agent.admin import versions as V
from reglament_agent.admin.routes import render
from reglament_agent.answer import SourceRef
from reglament_agent.chat import session as S
from reglament_agent.ingest.embedding import make_embedder
from reglament_agent.model.ollama import ModelError
from reglament_agent.quality import faq as F
from reglament_agent.quality.mail import REASONS
from reglament_agent.quality.tickets import get_ticket, get_ticket_for_user
from reglament_agent.storage.sources import point_view

router = APIRouter()
expert_router = APIRouter(prefix="/admin")
STATUS = {"open": "открыто", "answered": "отвечено", "closed_duplicate": "закрыто: дубликат",
          "closed_offtopic": "закрыто: не по регламентам"}
MAIL = {"pending": "в очереди", "sent": "отправлено", "error": "не доставлено"}
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
    mails = cur.execute("SELECT kind, status, attempts, last_error FROM mail_outbox WHERE ticket_id=%s ORDER BY id",
                        (t["id"],)).fetchall()
    ans = cur.execute("SELECT * FROM ticket_answers WHERE ticket_id=%s", (t["id"],)).fetchone()
    cands = []
    for c in t["candidates"]:
        version = c.get("version_no") or c.get("version")
        view = view_text(conn, c.get("doc_id"), c.get("point"), version) if c.get("point") else None
        cands.append({**c, "version_no": version, "view": view})
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


@expert_router.get("/tickets", response_class=HTMLResponse)
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


@expert_router.get("/tickets/{number}", response_class=HTMLResponse)
def ticket_page(request: Request, number: str):
    with jobs.connect() as conn:
        return _ticket_page(request, conn, number)


@expert_router.post("/tickets/{number}", response_class=HTMLResponse)
async def ticket_submit(request: Request, number: str):
    form = await request.form()
    return await asyncio.to_thread(_submit_ticket, request, number, form)


def _submit_ticket(request: Request, number: str, f):
    ident = S.identity_from_headers(request.headers, _import_secret())
    action = f.get("action", "answer")
    form = {"answer": f.get("answer", ""), "basis_kind": f.get("basis_kind", "points"),
            "extra_points": f.get("extra_points", ""), "visibility": f.get("visibility", "private"),
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
                alt_questions=form["alt_questions"].splitlines(),
                embedder=make_embedder() if form["visibility"] == "faq" else None,
            )
        except ModelError:
            return _ticket_page(request, conn, number, status_code=503,
                                error="Индекс FAQ недоступен. Повторите отправку.", form=form)
        except F.TicketError as exc:
            return _ticket_page(request, conn, number, status_code=400, error=str(exc), form=form)
    return RedirectResponse(f"/admin/tickets/{number}", status_code=303)


def _import_secret() -> str:
    from reglament_agent.admin.routes import _secret
    return _secret()


@expert_router.get("/faq", response_class=HTMLResponse)
def faq_list(request: Request, status: str = ""):
    with jobs.connect() as conn:
        rows = F.list_faq(conn, status or None)
    return render("faq.html", request, nav="faq", rows=rows, status=status, statuses=FAQ_STATUS)


@expert_router.get("/faq/{faq_id}", response_class=HTMLResponse)
def faq_page(request: Request, faq_id: int, error: str = "", status_code: int = 200):
    with jobs.connect() as conn, V.snapshot(conn):  # text and token come from one snapshot
        e = F.get_faq(conn, faq_id)
        if e is None:
            return render("forbidden.html", request, 404, message="Запись FAQ не найдена")
        items, shown = [], []
        for b in e["basis"]:
            st = F.point_state(conn, b) if b.get("number") else None
            shown.append(st["text"] if st else None)
            if st and st["state"] != "removed":
                st["text"] = (view_text(conn, b["doc_id"], st["number"]) or {}).get("text", st["text"])
            items.append({**b, "now": st})
        token = V.shown_token(conn, e, shown)
    return render("faq_entry.html", request, status_code, nav="faq", e=e, items=items, error=error, token=token,
                  status_text=FAQ_STATUS.get(e["status"], e["status"]),
                  label=F.basis_label(e["basis_kind"], e["basis"]))


@expert_router.post("/faq/{faq_id}/{action}")
async def faq_action(request: Request, faq_id: int, action: str):
    """Plain-card buttons share the single decision path (and history) of the review page."""
    decide = {"confirm": V.confirm_entry, "delete": V.delete_entry}.get(action)
    if decide is None:
        return RedirectResponse(f"/admin/faq/{faq_id}", status_code=303)
    actor = S.identity_from_headers(request.headers, _import_secret()).email
    form = await request.form()
    shown = V.make_shown(form.get("expected_version_id", ""), form.get("expected_hashes", ""))
    with jobs.connect() as conn:
        try:
            decide(conn, faq_id, actor, shown)
        except V.StaleBasis as exc:
            return faq_page(request, faq_id, error=str(exc), status_code=409)
        except V.VersionError as exc:
            return RedirectResponse(f"/admin/faq/{faq_id}?error={exc}", status_code=303)
    return RedirectResponse(f"/admin/faq/{faq_id}", status_code=303)


@router.get("/tickets/{number}", response_class=HTMLResponse)
def ticket_status(request: Request, number: str):
    identity = S.identity_from_headers(request.headers, _import_secret())
    if identity is None:
        return RedirectResponse("/start", status_code=303)

    with jobs.connect() as conn:
        if identity.role == "expert":
            response = _ticket_page(request, conn, number)
        else:
            ticket = get_ticket_for_user(conn, number, identity.email)
            answer = None
            if ticket:
                answer = conn.cursor(row_factory=dict_row).execute(
                    "SELECT expert_email,answer,created_at,visibility,basis_kind,basis "
                    "FROM ticket_answers WHERE ticket_id=%s", (ticket["id"],),
                ).fetchone()
            response = render(
                "employee_ticket.html",
                request,
                200 if ticket else 404,
                ticket=ticket,
                answer=answer,
                answer_basis=F.basis_label(answer["basis_kind"], answer["basis"]) if answer else None,
                status=STATUS.get(ticket["status"], ticket["status"]) if ticket else None,
                delivery=MAIL.get(ticket["delivery"], ticket["delivery"]) if ticket else None,
            )
    response.headers["Cache-Control"] = "no-store"
    return response


router.include_router(expert_router)
