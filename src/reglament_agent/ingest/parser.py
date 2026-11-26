"""Read literal regulation point numbers and preserve the order of their OOXML blocks."""
import io
import re
import tomllib
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from .model import Block, BlockKind, DocumentCard, ParsedDocument, ParsedPoint, Table
from .text import normalize

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
NUMBER = re.compile(r"^(\d+(?:\.\d+)*)\.\s+(.*)$")
NOTE = re.compile(r"^(?:важно\b|примечани[ея]\b|исключени[ея]\b)", re.I)
BULLET = re.compile(r"^(?:[-–—•]|[a-zа-я][.)])\s+", re.I)


def load_cards(directory: Path) -> list[DocumentCard]:
    with (directory / "documents.toml").open("rb") as f:
        entries = tomllib.load(f)["document"]
    cards = [DocumentCard(**{**entry, "stages": tuple(entry["stages"])}) for entry in entries]
    if len({c.doc_id for c in cards}) != len(cards):
        raise ValueError("Duplicate document identifier")
    for c in cards:
        if Path(c.file).name != c.file or not c.stages or not set(c.stages) <= {"presale", "project"}:
            raise ValueError(f"Invalid file or stages for {c.doc_id}")
    return cards


def _paragraph(node: ET.Element) -> str:
    parts = []
    for child in node.iter():
        if child.tag == W + "t":
            parts.append(child.text or "")
        elif child.tag in (W + "tab", W + "br", W + "cr"):
            parts.append(" ")
    return normalize("".join(parts))


def _table(node: ET.Element) -> Block:
    rows = [
        [" ".join(filter(None, (_paragraph(p) for p in cell.findall(W + "p"))))
         for cell in row.findall(W + "tc")]
        for row in node.findall(W + "tr")
    ]
    if not rows or not rows[0] or any(len(row) != len(rows[0]) for row in rows):
        raise ValueError("Table has missing or irregular columns")
    table = Table(rows[0], rows[1:])
    lines = [" | ".join(table.headers)]
    lines.extend(" | ".join(f"{h}: {v}" for h, v in zip(table.headers, row)) for row in table.rows)
    return Block(BlockKind.TABLE, "\n".join(lines), table)


def parse_docx(data: bytes) -> ParsedDocument:
    with ZipFile(io.BytesIO(data)) as archive:
        part = archive.getinfo("word/document.xml")
        if part.file_size > 64 * 1024 * 1024:
            raise ValueError("Document XML exceeds 64 MiB")
        body = ET.fromstring(archive.read(part)).find(W + "body")
    if body is None:
        raise ValueError("DOCX has no document body")
    title, points, warnings = [], [], []
    headings, last, seen = {}, {}, set()
    for node in body:
        if node.tag == W + "tbl":
            if points:
                points[-1].blocks.append(_table(node))
            else:
                warnings.append("Таблица вне пункта")
            continue
        if node.tag != W + "p":
            continue
        text = _paragraph(node)
        if not text:
            continue
        match = NUMBER.match(text)
        if not match:
            if not points:
                title.append(text)
                continue
            kind = BlockKind.TEXT
            if NOTE.match(text):
                kind = BlockKind.NOTE
            elif node.find(f"{W}pPr/{W}numPr") is not None or BULLET.match(text):
                kind = BlockKind.LIST_ITEM
            points[-1].blocks.append(Block(kind, text))
            continue
        number, heading = match.groups()
        parts = number.split(".")
        parent = ".".join(parts[:-1]) or None
        expected = last.get(parent, 0) + 1
        if number in seen:
            warnings.append(f"Дубль пункта {number}")
        elif int(parts[-1]) != expected:
            warnings.append(f"Нарушение нумерации перед {number}, ожидался номер {expected}")
        if parent and parent not in seen:
            warnings.append(f"У пункта {number} отсутствует родитель {parent}")
        seen.add(number)
        last[parent] = int(parts[-1])
        if parent is None:
            headings[number] = heading
        points.append(ParsedPoint(
            number, len(parts), parent, parts[0], headings.get(parts[0], ""), heading,
            [Block(BlockKind.TEXT, heading)], len(points),
        ))
    if not points:
        warnings.append("Весь текст вне нумерованных пунктов")
    return ParsedDocument(" ".join(title), points, warnings)
