"""Resolve a regulation reference to the precise version used in the answer."""
from dataclasses import dataclass

from psycopg import Connection

from reglament_agent.answer import PointView, SourceRef
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
    section_title: str = ""

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
                     ref.sentence, ref.column, neighbours, blocks)


def active_process(conn: Connection, selected: SourceRef, limit: int = 28) -> list[Hit]:
    """Read the selected point first and its process in the same still-active version."""
    rows = conn.execute("""
        SELECT p.point_id,d.doc_id,v.version_id,p.number,p.text,0.0,d.title,d.department,
               v.version_number,p.sentences,p.blocks,p.section_title
        FROM documents d JOIN document_versions v ON v.version_id=d.active_version_id
        JOIN points chosen ON chosen.version_id=v.version_id AND chosen.number=%s
        JOIN points p ON p.version_id=v.version_id AND p.section_number=chosen.section_number
        WHERE d.status='active' AND d.doc_id=%s AND v.version_number=%s AND p.number<>p.section_number
        ORDER BY p.point_id<>chosen.point_id,p.position LIMIT %s
    """, (selected.point, selected.doc_id, selected.version_no, limit))
    return [Hit(*row[:10], read_blocks(row[10]), 0.0, False, row[11]) for row in rows]
