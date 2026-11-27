"""Resolve a regulation reference to the precise version used in the answer."""
from dataclasses import dataclass

from psycopg import Connection

from psycopg.types.json import Jsonb

from reglament_agent.answer import PointView, Practice, SourceRef
from reglament_agent.ingest.model import Block, BlockKind, Table


def read_blocks(records: list[dict]) -> list[Block]:
    return [Block(BlockKind(r["kind"]), r["text"], Table(**r["table"]) if r["table"] else None)
            for r in records]


def validate_anchor(sentences, blocks, sentence, column):
    if sentence is not None and not 0 <= sentence < len(sentences):
        raise ValueError("Sentence index is outside the referenced point")
    if column is not None and not any(b.table and column in b.table.headers for b in blocks):
        raise ValueError("Column is absent from the referenced point")


@dataclass(frozen=True)
class Hit:
    point_id: int
    doc_id: str
    version_id: int
    number: str
    text: str
    score: float
    doc_title: str
    department: str
    version_no: int
    sentences: list[str]
    blocks: list[Block]
    similarity: float
    lexical_match: bool

    def source_ref(self, *, sentence: int | None = None, column: str | None = None) -> SourceRef:
        validate_anchor(self.sentences, self.blocks, sentence, column)
        return SourceRef(self.doc_id, self.doc_title, self.department, self.version_no,
                         self.number, sentence=sentence, column=column)


def point_view(conn: Connection, ref: SourceRef) -> PointView:
    if ref.faq_id is not None:
        raise ValueError("FAQ references are resolved by the FAQ source")
    with conn.transaction():
        row = conn.execute("""
            SELECT d.title,d.department,d.stages,v.version_number,v.created_at,p.section_number,
                   p.section_title,p.number,p.sentences,p.blocks,p.version_id,p.position
            FROM documents d JOIN document_versions v USING(doc_id) JOIN points p USING(version_id)
            WHERE d.doc_id=%s AND v.version_number=%s AND p.number=%s AND d.status='active'
        """, (ref.doc_id, ref.version_no, ref.point)).fetchone()
        if row is None:
            raise LookupError("Regulation point or version is unavailable")
        title, department, stages, version, uploaded, section, heading, number, sentences, data, vid, pos = row
        blocks = read_blocks(data)
        validate_anchor(sentences, blocks, ref.sentence, ref.column)
        neighbours = conn.execute("SELECT number,heading FROM points WHERE version_id=%s "
                                  "AND position IN (%s,%s) ORDER BY position", (vid, pos-1, pos+1)).fetchall()
    pieces = number.split(".")
    path = [f"Раздел {section}. {heading}", *(".".join(pieces[:i]) for i in range(2, len(pieces)+1))]
    return PointView(title, department, stages, version, uploaded.isoformat(), path, number, sentences,
                     ref.sentence, ref.column, neighbours, blocks,
                     practice_for_point(conn, ref.doc_id, vid, number))


def practice_for_point(conn: Connection, doc_id: str, version_id: int, number: str) -> list[Practice]:
    """Published FAQ whose basis names this point of the ACTIVE version, newest confirmation first.

    Read-only. `review`, `deleted`, «вне регламентов» and whole-document entries are never returned.
    """
    rows = conn.execute("""
        SELECT f.id, f.question, f.answer, coalesce(f.reviewed_at, f.created_at)
        FROM faq_entries f JOIN documents d ON d.doc_id=%s AND d.status='active' AND d.active_version_id=%s
        WHERE f.status='active' AND f.basis_kind='points' AND f.basis @> %s::jsonb
        ORDER BY coalesce(f.reviewed_at, f.created_at) DESC, f.id DESC
    """, (doc_id, version_id, Jsonb([{"doc_id": doc_id, "number": number}]))).fetchall()
    return [Practice(r[0], r[1], r[2], r[3].date().isoformat()) for r in rows]


def faq_confirmations(conn: Connection, faq_ids) -> dict[int, str]:
    """ISO confirmation date for each of the given FAQ ids that is still published."""
    rows = conn.execute("SELECT id, coalesce(reviewed_at, created_at) FROM faq_entries "
                        "WHERE id=ANY(%s) AND status='active'", (list(faq_ids),)).fetchall()
    return {r[0]: r[1].date().isoformat() for r in rows}


def resolve_point(conn: Connection, doc_title: str, department: str, number: str) -> SourceRef | None:
    """Reference to a point of the active version of the named document, or None if it is not there."""
    row = conn.execute("""
        SELECT d.doc_id, d.title, d.department, v.version_number, p.number
        FROM documents d JOIN document_versions v ON v.version_id=d.active_version_id
        JOIN points p ON p.version_id=v.version_id
        WHERE d.status='active' AND d.title=%s AND d.department=%s AND p.number=%s
        ORDER BY d.doc_id LIMIT 1
    """, (doc_title, department, number)).fetchone()
    return SourceRef(row[0], row[1], row[2], row[3], row[4]) if row else None
