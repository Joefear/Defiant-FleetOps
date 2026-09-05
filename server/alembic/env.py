"""Migrate only as the DDL owner on PostgreSQL 16; no app/superuser fallback."""

import os
from logging.config import fileConfig

from sqlalchemy import create_engine, pool
from sqlalchemy.engine import make_url

from alembic import context
from fleetops.db.metadata import metadata

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def run_migrations() -> None:
    if context.is_offline_mode():
        raise RuntimeError(
            "Offline migration is disabled: PostgreSQL version and role must be checked"
        )
    url = make_url(os.environ["FLEETOPS_MIGRATOR_URL"])
    # Checked twice on purpose: the URL check fails fast and offline, the server check
    # below catches a URL that lies (wrong role behind a proxy, SET ROLE in a pooler).
    if url.drivername != "postgresql+psycopg" or url.username != "fleetops_migrator":
        raise RuntimeError(
            "Migrations require postgresql+psycopg and fleetops_migrator credentials"
        )
    # A fixed catalog-only migration search path keeps explicit FleetOps schemas
    # distinct from the default schema during reflection and autogeneration, and
    # forces every migration to name fleetops.<object> explicitly, so a table can
    # never be created in whatever schema happened to be first on someone's path.
    # NullPool: one connection, used once, disposed; nothing here is worth pooling.
    engine = create_engine(
        url, poolclass=pool.NullPool, connect_args={"options": "-csearch_path=pg_catalog"}
    )
    try:
        with engine.connect() as connection:
            version, actor, session_actor = connection.exec_driver_sql(
                "SELECT current_setting('server_version_num')::int, current_user, session_user"
            ).one()
            # session_user must match too: a different login that merely SET ROLE to the
            # migrator would pass a current_user check while leaving the real actor hidden.
            if version // 10000 != 16 or actor != "fleetops_migrator" or session_actor != actor:
                raise RuntimeError("Migrations require PostgreSQL 16 and a fleetops_migrator login")
            # SQLAlchemy autobegins a transaction for the probe query; end it so the
            # transactional-DDL block Alembic opens below is the only one in flight.
            connection.commit()
            # The bookkeeping table lives in the migrator-owned schema. public is closed
            # and the migration search_path is catalog-only, so an unqualified default
            # would have nowhere legal to go, which is exactly the point: migration state
            # is migrator-owned and invisible to the runtime role.
            context.configure(
                connection=connection,
                target_metadata=metadata,
                version_table_schema="fleetops",
                include_schemas=True,
                compare_type=True,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


run_migrations()
