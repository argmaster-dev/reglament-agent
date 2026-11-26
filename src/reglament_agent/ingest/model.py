"""Contract between the DOCX parser and the storage/publication layer.

The parser turns one regulation file into a ParsedDocument. Storage publishes it as a new
document version (ADR-010). Neither side depends on the other's internals.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class BlockKind(StrEnum):
    """Kind of an attached block inside a point."""

    TEXT = "text"  # the numbered paragraph itself or a plain continuation paragraph
    NOTE = "note"  # «Важно: …», «Примечания: …», «Исключение: …»
    LIST_ITEM = "list_item"  # bullet / «a.» / «-» item belonging to the point
    TABLE = "table"  # a table placed under the point


@dataclass(frozen=True)
class Table:
    headers: list[str]  # header row cells, e.g. ["Обязательно", "При наличии"]
    rows: list[list[str]]  # body rows, same length as headers; empty cell = ""

    def column(self, header: str) -> list[str]:
        """Non-empty cells of one column, top to bottom."""
        i = self.headers.index(header)
        return [r[i] for r in self.rows if r[i].strip()]


@dataclass(frozen=True)
class Block:
    kind: BlockKind
    text: str  # plain text of the block; for TABLE a readable rendering «Заголовок: ячейка; …»
    table: Table | None = None


@dataclass(frozen=True)
class ParsedPoint:
    """A numbered unit of a regulation: section heading («16.») or point («16.1.», «5.1.3.1.»)."""

    number: str  # without trailing dot: "16", "16.1", "5.1.3.1"
    level: int  # count of number components: "16" -> 1, "5.1.3.1" -> 4
    parent: str | None  # "5.1.3" for "5.1.3.1"; None for level 1
    section_number: str  # top-level number, "5" for "5.1.3.1"
    section_title: str  # title of the level-1 section, e.g. "Назначение РП"
    heading: str  # text of the numbered paragraph after the number
    blocks: list[Block]  # blocks[0] is the numbered paragraph (TEXT); then attached blocks in order
    position: int  # order of the point within the document, 0-based

    @property
    def text(self) -> str:
        """Full text used for search, hashing and display (number prefix excluded)."""
        return "\n".join(b.text for b in self.blocks if b.text)

    @property
    def sentences(self) -> list[str]:
        """Sentence split used for claim references (FR-01/FR-02). Implemented in parser.sentences."""
        from reglament_agent.ingest.text import split_sentences

        return split_sentences(self.text)


@dataclass
class ParsedDocument:
    title: str  # text before the first numbered paragraph
    points: list[ParsedPoint]
    warnings: list[str] = field(default_factory=list)  # numbering gaps, duplicates, orphan text…

    def get(self, number: str) -> ParsedPoint:
        for p in self.points:
            if p.number == number:
                return p
        raise KeyError(number)


@dataclass(frozen=True)
class DocumentCard:
    """Metadata set by the quality service on upload (docx/documents.toml)."""

    doc_id: str
    title: str
    department: str
    stages: tuple[str, ...]  # subset of {"presale", "project"}
    file: str
