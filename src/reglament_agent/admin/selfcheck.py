"""Search self-check without model calls: every point must be found (top-3) by its own text."""
from collections.abc import Callable

from psycopg import Connection

from reglament_agent.storage.search import search


def selfcheck(conn: Connection, doc_id: str, progress: Callable[[int, int], None] | None = None) -> dict:
    department, version_id = conn.execute(
        "SELECT department, active_version_id FROM documents WHERE doc_id=%s", (doc_id,)).fetchone()
    rows = conn.execute("SELECT point_id, number, text, embedding::text FROM points "
                        "WHERE version_id=%s ORDER BY position", (version_id,)).fetchall()
    total, found, top1, misses = len(rows), 0, 0, []
    for i, (point_id, number, text, embedding) in enumerate(rows, 1):
        vector = [float(x) for x in embedding.strip("[]").split(",")]
        # Short items («Бюджет проекта;») can lose the first place to a lexical neighbour in the
        # hybrid fusion, so "found" means top-3 and top1 is reported separately.
        hits = search(conn, text, vector, department=department, limit=3)
        if any(h.point_id == point_id for h in hits):
            found += 1
            top1 += bool(hits and hits[0].point_id == point_id)
        else:
            misses.append(number)
        if progress and (i % 10 == 0 or i == total):
            progress(i, total)
    return {"found": found, "total": total, "top1": top1, "misses": misses}
