import os
import uuid

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo



@pytest.fixture(scope="session")
def db_uri(tmp_path_factory):
    if os.getenv("DATABASE_URL"):
        yield os.environ["DATABASE_URL"]
    else:
        from pgserver import get_server
        server = get_server(tmp_path_factory.mktemp("pgsql_ops"), cleanup_mode="delete")
        try:
            yield server.get_uri()
        finally:
            server.cleanup()


@pytest.fixture
def fresh_db(db_uri):
    """A throwaway database: demo-reset drops the whole public schema."""
    name = "ops_" + uuid.uuid4().hex[:12]
    with psycopg.connect(db_uri, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            yield make_conninfo(db_uri, dbname=name)
        finally:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
