"""Expert answers waiting for an employee in the chat (no Chainlit imports, unit-testable)."""
from __future__ import annotations

from typing import Any, Sequence

from psycopg import Connection
from psycopg.rows import dict_row

from reglament_agent.quality.faq import basis_label


def pending_notifications(conn: Connection, email: str) -> list[dict[str, Any]]:
    """Undelivered notifications of exactly this user, oldest first."""
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT id, user_email, dialog_id, ticket_number, kind, payload, created_at FROM chat_notifications "
        "WHERE user_email=%s AND delivered_at IS NULL ORDER BY id", (email,)).fetchall()


def mark_delivered(conn: Connection, ids: Sequence[int]) -> int:
    if not ids:
        return 0
    return conn.execute("UPDATE chat_notifications SET delivered_at=now() "
                        "WHERE id = ANY(%s) AND delivered_at IS NULL", (list(ids),)).rowcount


def notification_markdown(n: dict) -> str:
    p = n["payload"]
    head = f"**Ответ службы качества · обращение №{n['ticket_number']}**"
    if n["kind"] == "closed":
        return f"{head}\n\n{p.get('answer', '')}"
    return f"{head}\n\n{p.get('answer', '')}\n\n_{basis_label(p.get('basis_kind', 'outside'), p.get('basis', []))}_"
