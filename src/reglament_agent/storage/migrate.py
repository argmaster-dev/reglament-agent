"""Apply ordered SQL migrations once. Caller supplies an idle connection."""
from pathlib import Path

from psycopg import Connection


def migrate(conn: Connection) -> None:
    directory = Path(__file__).parent / "migrations"
    if not directory.is_dir():
        directory = Path(__file__).resolve().parents[3] / "migrations"
    scripts = sorted(directory.glob("[0-9]*.sql"))
    if not scripts:
        raise RuntimeError("No SQL migrations installed")
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(728641921)")
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name text PRIMARY KEY, "
                     "applied_at timestamptz NOT NULL DEFAULT now())")
        for script in scripts:
            if conn.execute("SELECT 1 FROM schema_migrations WHERE name=%s", (script.name,)).fetchone():
                continue
            conn.execute(script.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO schema_migrations(name) VALUES (%s)", (script.name,))
