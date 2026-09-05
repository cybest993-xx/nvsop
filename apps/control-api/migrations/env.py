"""Alembic's entrypoint: what the schema is compared against, and how it connects.

Every module's tables are imported here. That is a deliberate exception to the rule that a
module reaches another only through its `api.py` (§5.16) — nothing is being called, and the
imports exist so that one `MetaData` describes the whole schema. Without them, `autogenerate`
would compare the database against a partial model and offer to drop every table it could not
see.

The connection URL comes from the same `Settings` object the application uses, so a migration
runs against the database the deployment is configured for and a credential lives in exactly
one place (§六).
"""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import create_engine

# Imported for the side effect of registering the tables on `Table.metadata`. `noqa: F401`
# because nothing in this file references the names.
from factory_sop.auth.adapters import tables as auth_tables  # noqa: F401
from factory_sop.persistence import Table, database_url
from factory_sop.settings import Settings

target_metadata = Table.metadata


def _url() -> str:
    """Resolve the URL: the configured one if a caller set it, else the environment.

    The integration suite sets `sqlalchemy.url` on the config object to point at its
    container. A deployment sets none, and the environment is then the only source (§六).
    """
    configured = context.config.get_main_option("sqlalchemy.url")
    return configured or database_url(Settings.from_environment(os.environ))


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting. `alembic upgrade head --sql`, for a review."""
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations against the configured database."""
    engine = create_engine(_url())
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # Without this, a column whose type changed is silently not detected.
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
