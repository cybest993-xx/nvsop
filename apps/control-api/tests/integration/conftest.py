"""Real PostgreSQL for the integration suite, brought up once per session.

Harness §6 keeps this out of `make check`: a developer without Docker must still be able to
run the gate. §4 is why it is not SQLite either — transaction isolation, `JSONB`, timezone
and deferred foreign key behavior differ enough between the two to produce a false green, and
this suite exists precisely to prove the adapter against what production runs.

The schema is created by running the Alembic migrations, not by `metadata.create_all`. The
migrations are what production applies, so this is also the test that they produce the schema
the ORM expects; `create_all` would build the schema from the same model definitions the code
under test uses and could never disagree with them.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker
from testcontainers.community.postgres import PostgresContainer

# The version the center machine's Compose runs (§六). Pinned rather than `latest`, so a
# release upstream cannot change what the suite proved.
POSTGRES_IMAGE = "postgres:17.6-alpine"

CONTROL_API = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    """A migrated database. One container for the whole session — startup is the slow part."""
    with PostgresContainer(POSTGRES_IMAGE, driver="psycopg") as container:
        url = container.get_connection_url()
        configuration = Config(str(CONTROL_API / "alembic.ini"))
        configuration.set_main_option("script_location", str(CONTROL_API / "migrations"))
        configuration.set_main_option("sqlalchemy.url", url)
        command.upgrade(configuration, "head")

        built = create_engine(url)
        yield built
        built.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """A transaction rolled back when the test ends.

    Every test therefore starts from the migrated empty schema without paying for a new
    container, and nothing one test writes can reach another.
    """
    connection = engine.connect()
    transaction = connection.begin()
    opened = sessionmaker(bind=connection)()
    try:
        yield opened
    finally:
        opened.close()
        # A test that provoked an `IntegrityError` has already had its transaction rolled
        # back by the failed statement, so rolling back again warns about a transaction that
        # is no longer associated with the connection.
        if transaction.is_active:
            transaction.rollback()
        connection.close()
