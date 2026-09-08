"""Independent PostgreSQL 16 pools, each restricted to its actual execution identity."""

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import URL

from fleetops.settings import Settings


def create_runtime_engine(settings: Settings, **options) -> Engine:
    """Verify the actual login as well as its URL; a privileged fallback defeats RLS."""
    return _role_engine(settings.database_url, "fleetops_app", **options)


def create_authenticator_engine(settings: Settings, **options) -> Engine:
    """The login-only pool cannot fall back to ordinary app or deployment authority."""
    return _role_engine(settings.authenticator_url, "fleetops_authenticator", **options)


def create_deployment_engine(url: URL, **options) -> Engine:
    """Explicit admin path for initial-user bootstrap, never ordinary requests."""
    return _role_engine(url, "fleetops_migrator", **options)


def _role_engine(url: URL, expected_role: str, **options) -> Engine:
    """Validate both URL intent and actual login on every new physical connection."""
    if url.drivername != "postgresql+psycopg" or url.username != expected_role:
        raise ValueError(f"Connection requires PostgreSQL and {expected_role} credentials")
    engine = create_engine(
        url,
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args={"options": "-csearch_path=pg_catalog"},
        **options,
    )

    @event.listens_for(engine, "connect")
    def verify_runtime(dbapi_connection, _record) -> None:
        try:
            with dbapi_connection.cursor() as cursor:
                cursor.execute(
                    "SELECT current_setting('server_version_num')::int, current_user, session_user"
                )
                version, role, login = cursor.fetchone()
            dbapi_connection.rollback()
            if version // 10000 != 16 or role != expected_role or login != role:
                raise RuntimeError(f"Connection requires PostgreSQL 16 and a {expected_role} login")
        except BaseException:
            # A connect-event rejection happens before the pool owns this connection.
            # Close it explicitly so failed identity validation cannot leak a login.
            dbapi_connection.close()
            raise

    return engine
