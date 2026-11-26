"""Ticket creation (atomic with its outbox mail) and reads."""
from __future__ import annotations

import datetime as dt
from typing import Any

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from reglament_agent.answer import TicketInfo, UserContext

from .mail import REASONS, build_body, message_id_for, subject_for


def _transferred(restated: str | None, profile: dict, clarifications: list) -> list[str]:
    items = ["вопрос"]
    dept = (profile or {}).get("department")
    if profile and any(profile.values()):
        items.append(f"профиль ({dept})" if dept else "профиль")
    n = len(clarifications or [])
    if n:
        items.append(f"{n} {_plural(n)}")
    return items


def _plural(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "уточнение"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "уточнения"
    return "уточнений"


def _info(row: dict, delivery: str | None) -> TicketInfo:
    return TicketInfo(number=row["number"], reason=REASONS.get(row["reason"], row["reason"]),
                      delivery=delivery or "pending",
                      transferred=_transferred(row["restated_question"], row["profile"],
                                               row["clarifications"]))


_SELECT = ("SELECT t.*, (SELECT o.status FROM mail_outbox o WHERE o.ticket_id = t.id "
           "AND o.kind = 'ticket_created') AS delivery FROM tickets t ")


def _row_info(row: dict) -> TicketInfo:
    return _info(row, row["delivery"])


def create_ticket(conn: Connection, *, user: UserContext, question: str, restated: str | None,
                  reason: str, profile: dict, clarifications: list[dict], candidates: list[dict],
                  service_email: str, public_base_url: str) -> TicketInfo:
    with conn.transaction():
        cur = conn.cursor(row_factory=dict_row)
        if user.message_id is not None:
            cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                        (f"ticket:{user.email}:{user.message_id}",))
            cur.execute(_SELECT + "WHERE t.user_email = %s AND t.message_id = %s",
                        (user.email, user.message_id))
            if (row := cur.fetchone()) is not None:
                return _row_info(row)
        year = dt.datetime.now(dt.timezone.utc).year
        cur.execute("INSERT INTO ticket_counters (year, last_value) VALUES (%s, 1) "
                    "ON CONFLICT (year) DO UPDATE SET last_value = ticket_counters.last_value + 1 "
                    "RETURNING last_value", (year,))
        number = f"{year}-{cur.fetchone()['last_value']:04d}"
        cur.execute(
            "INSERT INTO tickets (number, user_email, dialog_id, message_id, question, "
            "restated_question, reason, profile, clarifications, candidates) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (number, user.email, user.dialog_id, user.message_id, question, restated, reason,
             Jsonb(profile or {}), Jsonb(clarifications or []), Jsonb(candidates or [])))
        ticket_id = cur.fetchone()["id"]
        body = build_body(number=number, question=question, restated=restated, reason=reason,
                          user_email=user.email, profile=profile, clarifications=clarifications,
                          candidates=candidates, public_base_url=public_base_url)
        cur.execute(
            "INSERT INTO mail_outbox (ticket_id, kind, to_addr, cc_addr, subject, body, "
            "message_id_header) VALUES (%s,'ticket_created',%s,%s,%s,%s,%s)",
            (ticket_id, service_email, user.email, subject_for(number, question), body,
             message_id_for(number)))
        return TicketInfo(number=number, reason=REASONS.get(reason, reason), delivery="pending",
                          transferred=_transferred(restated, profile, clarifications))


def get_ticket(conn: Connection, number: str) -> dict[str, Any] | None:
    """Full ticket for experts; includes `delivery`."""
    return conn.cursor(row_factory=dict_row).execute(
        _SELECT + "WHERE t.number = %s", (number,)).fetchone()


def get_ticket_for_user(conn: Connection, number: str, email: str) -> dict[str, Any] | None:
    row = get_ticket(conn, number)
    return row if row is not None and row["user_email"] == email else None


def list_user_tickets(conn: Connection, email: str) -> list[dict[str, Any]]:
    return conn.cursor(row_factory=dict_row).execute(
        _SELECT + "WHERE t.user_email = %s ORDER BY t.created_at DESC, t.id DESC", (email,)).fetchall()
