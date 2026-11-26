"""Operations commands: demo-reset."""
import argparse
import json
import os
import sys
from pathlib import Path

from reglament_agent.ingest.embedding import open_embedder

from .demo import allowed, demo_reset


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    reset = parser.add_subparsers(dest="command", required=True).add_parser(
        "demo-reset", help="WIPE the database schema and reload the demo regulations")
    reset.add_argument("--without", action="append", default=[], metavar="DOC_ID")
    reset.add_argument("--yes", action="store_true", help="confirm data wipe (or DEMO_RESET_ALLOWED=1)")
    reset.add_argument("--directory", type=Path, default=Path("docx"))
    args = parser.parse_args(argv)
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        parser.error("Set DATABASE_URL")
    schema = os.getenv("DEMO_RESET_SCHEMA", "public")
    kind = os.getenv("EMBEDDER", "ollama")
    print(f"demo-reset: will DROP schema '{schema}' (all app data), re-run migrations, "
          f"load {args.directory}/ without {args.without or 'nothing'}, embedder={kind}", file=sys.stderr)
    if not allowed(args.yes):
        print("Refused: pass --yes or set DEMO_RESET_ALLOWED=1", file=sys.stderr)
        return 2
    with open_embedder(kind) as embedder:
        summary = demo_reset(dsn, args.directory, without=tuple(args.without), embedder=embedder, schema=schema)
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
