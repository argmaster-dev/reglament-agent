"""Load the manifest and publish each regulation independently."""
import argparse
import json
import os
from pathlib import Path

import psycopg

from reglament_agent.quality.faq import recheck_faq_after_publication
from reglament_agent.storage.migrate import migrate
from reglament_agent.storage.publish import publish

from .embedding import open_embedder
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
    with psycopg.connect(dsn, autocommit=True) as conn, open_embedder() as embedder:
        _load(conn, embedder, args)


def _load(conn, embedder, args):
    migrate(conn)
    for card in load_cards(args.directory):
        faq_review = 0

        def check_faq(conn, publication):
            nonlocal faq_review
            faq_review = recheck_faq_after_publication(conn, card.doc_id)

        result = publish(conn, card, (args.directory / card.file).read_bytes(), embedder, args.uploaded_by,
                         before_commit=check_faq)
        print(json.dumps({"doc_id": card.doc_id, "version": result.number, "created": result.created,
                          "version_stats": result.stats, "faq_review": faq_review}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
