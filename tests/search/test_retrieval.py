import math
from pathlib import Path

import pytest

from reglament_agent.answer import SourceRef
from reglament_agent.ingest.embedding import FakeEmbedder, vector_literal
from reglament_agent.ingest.parser import load_cards
from reglament_agent.storage.publish import publish
from reglament_agent.storage.retrieve import FAQHit, retrieve
from reglament_agent.storage.search import lexical_query, search
from reglament_agent.storage.sources import point_view

CORPUS = Path(__file__).resolve().parents[2] / "docx"
CARDS = load_cards(CORPUS)


def test_table_evidence_and_scoped_references(isolated):
    conn, _ = isolated
    c = next(c for c in CARDS if c.doc_id == "ib-project")
    embedder = FakeEmbedder()
    publish(conn, c, (CORPUS / c.file).read_bytes(), embedder, "tests")
    question = "Какие документы обязательно вложить в карточку проекта?"
    hits = search(conn, question, embedder.embed([question])[0], department=c.department, limit=50)
    hit = next(h for h in hits if h.number == "1.2")
    ref = hit.source_ref(column="Обязательно")
    view = point_view(conn, ref)
    assert view.column == "Обязательно" and view.version_no == 1
    table = next(b.table for b in view.blocks if b.table)
    assert len(table.column("Обязательно")) == 4 and len(table.column("При наличии")) == 7
    assert view.path[-1] == "1.2" and view.neighbours
    with pytest.raises(ValueError):
        hit.source_ref(column="несуществующая колонка")


def test_large_scoped_top_k_matches_brute_force(isolated):
    conn, _ = isolated
    card = CARDS[0]
    version = publish(conn, card, (CORPUS / card.file).read_bytes(), FakeEmbedder(), "test")
    # A reproducible 1,300-point collection with unique directions; a second department
    # contains real points and must be excluded by the same SQL snapshot.
    other = CARDS[-1]
    publish(conn, other, (CORPUS / other.file).read_bytes(), FakeEmbedder(), "test")
    vid = version.version_id
    with conn.transaction():
        conn.execute("DELETE FROM points WHERE version_id=%s", (vid,))
        with conn.cursor().copy("""
            COPY points(doc_id,version_id,number,level,section_number,section_title,heading,text,
                        blocks,sentences,position,sha256,embedding) FROM STDIN
        """) as copy:
            for i in range(1300):
                angle = i * (2.0 * math.pi / 1300)
                vec = [math.cos(angle), math.sin(angle)] + [0.0] * 1022
                copy.write_row((card.doc_id, vid, str(i+1), 1, str(i+1), "Раздел", "Регламент",
                                "Регламент", "[]", ["Регламент"], i, str(i), vector_literal(vec)))
        conn.execute("UPDATE document_versions SET stats=jsonb_set(stats,'{points}','1300') WHERE version_id=%s",
                     (vid,))
    conn.execute("ANALYZE points")
    vec = [math.cos(1.021), math.sin(1.021)] + [0.0] * 1022
    oracle = conn.execute("""
        SELECT p.point_id FROM points p JOIN documents d ON p.version_id=d.active_version_id
        WHERE d.status='active' AND d.department=%s AND %s=ANY(d.stages)
        ORDER BY (p.embedding <=> %s::vector) + 0,p.point_id LIMIT 10
    """, (card.department, "presale", vector_literal(vec))).fetchall()
    opts = dict(department=card.department, stage="presale")
    for exact in (False, True):
        hits = search(conn, "несуществующийтермин", vec, exact=exact, **opts)
        assert [h.point_id for h in hits] == [row[0] for row in oracle]


def test_retrieve_published_faq_with_scope(isolated):
    conn, _ = isolated

    class Published:
        def search_published(self, conn, question, vector, *, department, stage, limit):
            assert "пресейл" in question.lower() and department == "Дирекция ИБ" and stage == "presale"
            def source(number):
                return SourceRef("faq", "FAQ службы качества", "Служба качества", 1, "", faq_id=number)
            return [FAQHit(source(7), "Пояснение эксперта", None, department, stage),
                    FAQHit(source(9), "Другое подразделение", None, "ДБРЦТ", stage)]
    vector = FakeEmbedder().embed(["ЗНП"])[0]
    batch = retrieve(conn, "Что такое ЗНП?", vector, department="Дирекция ИБ", stage="presale",
                     faq=Published())
    assert not batch.points and [r.source.faq_id for r in batch.faq] == [7]
    assert retrieve(conn, "Без поставщика FAQ", vector).faq == []


def test_presale_deadlines_without_profile(isolated):
    conn, _ = isolated
    embedder = FakeEmbedder()
    for card in CARDS:
        publish(conn, card, (CORPUS / card.file).read_bytes(), embedder, "tests")

    for question in ("За сколько нужно подать заявку на пресейл?", "За сколько подавать заявку на пресейл?"):
        hits = search(conn, question, embedder.embed([question])[0])
        matched = {(hit.doc_id, hit.number) for hit in hits if hit.lexical_match}
        assert {("td-project", "1.5"), ("dbrct-project", "1.5")} <= matched


@pytest.mark.parametrize(("short", "long"), [
    ("ЗНП", "заявка на пресейл"),
    ("ТКП", "технико-коммерческое предложение"),
])
def test_search_terms_include_both_glossary_forms(short, long):
    assert short.lower() in lexical_query(long).split(" OR ")
    assert set(long.replace("-", " ").split()) <= set(lexical_query(short).split(" OR "))
