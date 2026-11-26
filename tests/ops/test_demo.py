from pathlib import Path

import psycopg

from reglament_agent.ingest.embedding import FakeEmbedder
from reglament_agent.ops.__main__ import main
from reglament_agent.ops.demo import demo_reset

DOCX = Path(__file__).resolve().parents[2] / "docx"


def active(dsn):
    with psycopg.connect(dsn) as c:
        return {r[0] for r in c.execute("SELECT doc_id FROM documents WHERE status='active'")}


def test_reset_loads_all_and_is_repeatable(fresh_db):
    for _ in range(2):
        out = demo_reset(fresh_db, DOCX, embedder=FakeEmbedder())
        assert out["documents_loaded"] == 4 and out["points"] > 0
    assert active(fresh_db) == {"td-project", "dbrct-project", "ib-project", "ib-presale"}


def test_reset_wipes_unknown_tables(fresh_db):
    demo_reset(fresh_db, DOCX, embedder=FakeEmbedder())
    with psycopg.connect(fresh_db, autocommit=True) as c:
        c.execute("CREATE TABLE tickets (id int)")
    demo_reset(fresh_db, DOCX, embedder=FakeEmbedder())
    with psycopg.connect(fresh_db) as c:
        assert c.execute("SELECT to_regclass('tickets')").fetchone()[0] is None


def test_without(fresh_db, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", fresh_db)
    monkeypatch.setenv("EMBEDDER", "fake")
    assert main(["demo-reset", "--without", "td-project", "--yes", "--directory", str(DOCX)]) == 0
    assert '"documents_loaded": 3' in capsys.readouterr().out
    assert active(fresh_db) == {"dbrct-project", "ib-project", "ib-presale"}


def test_refuses_without_yes(fresh_db, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", fresh_db)
    monkeypatch.setenv("EMBEDDER", "fake")
    monkeypatch.delenv("DEMO_RESET_ALLOWED", raising=False)
    assert main(["demo-reset", "--directory", str(DOCX)]) == 2
    with psycopg.connect(fresh_db) as c:
        assert c.execute("SELECT to_regclass('documents')").fetchone()[0] is None
    monkeypatch.setenv("DEMO_RESET_ALLOWED", "1")
    assert main(["demo-reset", "--directory", str(DOCX)]) == 0
