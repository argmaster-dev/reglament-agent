"""Reset the stand to a known demo state: wipe the schema, migrate, load the cards."""
import json
import os
import time
from pathlib import Path

import httpx
import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from reglament_agent.ingest.embedding import Embedder, FakeEmbedder, OllamaEmbedder
from reglament_agent.ingest.parser import load_cards
from reglament_agent.storage.migrate import migrate
from reglament_agent.storage.publish import publish


class ResetRefused(RuntimeError):
    pass


def allowed(yes: bool) -> bool:
    return yes or os.getenv("DEMO_RESET_ALLOWED") == "1"


def recreate_schema(conn: psycopg.Connection, schema: str) -> None:
    """Drop everything in the schema, so new migrations' tables need no listing here."""
    ident = sql.Identifier(schema)
    conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(ident))
    conn.execute(sql.SQL("CREATE SCHEMA {}").format(ident))
    if schema == "public":
        conn.execute("GRANT ALL ON SCHEMA public TO PUBLIC")


def demo_reset(dsn: str, directory: Path, *, without: tuple[str, ...] = (), embedder: Embedder,
               schema: str = "public") -> dict:
    started = time.monotonic()
    cards = load_cards(directory)
    unknown = set(without) - {c.doc_id for c in cards}
    if unknown:
        raise ValueError(f"Unknown doc_id in --without: {sorted(unknown)}")
    if schema != "public":
        dsn = make_conninfo(dsn, options=f"-csearch_path={schema},public")
    loaded, points = [], 0
    with psycopg.connect(dsn, autocommit=True) as conn:
        recreate_schema(conn, schema)
        migrate(conn)
        for card in cards:
            if card.doc_id in without:
                continue
            result = publish(conn, card, (directory / card.file).read_bytes(), embedder, "demo-reset")
            loaded.append(card.doc_id)
            points += result.stats["points"]
    return {"documents": loaded, "documents_loaded": len(loaded), "skipped": list(without),
            "points": points, "seconds": round(time.monotonic() - started, 2)}


def make_embedder(kind: str):
    """Return (embedder, closer)."""
    if kind == "fake":
        return FakeEmbedder(), None
    if kind != "ollama":
        raise ValueError("EMBEDDER must be ollama or fake")
    client = httpx.Client(base_url=os.getenv("MODEL_OLLAMA_URL", "http://127.0.0.1:11434"),
                          timeout=180, trust_env=False)
    return OllamaEmbedder(client, os.getenv("MODEL_EMBED", "bge-m3")), client
