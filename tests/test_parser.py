from collections import Counter
from pathlib import Path

from reglament_agent.ingest.model import BlockKind
from reglament_agent.ingest.parser import load_cards, parse_docx

CORPUS = Path(__file__).resolve().parents[1] / "docx"


def test_four_regulations_and_precise_references():
    documents = {c.doc_id: parse_docx((CORPUS / c.file).read_bytes()) for c in load_cards(CORPUS)}
    levels = {doc_id: dict(Counter(p.level for p in doc.points)) for doc_id, doc in documents.items()}
    assert levels == {
        "td-project": {1: 24, 2: 148, 3: 23}, "dbrct-project": {1: 23, 2: 123, 3: 21},
        "ib-project": {1: 34, 2: 145, 3: 58, 4: 3}, "ib-presale": {1: 16, 2: 88, 3: 12},
    }
    assert all(doc.title and not doc.warnings for doc in documents.values())
    td = documents["td-project"]
    assert td.get("16.1").sentences[0] == "РП создает Заявку на подрядчика в CRM."
    assert "отвечает Логист" in td.get("16.1").sentences[1]
    assert any(b.kind == BlockKind.NOTE and "2 недели" in b.text for b in td.get("1.5").blocks)
    ib = documents["ib-project"]
    assert ib.get("5.1.3.1").parent == "5.1.3"
    table = next(b.table for b in ib.get("1.2").blocks if b.table)
    assert table.headers == ["Обязательно", "При наличии"]
    assert len(table.rows) == 7
    assert table.rows[-1][0] == "" and table.rows[-1][1]
    assert any(b.kind == BlockKind.LIST_ITEM for p in td.points for b in p.blocks)


def test_reports_gaps_duplicates_and_orphan_text():
    from io import BytesIO
    from zipfile import ZipFile

    def document(paragraphs):
        xml = '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
        for text in paragraphs:
            xml += f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"
        data = BytesIO()
        with ZipFile(data, "w") as archive:
            archive.writestr("word/document.xml", xml + "</w:body></w:document>")
        return parse_docx(data.getvalue())

    parsed = document(["Регламент", "1. Раздел", "1.2. Правило", "a. Деталь", "1.2. Другое"])
    assert parsed.title == "Регламент"
    assert parsed.points[1].blocks[-1].kind == BlockKind.LIST_ITEM
    assert any("Дубль" in w for w in parsed.warnings) and any("Нарушение" in w for w in parsed.warnings)
    assert document(["Только заголовок"]).warnings == ["Весь текст вне нумерованных пунктов"]
