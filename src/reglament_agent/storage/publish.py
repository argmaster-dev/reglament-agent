"""Atomically publish a whole source file and its point vectors."""
import hashlib
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

from psycopg import Connection
from psycopg.types.json import Jsonb

from reglament_agent.ingest.embedding import Embedder, vector_literal
from reglament_agent.ingest.model import DocumentCard
from reglament_agent.ingest.parser import parse_docx
from reglament_agent.ingest.text import content_hash


@dataclass(frozen=True)
class Publication:
    version_id: int
    number: int
    stats: dict
    created: bool


def _existing(conn, doc_id, digest):
    row = conn.execute("SELECT version_id, version_number, stats FROM document_versions "
                       "WHERE doc_id=%s AND sha256=%s", (doc_id, digest)).fetchone()
    return Publication(*row, created=False) if row else None


def publish(conn: Connection, card: DocumentCard, data: bytes, embedder: Embedder,
            uploaded_by: str) -> Publication:
    digest = hashlib.sha256(data).hexdigest()
    parsed = parse_docx(data)
    if not parsed.points or len({p.number for p in parsed.points}) != len(parsed.points):
        raise ValueError(f"Cannot publish {card.doc_id}: {parsed.warnings}")
    hashes = [content_hash(p.text) for p in parsed.points]
    with conn.transaction():
        prior = _existing(conn, card.doc_id, digest)
        if prior:
            return prior
        cached = dict(conn.execute(
            "SELECT DISTINCT ON (sha256) sha256, embedding::text FROM points "
            "WHERE doc_id=%s AND sha256=ANY(%s) ORDER BY sha256, version_id DESC",
            (card.doc_id, hashes)).fetchall())

    missing = {h: p.text for h, p in zip(hashes, parsed.points) if h not in cached}
    embeddings = embedder.embed(list(missing.values())) if missing else []
    if len(embeddings) != len(missing):
        raise ValueError("Missing embeddings for publication")
    cached.update((h, vector_literal(v)) for h, v in zip(missing, embeddings))
    stats = {"points": len(parsed.points), "levels": dict(Counter(str(p.level) for p in parsed.points)),
             "warnings": parsed.warnings, "vectors_computed": len(missing),
             "vectors_reused": len(hashes) - len(missing)}

    with conn.transaction():
        # Serialize competing publishers even when no row exists yet for this document.
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 7))", (card.doc_id,))
        prior = _existing(conn, card.doc_id, digest)
        if prior:
            return prior
        conn.execute("""
            INSERT INTO documents(doc_id,title,department,stages) VALUES (%s,%s,%s,%s)
            ON CONFLICT (doc_id) DO UPDATE SET title=excluded.title, department=excluded.department,
                stages=excluded.stages
        """, (card.doc_id, card.title, card.department, list(card.stages)))
        vid, number = conn.execute("""
            INSERT INTO document_versions(doc_id,version_number,sha256,filename,uploaded_by,stats,source_docx)
            SELECT %s, coalesce(max(version_number),0)+1, %s,%s,%s,%s,%s
            FROM document_versions WHERE doc_id=%s RETURNING version_id,version_number
        """, (card.doc_id, digest, Path(card.file).name, uploaded_by, Jsonb(stats), data, card.doc_id)).fetchone()
        with conn.cursor() as cur:
            cur.executemany("""
                INSERT INTO points (version_id,doc_id,number,level,parent,section_number,section_title,
                    heading,text,blocks,sentences,position,sha256,embedding)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::vector)
            """, ((vid, card.doc_id, p.number, p.level, p.parent, p.section_number, p.section_title,
                   p.heading, p.text, Jsonb([asdict(block) for block in p.blocks]), p.sentences,
                   p.position, h, cached[h]) for p, h in zip(parsed.points, hashes)))
        conn.execute("UPDATE documents SET active_version_id=%s,status='active' WHERE doc_id=%s",
                     (vid, card.doc_id))
    return Publication(vid, number, stats, created=True)
