"""Read helpers over the document catalog."""
from psycopg import Connection


def active_departments(conn: Connection) -> list[str]:
    """Distinct departments of active documents, sorted (used for chat clarification buttons)."""
    rows = conn.execute("SELECT DISTINCT department FROM documents "
                        "WHERE status='active' AND active_version_id IS NOT NULL ORDER BY department").fetchall()
    return [r[0] for r in rows]
