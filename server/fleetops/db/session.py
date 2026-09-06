"""Runtime connection pool restricted to the unprivileged PostgreSQL 16 login."""

from sqlalchemy import Engine, create_engine, event

from fleetops.settings import Settings


def create_runtime_engine(settings: Settings, **options) -> Engine:
    """Verify the actual login as well as its URL; a privileged fallback defeats RLS."""
    engine = create_engine(
        settings.database_url,
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args={"options": "-csearch_path=pg_catalog"},
        **options,
    )

    @event.listens_for(engine, "connect")
    def verify_runtime(dbapi_connection, _record) -> None:
        with dbapi_connection.cursor() as cursor:
            cursor.execute(
                "SELECT current_setting('server_version_num')::int, current_user, session_user"
            )
            version, role, login = cursor.fetchone()
        dbapi_connection.rollback()
        if version // 10000 != 16 or role != "fleetops_app" or login != role:
            raise RuntimeError("Runtime requires PostgreSQL 16 and a fleetops_app login")

    return engine
