"""Analytics, request journal and economics (quality service only; guarded by the /admin middleware)."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from reglament_agent.admin import analytics as A
from reglament_agent.admin import jobs
from reglament_agent.admin.routes import render

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
              "ticket": A.ticket_number(r)} for r in rows]
    return render("analytics_journal.html", request, nav="analytics", periods=A.PERIODS, period=period, items=items)


@router.get("/journal/{request_id}", response_class=HTMLResponse)
def journal_entry(request: Request, request_id: str):
    with jobs.connect() as conn:
        d = A.request_detail(conn, request_id)
    if d is None:
        return render("forbidden.html", request, 404, message="Запрос не найден")
    return render("analytics_request.html", request, nav="analytics", **d)
