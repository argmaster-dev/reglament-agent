"""Check where each searchable point ranks when queried by its own text.

Section headings (level 1) are titles, not answer units: a query equal to a heading legitimately
returns its sub-points, so they are excluded. Misses are reported with their text."""
from collections.abc import Callable

from psycopg import Connection

from reglament_agent.ingest.text import normalize
from reglament_agent.storage.search import search


def selfcheck(conn: Connection, doc_id: str, progress: Callable[[int, int], None] | None = None) -> dict:
    department, version_id = conn.execute(
        "SELECT department, active_version_id FROM documents WHERE doc_id=%s", (doc_id,)).fetchone()
    rows = conn.execute("SELECT point_id, number, text, embedding::text FROM points "
                        "WHERE version_id=%s AND level > 1 ORDER BY position", (version_id,)).fetchall()
    total = len(rows)
    found = top1 = full_text = 0
    misses, top1_misses = [], []
    for i, (point_id, number, text, embedding) in enumerate(rows, 1):
        vector = [float(x) for x in embedding.strip("[]").split(",")]
        hits = search(conn, text, vector, department=department, limit=3, literal=False)
        literal_hits = search(conn, text, vector, department=department, limit=1, literal=True)
        if literal_hits and literal_hits[0].point_id == point_id:
            full_text += 1
        if any(hit.point_id == point_id for hit in hits):
            found += 1
        else:
            misses.append({"number": number, "text": text})
        first = hits[0] if hits else None
        if first is not None and first.point_id == point_id:
            top1 += 1
        else:
            miss = {"number": number, "text": text}
            if first:
                miss.update(first_number=first.number, first_title=first.doc_title, first_text=first.text,
                            same_text=normalize(first.text) == normalize(text))
            top1_misses.append(miss)
        if progress and (i % 10 == 0 or i == total):
            progress(i, total)
    return {"found": found, "total": total, "top1": top1, "full_text": full_text,
            "misses": misses, "top1_misses": top1_misses}
