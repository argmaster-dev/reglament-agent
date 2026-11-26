"""Lexical and vector search over the same active and scoped set of points."""
from dataclasses import dataclass

from psycopg import Connection, sql

from reglament_agent.ingest.embedding import vector_literal


@dataclass(frozen=True)
class Hit:
    point_id: int
    doc_id: str
    version_id: int
    number: str
    text: str
    score: float


def search(conn: Connection, query: str, vector: list[float], *, department: str | None = None,
           stage: str | None = None, limit: int = 10, exact: bool = False) -> list[Hit]:
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    pool = max(32, 4 * limit)
    filters = [sql.SQL("d.status='active'")]
    params = {"vec": vector_literal(vector), "query": query, "pool": pool, "limit": limit,
              "dept": department, "stage": stage}
    if department is not None:
        filters.append(sql.SQL("d.department=%(dept)s"))
    if stage is not None:
        filters.append(sql.SQL("%(stage)s=ANY(d.stages)"))
    scope = sql.SQL("SELECT p.* FROM documents d JOIN points p ON p.version_id=d.active_version_id "
                    "WHERE {}").format(sql.SQL(" AND ").join(filters))
    with conn.transaction():
        # Exact ordering on small filtered corpora avoids ANN post-filter candidate loss.
        small = conn.execute(sql.SQL("SELECT count(*) <= 1000 FROM ({} LIMIT 1001) s").format(scope), params).fetchone()[0]
        distance = sql.SQL("embedding <=> %(vec)s::vector")
        order = sql.SQL("({}) + 0, point_id").format(distance) if exact or small else distance
        if not exact and not small:
            version = conn.execute("SELECT extversion FROM pg_extension WHERE extname='vector'").fetchone()[0]
            if tuple(int(v) for v in version.split(".")[:2]) >= (0, 8):
                conn.execute("SET LOCAL hnsw.iterative_scan = 'strict_order'")
            conn.execute(sql.SQL("SET LOCAL hnsw.ef_search = {}").format(sql.Literal(max(160, pool))))
        statement = sql.SQL("""
            WITH eligible AS NOT MATERIALIZED ({}), q AS (SELECT websearch_to_tsquery('russian', %(query)s) t),
            v AS (SELECT point_id, {} dist FROM eligible ORDER BY {} LIMIT %(pool)s),
            f AS (SELECT point_id, ts_rank_cd(tsv,q.t) relev FROM eligible,q WHERE tsv @@ q.t
                  ORDER BY relev DESC,point_id LIMIT %(pool)s),
            ranks AS (
                SELECT point_id, row_number() OVER (ORDER BY dist, point_id) AS r FROM v
                UNION ALL SELECT point_id, row_number() OVER (ORDER BY relev DESC,point_id) FROM f
            ), fused AS (SELECT point_id, sum(1.0::float8 / (60 + r)) score FROM ranks GROUP BY point_id)
            SELECT p.point_id,p.doc_id,p.version_id,p.number,p.text,score FROM fused JOIN points p USING(point_id)
            ORDER BY score DESC,p.point_id LIMIT %(limit)s
        """).format(scope, distance, order)
        return [Hit(*row) for row in conn.execute(statement, params)]
