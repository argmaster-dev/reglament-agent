from reglament_agent.ingest.model import Block, BlockKind, ParsedPoint, Table
from reglament_agent.ingest.text import content_hash, split_sentences


def test_point_text_and_sentences():
    p = ParsedPoint(
        number="16.1", level=2, parent="16", section_number="16", section_title="Договоры",
        heading="РП создает Заявку.",
        blocks=[Block(BlockKind.TEXT, "РП создает Заявку на подрядчика в CRM. За подписание отвечает Логист.")],
        position=0,
    )
    assert p.sentences == ["РП создает Заявку на подрядчика в CRM.", "За подписание отвечает Логист."]
    assert content_hash(p.text) == content_hash("  " + p.text.replace(" ", " ") + "\n")


def test_table_column():
    t = Table(headers=["Обязательно", "При наличии"], rows=[["ТКП", "Описание"], ["", "Прочее"]])
    assert t.column("При наличии") == ["Описание", "Прочее"]
    assert t.column("Обязательно") == ["ТКП"]
