"""Apply the product migrations before starting application services."""

import os

import psycopg

from reglament_agent.storage.migrate import migrate


def main() -> None:
    with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
        migrate(connection)


if __name__ == "__main__":
    main()
