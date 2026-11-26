import io
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from zipfile import ZipFile

import httpx
import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from reglament_agent.ingest.embedding import FakeEmbedder, OllamaEmbedder, vector_literal
from reglament_agent.ingest.parser import load_cards
from reglament_agent.storage.migrate import migrate
from reglament_agent.storage.publish import publish
from reglament_agent.storage.search import search

CORPUS = Path(__file__).resolve().parents[1] / "docx"
CARDS = load_cards(CORPUS)


@pytest.fixture(scope="session")
def db_uri(tmp_path_factory):
    if os.getenv("DATABASE_URL"):
        yield os.environ["DATABASE_URL"]
    else:
        from pgserver import get_server
        server = get_server(tmp_path_factory.mktemp("pgsql"), cleanup_mode="delete")
        try:
            yield server.get_uri()
        finally:
            server.cleanup()


@pytest.fixture
def isolated(db_uri):
    schema = "test_" + uuid.uuid4().hex
    with psycopg.connect(db_uri, autocommit=True) as admin:
        admin.execute("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public")
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            dsn = make_conninfo(db_uri, options=f"-csearch_path={schema},public")
            with psycopg.connect(dsn, autocommit=True) as conn:
                migrate(conn)
                migrate(conn)
                yield conn, dsn
        finally:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def new_revision(data):
    output = io.BytesIO()
    with ZipFile(io.BytesIO(data)) as src, ZipFile(output, "w") as dst:
        for item in src.infolist():
            part = src.read(item)
            if item.filename == "word/document.xml":
                part = part.replace("Логист".encode(), "Координатор".encode(), 1)
            dst.writestr(item, part)
    return output.getvalue()


def test_versions_reuse_and_rollback(isolated):
    conn, _ = isolated
    card = CARDS[0]
    data = (CORPUS / card.file).read_bytes()
    fake = FakeEmbedder()
    first = publish(conn, card, data, fake, "tester")
    again = publish(conn, card, data, fake, "tester")
    assert first.created and not again.created and first.version_id == again.version_id
    conn.execute("ALTER TABLE points ADD CONSTRAINT inject_failure CHECK (version_id=1 OR number <> '16.1')")
    changed = new_revision(data)
    with pytest.raises(psycopg.errors.CheckViolation):
        publish(conn, card, changed, fake, "tester")
    assert conn.execute("SELECT active_version_id FROM documents").fetchone()[0] == first.version_id
    assert conn.execute("SELECT count(*) FROM document_versions").fetchone()[0] == 1
    conn.execute("ALTER TABLE points DROP CONSTRAINT inject_failure")
    updated = publish(conn, card, changed, fake, "tester")
    assert updated.number == 2 and updated.stats["vectors_computed"] == 1
    assert updated.stats["vectors_reused"] == first.stats["points"] - 1
    assert conn.execute("SELECT source_docx FROM document_versions WHERE version_id=%s",
                        (updated.version_id,)).fetchone()[0] == changed
    assert publish(conn, card, data, fake, "tester").version_id == first.version_id
    hits = search(conn, "логист", fake.embed(["логист"])[0])
    assert hits and {h.version_id for h in hits} == {updated.version_id}


def test_concurrent_upload(isolated):
    conn, dsn = isolated
    card = CARDS[-1]
    data = (CORPUS / card.file).read_bytes()

    def load_once():
        with psycopg.connect(dsn, autocommit=True) as independent:
            return publish(independent, card, data, FakeEmbedder(), "tester")
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda _: load_once(), range(2)))
    assert {r.created for r in results} == {False, True}
    assert len({r.version_id for r in results}) == 1
    assert conn.execute("SELECT count(*) FROM document_versions").fetchone()[0] == 1


def test_scoped_search(isolated):
    conn, _ = isolated
    fake = FakeEmbedder()
    for card in CARDS:
        publish(conn, card, (CORPUS / card.file).read_bytes(), fake, "tester")
    query = "заявка пресейл срок конкурса"
    vector = fake.embed([query])[0]
    assert {"td-project", "dbrct-project"} <= {h.doc_id for h in search(conn, query, vector, limit=20)}
    opts = dict(stage="presale", department="Дирекция ИБ", limit=8)
    hits = search(conn, query, vector, **opts)
    assert hits and {h.doc_id for h in hits} == {"ib-presale"}
    assert hits == search(conn, query, vector, **opts, exact=True)
    # Compare both ranked lists against an exhaustive independent reference on filtered rows.
    eligible = conn.execute("""
        SELECT p.point_id,embedding <=> %s::vector,ts_rank_cd(tsv,q),
               tsv @@ q FROM points p JOIN documents d ON p.version_id=d.active_version_id
        CROSS JOIN websearch_to_tsquery('russian', %s) q
        WHERE d.department=%s AND %s=ANY(d.stages) AND d.status='active'
        """, (vector_literal(vector), query, opts["department"], opts["stage"])).fetchall()
    by_vector = sorted(eligible, key=lambda r: (r[1], r[0]))[:32]
    by_words = sorted((r for r in eligible if r[3]), key=lambda r: (-r[2], r[0]))[:32]
    scores = {}
    for ranking in (by_vector, by_words):
        for rank, row in enumerate(ranking, 1):
            scores[row[0]] = scores.get(row[0], 0.0) + 1.0 / (60 + rank)
    assert [h.point_id for h in hits] == sorted(scores, key=lambda i: (-scores[i], i))[:8]
    conn.execute("UPDATE documents SET status='inactive' WHERE doc_id='ib-presale'")
    assert search(conn, query, vector, **opts) == []


def test_ollama_batches_on_cpu():
    calls = []
    import json

    def handler(req):
        assert req.url.path == "/api/embed"
        payload = json.loads(req.content)
        assert payload["options"]["num_gpu"] == 0 and payload["model"] == "bge-m3"
        calls.append(payload["input"])
        return httpx.Response(200, json={"embeddings": FakeEmbedder().embed(payload["input"])})
    with httpx.Client(base_url="http://localhost", transport=httpx.MockTransport(handler)) as client:
        assert len(OllamaEmbedder(client, batch_size=2).embed(["a","b","c"])) == 3
    assert calls == [["a", "b"], ["c"]]


def test_large_corpus_with_filtered_vector_candidates(isolated):
    from dataclasses import replace
    conn, _ = isolated
    fake = FakeEmbedder()
    card = CARDS[0]
    data = (CORPUS / card.file).read_bytes()
    for i in range(6):
        publish(conn, replace(card, doc_id=f"large-{i}"), data, fake, "tester")
    publish(conn, CARDS[-1], (CORPUS / CARDS[-1].file).read_bytes(), fake, "tester")
    query = "РП создает Заявку на подрядчика в CRM."
    hits = search(conn, query, fake.embed([query])[0], department=card.department)
    assert len(hits) == 10 and all(h.doc_id.startswith("large-") for h in hits)
    assert any(h.number == "16.1" for h in hits)
