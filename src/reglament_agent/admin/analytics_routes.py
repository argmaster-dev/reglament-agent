"""Analytics, request journal and economics (quality service only; guarded by the /admin middleware)."""
from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from reglament_agent.admin import analytics as A
from reglament_agent.admin import jobs
from reglament_agent.admin.routes import render
from reglament_agent.answer import UserContext
from reglament_agent.chat import session as S
from reglament_agent.quality.mail import MailSettings
from reglament_agent.quality.tickets import create_ticket

router = APIRouter(prefix="/admin/analytics")


def _period(value: str) -> str:
    return value if value in A.PERIODS else A.DEFAULT_PERIOD


def _csv(text: str, name: str) -> Response:
    return Response(text, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("", response_class=HTMLResponse)
def analytics(request: Request, period: str = A.DEFAULT_PERIOD):
    period = _period(period)
    with jobs.connect() as conn:
        data = A.summary(conn, period)
    return render("analytics.html", request, nav="analytics", periods=A.PERIODS, tickets=A.TICKET_OUTCOMES,
                  labels=A.OUTCOMES, **data)


@router.get("/requests.csv")
def requests_csv(period: str = A.DEFAULT_PERIOD):
    with jobs.connect() as conn:
        return _csv(A.requests_csv(A.fetch(conn, _period(period))), f"requests-{_period(period)}.csv")


@router.get("/economics.csv")
def economics_csv(period: str = A.DEFAULT_PERIOD):
    with jobs.connect() as conn:
        return _csv(A.economics_csv(A.fetch(conn, _period(period))), f"economics-{_period(period)}.csv")


@router.get("/journal", response_class=HTMLResponse)
def journal(request: Request, period: str = A.DEFAULT_PERIOD):
    period = _period(period)
    with jobs.connect() as conn:
        rows = A.fetch(conn, period, limit=200)
    items = [{"r": r, "outcome": A.OUTCOMES.get(A.outcome(r), A.outcome(r)), "ms": A.elapsed(r),
              "ticket": r["tickets"]} for r in rows]
    return render("analytics_journal.html", request, nav="analytics", periods=A.PERIODS, period=period, items=items)


@router.get("/journal/by-ticket/{number}")
def journal_by_ticket(request: Request, number: str):
    with jobs.connect() as conn:
        row = conn.execute("SELECT id FROM dialog_requests WHERE answer->'ticket'->>'number' = %s "
                           "ORDER BY created_at DESC LIMIT 1", (number,)).fetchone()
        if row is None:  # ticket from a 👎 with forwarding: message_id = "feedback:<request id>"
            row = conn.execute(
                "SELECT r.id FROM tickets t JOIN dialog_requests r ON r.user_email=t.user_email AND "
                "(t.message_id = 'feedback:' || r.id::text OR (r.dialog_id=t.dialog_id AND r.message_id=t.message_id)) "
                "WHERE t.number = %s ORDER BY r.created_at DESC LIMIT 1", (number,)).fetchone()
    if row is None:
        return render("forbidden.html", request, 404, message="Запрос по этому обращению не найден")
    return RedirectResponse(f"/admin/analytics/journal/{row[0]}", status_code=303)


@router.post("/faq")
def create_faq(request: Request, group: str = Form(""), period: str = Form(A.DEFAULT_PERIOD)):
    """Repeated-question group -> ticket (idempotent); the expert publishes the FAQ from its card as usual."""
    ident = S.identity_from_headers(request.headers, jobs_secret())
    with jobs.connect() as conn:
        found = next((g for g in A.repeated(A.fetch(conn, _period(period))) if g["hash"] == group), None)
        if found is None:
            return render("forbidden.html", request, 404, message="Группа вопросов не найдена")
        mail = MailSettings()
        info = create_ticket(
            conn, user=UserContext(ident.email, "expert", "analytics", f"analytics:{group}"),
            question=found["example"], restated=found["example"], reason="no_basis", profile={},
            clarifications=[], candidates=[], service_email=mail.service_email, public_base_url=mail.public_base_url)
    return RedirectResponse(f"/admin/tickets/{info.number}", status_code=303)


def jobs_secret() -> str:
    from reglament_agent.admin.routes import _secret
    return _secret()


@router.get("/journal/{request_id}", response_class=HTMLResponse)
def journal_entry(request: Request, request_id: str):
    with jobs.connect() as conn:
        d = A.request_detail(conn, request_id)
    if d is None:
        return render("forbidden.html", request, 404, message="Запрос не найден")
    return render("analytics_request.html", request, nav="analytics", **d)
