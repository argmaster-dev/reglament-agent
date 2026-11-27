"""Expert replies scoped to the employee and their originating conversation."""
from __future__ import annotations

import re
from typing import Any, Sequence

from psycopg import Connection
from psycopg.rows import dict_row

from reglament_agent.quality.faq import basis_label


def _notes(conn: Connection, email: str, dialog_id: str | None, *, pending: bool) -> list[dict[str, Any]]:
    conditions = ["n.user_email=%s"]
    params = [email]
    if dialog_id is not None:
        conditions.append("n.dialog_id=%s")
        params.append(dialog_id)
    if pending:
        conditions.append("n.delivered_at IS NULL")
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT n.id,n.dialog_id,n.ticket_number,n.kind,n.payload,n.created_at,"
        "coalesce(n.payload->>'expert_email',a.expert_email) AS expert_email,"
        "coalesce(n.payload->>'visibility',a.visibility) AS visibility "
        "FROM chat_notifications n LEFT JOIN tickets t ON t.number=n.ticket_number AND t.user_email=n.user_email "
        "LEFT JOIN ticket_answers a ON a.ticket_id=t.id WHERE " + " AND ".join(conditions) + " ORDER BY n.id",
        params,
    ).fetchall()


def pending_notifications(conn: Connection, email: str, dialog_id: str | None = None) -> list[dict[str, Any]]:
    return _notes(conn, email, dialog_id, pending=True)


def conversation_notifications(conn: Connection, email: str, dialog_id: str) -> list[dict[str, Any]]:
    return _notes(conn, email, dialog_id, pending=False)


def mark_delivered(conn: Connection, ids: Sequence[int]) -> int:
    if not ids:
        return 0
    return conn.execute(
        "UPDATE chat_notifications SET delivered_at=now() WHERE id = ANY(%s) AND delivered_at IS NULL", (list(ids),)
    ).rowcount


def notification_details(n: dict) -> list[str]:
    if n["kind"] == "closed":
        return []
    payload = n["payload"]
    details = [basis_label(payload.get("basis_kind", "outside"), payload.get("basis", []))]
    if n.get("expert_email"):
        details.append(n["expert_email"])
    visibility = {"private": "только сотруднику", "faq": "опубликовано в FAQ"}.get(n.get("visibility"))
    if visibility:
        details.append(visibility)
    return details


def _plain_markdown(text: str) -> str:
    return re.sub(r"([\x5c`*_{}\[\]()#+!<>|~])", r"\\\1", text)


def notification_markdown(n: dict) -> str:
    date = n["created_at"]
    head = f"**Ответ службы качества · обращение №{n['ticket_number']} · {date:%d.%m.%Y}**"
    body = _plain_markdown(n["payload"].get("answer", ""))
    details = " · ".join(_plain_markdown(s) for s in notification_details(n))
    return f"{head}\n\n{body}" + (f"\n\n_{details}_" if details else "")
