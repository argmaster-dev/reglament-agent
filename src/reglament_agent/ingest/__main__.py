"""Load the manifest and publish each regulation independently."""
import argparse
import asyncio
import json
import os
from pathlib import Path

import psycopg

from reglament_agent.model import OllamaClient
from reglament_agent.storage.migrate import migrate
from reglament_agent.storage.publish import publish

from .embedding import OllamaEmbedder
from .parser import load_cards


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    load = parser.add_subparsers(dest="command", required=True).add_parser("load")
    load.add_argument("directory", type=Path)
    load.add_argument("--uploaded-by", default="ingest-cli")
    args = parser.parse_args()
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        parser.error("Set DATABASE_URL to a PostgreSQL database with pgvector")
    with psycopg.connect(dsn, autocommit=True) as conn, asyncio.Runner() as runner:
        client = OllamaClient()
        try:
            _load(conn, OllamaEmbedder(client, runner), args)
        finally:
            runner.run(client.aclose())


def _load(conn, embedder, args):
    migrate(conn)
    for card in load_cards(args.directory):
        result = publish(conn, card, (args.directory / card.file).read_bytes(), embedder, args.uploaded_by)
        print(json.dumps({"doc_id": card.doc_id, "version": result.number, "created": result.created,
                          "version_stats": result.stats}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
