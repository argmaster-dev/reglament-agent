import os
import uuid

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from reglament_agent.storage.migrate import migrate


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
