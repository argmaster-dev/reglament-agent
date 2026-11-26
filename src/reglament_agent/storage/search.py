"""Rank active regulation points using Russian word stems and cosine similarity."""
import re
from collections import defaultdict

from psycopg import Connection

from reglament_agent.dialog.lexicon import expand_query, words
from reglament_agent.ingest.embedding import vector_literal

from .sources import Hit, read_blocks

# Conversational words add noise to an OR query. PostgreSQL additionally applies
# its Russian stop list and morphology to both indexed texts and these terms.
QUESTION_WORDS = set("какой какая какие какое каких кто что где когда сколько можно мне нужно "
                     "меня как пожалуйста подскажите подскажи должен должна должны ли".split())


# These forms widen retrieval only; the answer must still cite the actual wording.
_ABBREVIATION = re.compile(r"\b[A-ZА-ЯЁ][A-ZА-ЯЁ0-9]+\b")
_RETRIEVAL_FORMS = (
    (re.compile(r"\bподпис\w*"), "подписание подписывать"),
    (re.compile(r"\bсоглашени\w*"), "договор"),
    (re.compile(r"\bвнешн\w+\s+исполнител\w*"), "подрядчик субподрядчик"),
)


def lexical_query(text: str) -> str:
    expanded = expand_query(text)
    additions = [terms for pattern, terms in _RETRIEVAL_FORMS if pattern.search(text.lower())]
    tokens = dict.fromkeys(w for w in words(" ".join([expanded, *additions])) if w not in QUESTION_WORDS)
    return " OR ".join(tokens)


# All departments and both retrieval paths see the same active versions in one SQL snapshot.
_RANK = """
    WITH domains AS (
        SELECT d.department, array_agg(v.version_id) versions,
               sum((v.stats->>'points')::bigint) > 1000 AND NOT %(exact)s AS use_ann
        FROM documents d JOIN document_versions v ON v.version_id=d.active_version_id
        WHERE d.status='active' AND (%(dept)s::text IS NULL OR d.department=%(dept)s)
              AND (%(stage)s::text IS NULL OR %(stage)s=ANY(d.stages))
        GROUP BY d.department
    ), q AS (SELECT websearch_to_tsquery('russian', %(query)s) terms,
                    phraseto_tsquery('russian', %(original)s) phrase,
                    plainto_tsquery('russian', nullif(%(abbr)s, '')) abbr)
    SELECT p.point_id,p.doc_id,p.version_id,p.number,p.text,top.score,d.title,d.department,
           dv.version_number,p.sentences,p.blocks,1 - (p.embedding <=> %(vec)s::vector),
           p.tsv @@ q.terms
    FROM domains dom CROSS JOIN q CROSS JOIN LATERAL (
        WITH eligible AS NOT MATERIALIZED (
            SELECT point_id, embedding, tsv FROM points WHERE version_id=ANY(dom.versions)
        ), ann AS MATERIALIZED (
            SELECT point_id, embedding <=> %(vec)s::vector dist FROM eligible WHERE dom.use_ann
            ORDER BY embedding <=> %(vec)s::vector LIMIT %(pool)s
        ), strategy AS (
            SELECT dom.use_ann AND (SELECT count(*) FROM ann) = %(pool)s AS ready
        ), v AS (
            SELECT point_id,dist FROM ann WHERE (SELECT ready FROM strategy)
            UNION ALL
            (SELECT point_id,embedding <=> %(vec)s::vector dist FROM eligible
             WHERE NOT (SELECT ready FROM strategy)
             ORDER BY (embedding <=> %(vec)s::vector) + 0,point_id LIMIT %(pool)s)
        ), f AS (
            SELECT point_id, ts_rank_cd(tsv,q.terms) * (1 + coalesce((tsv @@ q.abbr)::int,0)) relev
            FROM eligible WHERE tsv @@ q.terms
            ORDER BY relev DESC,point_id LIMIT %(pool)s
        ), phrases AS (
            SELECT point_id,ts_rank_cd(tsv,q.phrase) relev FROM eligible
            WHERE numnode(q.phrase)>1 AND tsv @@ q.phrase ORDER BY relev DESC,point_id LIMIT %(pool)s
        ), ranks AS (
            SELECT point_id,row_number() OVER (ORDER BY dist,point_id) r FROM v
            UNION ALL SELECT point_id,row_number() OVER (ORDER BY relev DESC,point_id) FROM f
            UNION ALL SELECT point_id,row_number() OVER (ORDER BY relev DESC,point_id) FROM phrases
        )
        SELECT point_id,sum(1.0::float8 / (60 + r)) score FROM ranks GROUP BY point_id
        ORDER BY score DESC,point_id LIMIT %(limit)s
    ) top JOIN points p ON p.point_id=top.point_id JOIN documents d ON d.doc_id=p.doc_id
          JOIN document_versions dv ON dv.version_id=p.version_id
    ORDER BY top.score DESC,p.point_id
"""


def search(conn: Connection, query: str, vector: list[float], *, department: str | None = None,
           stage: str | None = None, limit: int = 10, exact: bool = False) -> list[Hit]:
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    if not query.strip():
        return []
    pool = max(32, 4 * limit)
    params = {"vec": vector_literal(vector), "query": lexical_query(query), "original": query,
              "dept": department, "stage": stage, "pool": pool, "limit": limit, "exact": exact,
              "abbr": " ".join(_ABBREVIATION.findall(query))}
    with conn.transaction():
        if not exact:
            version = conn.execute("SELECT extversion FROM pg_extension WHERE extname='vector'").fetchone()[0]
            if tuple(int(v) for v in version.split(".")[:2]) >= (0, 8):
                conn.execute("SET LOCAL hnsw.iterative_scan = 'strict_order'")
            conn.execute("SELECT set_config('hnsw.ef_search', %s, true)", (str(max(160, pool)),))
        hits = [Hit(*row[:10], read_blocks(row[10]), *row[11:]) for row in conn.execute(_RANK, params)]
    if department is not None or len(hits) <= limit:
        return hits
    # Per-department RRF scores use local ranks. Use cosine similarity to break ties across groups.
    hits.sort(key=lambda h: (-h.score, -h.similarity, h.point_id))
    groups = defaultdict(list)
    for hit in hits:
        groups[hit.department].append(hit)
    matched = [group for group in groups.values() if any(h.lexical_match for h in group)]
    quota = min(3, limit // len(matched)) if matched else 0
    reserved = {h.point_id for group in matched for h in group[:quota]}
    remaining = [hit for hit in hits if hit.point_id not in reserved]
    keep = reserved.union(hit.point_id for hit in remaining[:limit - len(reserved)])
    return [h for h in hits if h.point_id in keep]
