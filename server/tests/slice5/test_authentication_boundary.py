"""ADR-006 attacks use independent real logins and exact persisted PostgreSQL facts."""

import secrets
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import psycopg
import pytest
from server.tests.auth_context import set_authenticated
from server.tests.slice5.conftest import FUNCTION_SIGNATURE, call_transition, headers
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import DBAPIError
from uuid6 import uuid7

from fleetops.auth import hash_password, token_digest
from fleetops.db.metadata import actors, assets, items, metadata, sessions, users
from fleetops.db.session import create_authenticator_engine, create_runtime_engine
from fleetops.db.tenancy import set_credential_context, set_organization
from fleetops.settings import Settings

CURRENT = text("SELECT fleetops.current_authenticated_actor()")
ISSUE = text("SELECT fleetops.issue_session(:user, :session, :digest, :expiry)")


@pytest.fixture
def other_human(asset_data, migrator_connection):
    """A second existing HUMAN in A's tenant has an independently random credential."""
    a = asset_data[0]
    other = SimpleNamespace(
        org_id=a.org_id, actor_id=uuid7(), user_id=uuid7(), raw_token=secrets.token_urlsafe(32)
    )
    migrator_connection.execute(
        actors.insert().values(
            id=other.actor_id,
            org_id=a.org_id,
            type="HUMAN",
            display_name="Second human",
            created_by_actor_id=a.actor_id,
        )
    )
    migrator_connection.execute(
        users.insert().values(
            id=other.user_id,
            org_id=a.org_id,
            actor_id=other.actor_id,
            username="second-human",
            password_hash=hash_password(secrets.token_urlsafe(32)),
        )
    )
    migrator_connection.execute(
        sessions.insert().values(
            id=uuid7(),
            org_id=a.org_id,
            user_id=other.user_id,
            token_digest=token_digest(other.raw_token),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )
    migrator_connection.commit()
    return other


@pytest.mark.parametrize("table", ["users", "sessions"])
@pytest.mark.parametrize("operation", ["SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE"])
def test_app_has_no_auth_table_authority(table, operation, asset_data, app_connection):
    a = asset_data[0]
    statement = {
        "SELECT": f"SELECT * FROM fleetops.{table}",
        "INSERT": f"INSERT INTO fleetops.{table} DEFAULT VALUES",
        "UPDATE": f"UPDATE fleetops.{table} SET active=false",
        "DELETE": f"DELETE FROM fleetops.{table}",
        "TRUNCATE": f"TRUNCATE fleetops.{table}",
    }[operation]
    with pytest.raises(DBAPIError) as error:
        with app_connection.begin():
            set_authenticated(app_connection, a)
            app_connection.exec_driver_sql(statement)
    assert error.value.orig.sqlstate == "42501"


def test_app_cannot_mint_known_credential_for_existing_same_org_user(
    asset_data, other_human, app_connection, migrator_connection
):
    digest = secrets.token_bytes(32)
    with pytest.raises(DBAPIError) as error:
        with app_connection.begin():
            set_authenticated(app_connection, asset_data[0])
            app_connection.execute(
                sessions.insert().values(
                    id=uuid7(),
                    org_id=other_human.org_id,
                    user_id=other_human.user_id,
                    token_digest=digest,
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
            )
    assert error.value.orig.sqlstate == "42501"
    assert (
        migrator_connection.execute(
            select(sessions.c.id).where(sessions.c.token_digest == digest)
        ).all()
        == []
    )


@pytest.mark.parametrize(
    "source,target",
    [
        ("fleetops_app", "fleetops_authenticator"),
        ("fleetops_app", "fleetops_migrator"),
        ("fleetops_authenticator", "fleetops_app"),
        ("fleetops_authenticator", "fleetops_migrator"),
    ],
)
def test_execution_domains_have_no_role_escalation(source, target, database):
    engine = create_engine(database.url(source), hide_parameters=True)
    try:
        with engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT current_user, session_user").one() == (
                source,
                source,
            )
            assert connection.exec_driver_sql(
                "SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolinherit, "
                "rolreplication, rolbypassrls FROM pg_roles WHERE rolname=current_user"
            ).one() == (True, False, False, False, False, False, False)
            assert (
                connection.exec_driver_sql(
                    "SELECT 1 FROM pg_auth_members WHERE member=current_user::regrole"
                ).all()
                == []
            )
            with pytest.raises(DBAPIError) as error:
                connection.exec_driver_sql(f"SET ROLE {target}")
            assert error.value.orig.sqlstate == "42501"
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "table",
    [
        "actors",
        "parties",
        "items",
        "facilities",
        "locations",
        "assets",
        "asset_transitions",
        "users",
        "sessions",
    ],
)
@pytest.mark.parametrize("operation", ["INSERT", "UPDATE", "DELETE", "TRUNCATE"])
def test_authenticator_has_no_direct_mutation(
    table, operation, asset_data, authenticator_connection
):
    statement = {
        "INSERT": f"INSERT INTO fleetops.{table} DEFAULT VALUES",
        "UPDATE": f"UPDATE fleetops.{table} SET org_id=org_id",
        "DELETE": f"DELETE FROM fleetops.{table}",
        "TRUNCATE": f"TRUNCATE fleetops.{table}",
    }[operation]
    with pytest.raises(DBAPIError) as error:
        with authenticator_connection.begin():
            set_organization(authenticator_connection, asset_data[0].org_id)
            authenticator_connection.exec_driver_sql(statement)
    assert error.value.orig.sqlstate == "42501"


@pytest.mark.parametrize(
    "statement",
    [
        "CREATE TABLE fleetops.auth_attack(id integer)",
        "CREATE TABLE public.auth_attack(id integer)",
        "CREATE TEMP TABLE auth_attack(id integer)",
    ],
)
def test_authenticator_cannot_create_objects(statement, authenticator_connection):
    with pytest.raises(DBAPIError) as error:
        authenticator_connection.exec_driver_sql(statement)
    assert error.value.orig.sqlstate == "42501"


def test_authenticator_cannot_transition(asset_data, authenticator_connection):
    with pytest.raises(DBAPIError) as error:
        with authenticator_connection.begin():
            set_authenticated(authenticator_connection, asset_data[0])
            call_transition(authenticator_connection, asset_data[0])
    assert error.value.orig.sqlstate == "42501"


def test_authenticator_login_reads_are_minimal_and_rls_scoped(
    asset_data, authenticator_connection, migrator_connection
):
    allowed = {
        "users": {"id", "org_id", "actor_id", "username", "password_hash", "active"},
        "actors": {"id", "org_id", "type", "active"},
    }
    for name, columns in allowed.items():
        table = metadata.tables[f"fleetops.{name}"]
        for column in table.c:
            assert authenticator_connection.exec_driver_sql(
                "SELECT has_column_privilege(current_user, %s, %s, 'SELECT')",
                (f"fleetops.{name}", column.name),
            ).scalar_one() is (column.name in columns)
        assert authenticator_connection.execute(select(table.c.id)).all() == []
    authenticator_connection.rollback()
    a, b = asset_data
    with authenticator_connection.begin():
        set_organization(authenticator_connection, a.org_id)
        assert authenticator_connection.execute(select(users.c.id)).scalars().all() == [a.user_id]
        assert (
            authenticator_connection.execute(
                select(users.c.id).where(users.c.id == b.user_id)
            ).all()
            == []
        )


@pytest.mark.parametrize(
    "condition",
    [
        "valid",
        "missing_org",
        "cross_org",
        "missing_user",
        "inactive_user",
        "inactive_actor",
        "short_digest",
        "null_digest",
        "expired",
        "infinite",
        "too_long",
    ],
)
def test_issue_session_validates_every_boundary(
    condition, asset_data, authenticator_connection, migrator_connection
):
    a, b = asset_data
    params = dict(
        user=a.user_id,
        session=uuid7(),
        digest=secrets.token_bytes(32),
        expiry=datetime.now(UTC) + timedelta(hours=1),
    )
    if condition in {"inactive_user", "inactive_actor"}:
        table = users if condition == "inactive_user" else actors
        target = a.user_id if table is users else a.actor_id
        migrator_connection.execute(table.update().where(table.c.id == target).values(active=False))
        migrator_connection.commit()
    params.update(
        {
            "cross_org": {"user": b.user_id},
            "missing_user": {"user": uuid7()},
            "short_digest": {"digest": b"short"},
            "null_digest": {"digest": None},
            "expired": {"expiry": datetime.now(UTC) - timedelta(seconds=1)},
            "infinite": {"expiry": "infinity"},
            "too_long": {"expiry": datetime.now(UTC) + timedelta(days=2)},
        }.get(condition, {})
    )

    def issue():
        with authenticator_connection.begin():
            if condition != "missing_org":
                set_organization(authenticator_connection, a.org_id)
            return authenticator_connection.execute(ISSUE, params).scalar_one()

    if condition == "valid":
        assert issue() == ""  # PostgreSQL void exposes no row or credential material.
        row = (
            migrator_connection.execute(select(sessions).where(sessions.c.id == params["session"]))
            .mappings()
            .one()
        )
        assert row["org_id"] == a.org_id and row["user_id"] == a.user_id
        assert row["token_digest"] == params["digest"] and row["expires_at"] > row["created_at"]
        migrator_connection.rollback()
        with pytest.raises(DBAPIError) as error:
            issue()
        assert error.value.orig.sqlstate == "23505"
    else:
        with pytest.raises(DBAPIError) as error:
            issue()
        assert error.value.orig.sqlstate == (
            "42501"
            if condition
            in {"missing_org", "cross_org", "missing_user", "inactive_user", "inactive_actor"}
            else "23514"
        )
        assert (
            migrator_connection.execute(
                select(sessions.c.id).where(sessions.c.id == params["session"])
            ).all()
            == []
        )


def test_app_cannot_invoke_session_issuer(asset_data, app_connection):
    a = asset_data[0]
    with pytest.raises(DBAPIError) as error:
        with app_connection.begin():
            set_authenticated(app_connection, a)
            app_connection.execute(
                ISSUE,
                dict(
                    user=a.user_id,
                    session=uuid7(),
                    digest=secrets.token_bytes(32),
                    expiry=datetime.now(UTC) + timedelta(hours=1),
                ),
            )
    assert error.value.orig.sqlstate == "42501"


@pytest.mark.parametrize(
    "condition",
    [
        "valid",
        "org_only",
        "digest_only",
        "malformed",
        "guessed",
        "revoked",
        "expired",
        "inactive_user",
        "inactive_actor",
        "org_mismatch",
    ],
)
def test_current_actor_requires_valid_credential_and_matching_org(
    condition, asset_data, app_connection, migrator_connection
):
    a, b = asset_data
    if condition in {"revoked", "expired", "inactive_user", "inactive_actor"}:
        table, target, values = {
            "revoked": (sessions, sessions.c.user_id == a.user_id, {"active": False}),
            "expired": (
                sessions,
                sessions.c.user_id == a.user_id,
                {
                    "created_at": datetime(2000, 1, 1, tzinfo=UTC),
                    "expires_at": datetime(2001, 1, 1, tzinfo=UTC),
                },
            ),
            "inactive_user": (users, users.c.id == a.user_id, {"active": False}),
            "inactive_actor": (actors, actors.c.id == a.actor_id, {"active": False}),
        }[condition]
        migrator_connection.execute(table.update().where(target).values(**values))
        migrator_connection.commit()

    def resolve():
        with app_connection.begin():
            if condition != "digest_only":
                set_organization(app_connection, a.org_id)
            if condition != "org_only":
                set_credential_context(
                    app_connection,
                    token_digest(b.raw_token if condition == "org_mismatch" else a.raw_token),
                )
            if condition in {"malformed", "guessed"}:
                app_connection.execute(
                    text("SELECT set_config('fleetops.session_digest_hex', :value, true)"),
                    {"value": "invalid" if condition == "malformed" else secrets.token_hex(32)},
                )
            app_connection.execute(
                text("SELECT set_config('fleetops.actor_id', :actor, true)"),
                {"actor": str(b.actor_id)},
            )
            return app_connection.execute(CURRENT).scalar_one()

    if condition == "valid":
        assert resolve() == a.actor_id
    else:
        with pytest.raises(DBAPIError) as error:
            resolve()
        assert error.value.orig.sqlstate == "42501"


@pytest.mark.parametrize("rollback", [False, True])
def test_credential_context_ends_before_pool_reuse(rollback, database, asset_data):
    engine = create_runtime_engine(
        database.settings(asset_data[0].org_id), pool_size=1, max_overflow=0
    )
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            set_authenticated(connection, asset_data[0])
            assert connection.execute(CURRENT).scalar_one() == asset_data[0].actor_id
            pid = connection.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
            transaction.rollback() if rollback else transaction.commit()
        with engine.begin() as connection:
            assert connection.exec_driver_sql("SELECT pg_backend_pid()").scalar_one() == pid
            assert (
                connection.exec_driver_sql(
                    "SELECT NULLIF(current_setting('fleetops.session_digest_hex', true), '')"
                ).scalar_one()
                is None
            )
            set_organization(connection, asset_data[0].org_id)
            with pytest.raises(DBAPIError) as error:
                connection.execute(CURRENT)
            assert error.value.orig.sqlstate == "42501"
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "table_name", ["actors", "parties", "items", "external_references", "facilities", "locations"]
)
@pytest.mark.parametrize("spoof", [False, True])
def test_existing_creator_surfaces_require_authenticated_actor(
    table_name, spoof, asset_data, other_human, app_connection, migrator_connection
):
    a = asset_data[0]
    table = metadata.tables[f"fleetops.{table_name}"]
    row = dict(
        migrator_connection.execute(select(table).where(table.c.org_id == a.org_id).limit(1))
        .mappings()
        .one()
    )
    migrator_connection.rollback()
    for column in table.c:
        if column.computed is not None:
            row.pop(column.name)
    row.update(id=uuid7(), created_by_actor_id=other_human.actor_id if spoof else a.actor_id)
    for column in ("manufacturer_part_number", "external_value", "code"):
        if column in row:
            row[column] = str(uuid7())

    def insert():
        with app_connection.begin():
            set_authenticated(app_connection, a)
            return app_connection.execute(
                table.insert().values(row).returning(table.c.created_by_actor_id)
            ).scalar_one()

    if spoof:
        with pytest.raises(DBAPIError) as error:
            insert()
        assert error.value.orig.sqlstate == "42501"
        assert (
            migrator_connection.execute(select(table.c.id).where(table.c.id == row["id"])).all()
            == []
        )
    else:
        assert insert() == a.actor_id


@pytest.mark.parametrize("table_name", ["items", "assets"])
@pytest.mark.parametrize("supplied", [False, True])
def test_direct_descriptive_update_derives_actor_and_database_time(
    table_name, supplied, asset_data, other_human, app_connection, migrator_connection
):
    a = asset_data[0]
    table = items if table_name == "items" else assets
    target = a.item_id if table is items else a.asset_id
    with migrator_connection.begin():
        migrator_connection.execute(
            table.update()
            .where(table.c.id == target)
            .values(
                updated_by_actor_id=other_human.actor_id,
                updated_at=datetime(2000, 1, 1, tzinfo=UTC),
            )
        )
    values = {"description": "Direct SQL edit"}
    if supplied:
        values.update(
            updated_by_actor_id=other_human.actor_id, updated_at=datetime(2099, 1, 1, tzinfo=UTC)
        )
    with app_connection.begin():
        set_authenticated(app_connection, a)
        before = app_connection.exec_driver_sql("SELECT clock_timestamp()").scalar_one()
        row = (
            app_connection.execute(
                table.update().where(table.c.id == target).values(**values).returning(table)
            )
            .mappings()
            .one()
        )
        after = app_connection.exec_driver_sql("SELECT clock_timestamp()").scalar_one()
        assert row["updated_by_actor_id"] == a.actor_id
        assert before <= row["updated_at"] <= after
        if table is assets:
            assert row["version"] == 1 and row["current_state"] == "RECEIVED"


def test_transition_has_no_actor_argument_and_records_presented_credential(
    asset_data, other_human, app_connection, migrator_connection, snapshot
):
    signature = migrator_connection.exec_driver_sql(
        "SELECT proargnames FROM pg_proc WHERE oid=%s::regprocedure", (FUNCTION_SIGNATURE,)
    ).scalar_one()
    assert "p_actor_id" not in signature
    assert (
        migrator_connection.exec_driver_sql(
            "SELECT count(*) FROM pg_proc WHERE proname='transition_asset' "
            "AND pronamespace='fleetops'::regnamespace"
        ).scalar_one()
        == 1
    )
    migrator_connection.rollback()
    with app_connection.begin():
        set_authenticated(app_connection, asset_data[0])
        app_connection.execute(
            text("SELECT set_config('fleetops.actor_id', :id, true)"),
            {"id": str(other_human.actor_id)},
        )
        row = call_transition(app_connection, asset_data[0])
        assert row["actor_id"] == asset_data[0].actor_id
    assert snapshot(asset_data[0])[1][-1]["actor_id"] == asset_data[0].actor_id


@pytest.mark.parametrize("operation", ["creator", "updater", "transition", "logout"])
def test_org_only_cannot_produce_authenticated_writes(
    operation, asset_data, app_connection, snapshot
):
    a = asset_data[0]
    before = snapshot(a)
    with pytest.raises(DBAPIError) as error:
        with app_connection.begin():
            set_organization(app_connection, a.org_id)
            if operation == "creator":
                app_connection.execute(
                    actors.insert().values(
                        id=uuid7(),
                        org_id=a.org_id,
                        type="HUMAN",
                        display_name="Unproved",
                        created_by_actor_id=a.actor_id,
                    )
                )
            elif operation == "updater":
                app_connection.execute(
                    assets.update().where(assets.c.id == a.asset_id).values(description="Unproved")
                )
            elif operation == "transition":
                call_transition(app_connection, a)
            else:
                app_connection.execute(text("SELECT fleetops.revoke_current_session()"))
    assert error.value.orig.sqlstate == "42501"
    assert snapshot(a) == before


def test_logout_revokes_exactly_one_of_two_sessions(asset_client, asset_data, migrator_connection):
    a = asset_data[0]
    second = secrets.token_urlsafe(32)
    migrator_connection.execute(
        sessions.insert().values(
            id=uuid7(),
            org_id=a.org_id,
            user_id=a.user_id,
            token_digest=token_digest(second),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )
    migrator_connection.commit()
    assert asset_client.post("/auth/logout", headers=headers(a)).status_code == 204
    assert asset_client.get("/auth/me", headers=headers(a)).status_code == 401
    assert (
        asset_client.get("/auth/me", headers={"Authorization": f"Bearer {second}"}).status_code
        == 200
    )
    states = dict(
        migrator_connection.execute(
            select(sessions.c.token_digest, sessions.c.active).where(
                sessions.c.user_id == a.user_id
            )
        ).all()
    )
    assert states[token_digest(a.raw_token)] is False and states[token_digest(second)] is True


@pytest.mark.parametrize(
    "version,state", [(1, "RECEIVED"), (1, "READY"), (2, "RECEIVED"), (2, "READY")]
)
def test_owner_asset_insert_enforces_initial_pair(
    version, state, asset_data, seed_asset, migrator_connection
):
    if (version, state) == (1, "RECEIVED"):
        row = seed_asset(asset_data[0], version=version, current_state=state)
        assert row["version"] == 1 and row["current_state"] == "RECEIVED"
    else:
        with pytest.raises(DBAPIError) as error:
            seed_asset(asset_data[0], version=version, current_state=state)
        assert error.value.orig.sqlstate == "23514"
        assert error.value.orig.diag.constraint_name == "ck_assets_initial"
        migrator_connection.rollback()


def test_engines_validate_independent_execution_identities(database, asset_data):
    settings = database.settings(asset_data[0].org_id)
    for factory, role in [
        (create_runtime_engine, "fleetops_app"),
        (create_authenticator_engine, "fleetops_authenticator"),
    ]:
        engine = factory(settings)
        try:
            with engine.connect() as connection:
                assert connection.exec_driver_sql("SELECT current_user, session_user").one() == (
                    role,
                    role,
                )
        finally:
            engine.dispose()


def test_missing_or_wrong_authenticator_configuration_fails(database, asset_data, monkeypatch):
    monkeypatch.setenv(
        "FLEETOPS_APP_URL", database.url("fleetops_app").render_as_string(hide_password=False)
    )
    monkeypatch.setenv("FLEETOPS_ORG_ID", str(asset_data[0].org_id))
    monkeypatch.delenv("FLEETOPS_AUTHENTICATOR_URL", raising=False)
    with pytest.raises(KeyError, match="FLEETOPS_AUTHENTICATOR_URL"):
        Settings.from_environment()
    for role in ("fleetops_app", "fleetops_migrator"):
        with pytest.raises(ValueError):
            Settings(database.url("fleetops_app"), asset_data[0].org_id, database.url(role))


def test_authentication_function_catalog_is_narrow(migrator_connection):
    expected = {
        "issue_session": (
            True,
            ["p_user_id", "p_session_id", "p_token_digest", "p_expires_at"],
            "void",
            False,
            True,
        ),
        "current_authenticated_actor": (False, None, "uuid", True, False),
        "revoke_current_session": (True, None, "void", True, False),
        "enforce_authenticated_creator": (False, None, "trigger", False, False),
        "enforce_authenticated_updater": (False, None, "trigger", False, False),
        "enforce_asset_initial_state": (False, None, "trigger", False, False),
    }
    for name, contract in expected.items():
        row = migrator_connection.exec_driver_sql(
            "SELECT prosecdef, proargnames, prorettype::regtype::text, "
            "has_function_privilege('fleetops_app', p.oid, 'EXECUTE'), "
            "has_function_privilege('fleetops_authenticator', p.oid, 'EXECUTE'), "
            "pg_get_userbyid(proowner), proconfig, "
            "EXISTS (SELECT 1 FROM aclexplode(proacl) a WHERE a.grantee=0) "
            "FROM pg_proc p WHERE pronamespace='fleetops'::regnamespace AND proname=%s",
            (name,),
        ).one()
        assert tuple(row[:5]) == contract
        assert tuple(row[5:]) == ("fleetops_migrator", ["search_path=pg_catalog, pg_temp"], False)


@pytest.mark.parametrize("digest", [b"", b"x" * 31, b"x" * 33, "x" * 32, None])
def test_credential_helper_rejects_non_sha256_input(digest, app_connection):
    with app_connection.begin(), pytest.raises(ValueError):
        set_credential_context(app_connection, digest)


def test_credential_helper_requires_explicit_transaction(app_connection):
    with pytest.raises(RuntimeError, match="active transaction"):
        set_credential_context(app_connection, secrets.token_bytes(32))


def test_credential_binding_does_not_expose_digest_in_sql_logs_or_repr(
    database, asset_data, caplog
):
    a = asset_data[0]
    settings = database.settings(a.org_id)
    engine = create_runtime_engine(settings, echo=True)
    observer = create_runtime_engine(settings)
    digest = token_digest(a.raw_token)
    try:
        with engine.begin() as connection, observer.begin() as other:
            pid = connection.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
            set_credential_context(connection, digest)
            # A second app session can see query text, so server-side parameter
            # binding matters as well as SQLAlchemy's log redaction.
            query = other.execute(
                text("SELECT query FROM pg_stat_activity WHERE pid=:pid"), {"pid": pid}
            ).scalar_one()
            assert "set_config" in query and "$1" in query
            assert digest.hex() not in query and a.raw_token not in query
        assert digest.hex() not in caplog.text and a.raw_token not in caplog.text
        assert database.app_password not in repr(settings)
        assert database.authenticator_password not in repr(settings)
    finally:
        engine.dispose()
        observer.dispose()


def test_engine_rejects_mislabelled_url_with_actual_wrong_login(database, asset_data):
    actual_url = database.url("fleetops_app")
    settings = database.settings(asset_data[0].org_id)

    def connect_as_app():
        return psycopg.connect(
            **actual_url.translate_connect_args(username="user", database="dbname")
        )

    engine = create_authenticator_engine(settings, creator=connect_as_app)
    try:
        with pytest.raises(RuntimeError, match="fleetops_authenticator login"):
            engine.connect()
    finally:
        engine.dispose()


def test_creator_guard_applies_inside_migrator_owned_definer(
    asset_data, other_human, app_connection, migrator_connection
):
    """A temporary owner function proves current_user is never an attribution bypass."""
    migrator_connection.exec_driver_sql(
        "CREATE FUNCTION fleetops.attribution_definer_probe(uuid,uuid,uuid) RETURNS void "
        "LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS "
        "'INSERT INTO fleetops.parties(id,org_id,display_name,created_by_actor_id) "
        "VALUES ($1,$2,''Probe'',$3)'"
    )
    migrator_connection.exec_driver_sql(
        "GRANT EXECUTE ON FUNCTION fleetops.attribution_definer_probe(uuid,uuid,uuid) "
        "TO fleetops_app"
    )
    migrator_connection.commit()
    try:
        with pytest.raises(DBAPIError) as error:
            with app_connection.begin():
                set_authenticated(app_connection, asset_data[0])
                app_connection.execute(
                    text("SELECT fleetops.attribution_definer_probe(:id,:org,:actor)"),
                    {"id": uuid7(), "org": asset_data[0].org_id, "actor": other_human.actor_id},
                )
        assert error.value.orig.sqlstate == "42501"
    finally:
        app_connection.rollback()
        migrator_connection.rollback()
        migrator_connection.exec_driver_sql(
            "DROP FUNCTION fleetops.attribution_definer_probe(uuid,uuid,uuid)"
        )
        migrator_connection.commit()


@pytest.mark.parametrize("table_name", ["assets", "asset_identifiers", "asset_transitions"])
@pytest.mark.parametrize("spoof", [False, True])
def test_slice5_creator_guards_remain_durable_with_test_only_insert_grants(
    table_name,
    spoof,
    asset_data,
    asset_dml,
    other_human,
    seed_asset,
    app_connection,
    migrator_connection,
):
    """Separate attribution invariants from Slice 5's normally absent INSERT grants."""
    a = asset_data[0]
    table = metadata.tables[f"fleetops.{table_name}"]
    row = dict(
        migrator_connection.execute(select(table).where(table.c.org_id == a.org_id).limit(1))
        .mappings()
        .one()
    )
    migrator_connection.rollback()
    for column in table.c:
        if column.computed is not None:
            row.pop(column.name)
    field = "actor_id" if table_name == "asset_transitions" else "created_by_actor_id"
    row.update(id=uuid7())
    row[field] = other_human.actor_id if spoof else a.actor_id
    if table_name == "assets":
        row["asset_tag"] = str(uuid7())
    elif table_name == "asset_identifiers":
        row["value"] = str(uuid7())
    else:
        row["asset_id"] = seed_asset(a, history=False)["id"]

    def insert():
        with app_connection.begin():
            set_authenticated(app_connection, a)
            return app_connection.execute(
                table.insert().values(row).returning(table.c[field])
            ).scalar_one()

    if spoof:
        with pytest.raises(DBAPIError) as error:
            insert()
        assert error.value.orig.sqlstate == "42501"
        assert (
            migrator_connection.execute(select(table.c.id).where(table.c.id == row["id"])).all()
            == []
        )
    else:
        assert insert() == a.actor_id


@pytest.mark.parametrize(
    "version,state", [(1, "RECEIVED"), (1, "READY"), (2, "RECEIVED"), (2, "READY")]
)
def test_runtime_asset_initial_pair_with_test_only_insert_grant(
    version, state, asset_data, asset_dml, app_connection, migrator_connection
):
    a = asset_data[0]
    row = dict(
        migrator_connection.execute(select(assets).where(assets.c.id == a.asset_id))
        .mappings()
        .one()
    )
    migrator_connection.rollback()
    row.pop("item_serialized")
    row.update(id=uuid7(), asset_tag=str(uuid7()), version=version, current_state=state)

    def insert():
        with app_connection.begin():
            set_authenticated(app_connection, a)
            app_connection.execute(assets.insert().values(row))

    if (version, state) == (1, "RECEIVED"):
        insert()
    else:
        with pytest.raises(DBAPIError) as error:
            insert()
        assert error.value.orig.sqlstate == "23514"
        assert error.value.orig.diag.constraint_name == "ck_assets_initial"
