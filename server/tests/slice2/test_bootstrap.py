"""Run the actual separate bootstrap command with ephemeral environment credentials."""

import os
import secrets
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select

from fleetops.auth import PASSWORD_HASHER
from fleetops.bootstrap_user import BootstrapRefused, bootstrap_from_environment
from fleetops.db.metadata import actors, sessions, users
from fleetops.db.seed import ACTOR_ID, ORGANIZATION_ID
from fleetops.settings import Settings


@pytest.fixture
def bootstrap_environment(database, migrator_connection):
    environment = {
        "FLEETOPS_MIGRATOR_URL": database.url("fleetops_migrator").render_as_string(
            hide_password=False
        ),
        "FLEETOPS_ORG_ID": str(ORGANIZATION_ID),
        "FLEETOPS_BOOTSTRAP_USERNAME": "bootstrap",
        "FLEETOPS_BOOTSTRAP_PASSWORD": secrets.token_urlsafe(24),
    }
    try:
        assert migrator_connection.execute(select(users)).all() == []
        migrator_connection.rollback()
        yield environment
    finally:
        migrator_connection.rollback()
        migrator_connection.execute(sessions.delete().where(sessions.c.org_id == ORGANIZATION_ID))
        migrator_connection.execute(users.delete().where(users.c.org_id == ORGANIZATION_ID))
        migrator_connection.execute(
            actors.update().where(actors.c.id == ACTOR_ID).values(active=True)
        )
        migrator_connection.commit()


def run_bootstrap(environment):
    return subprocess.run(
        [sys.executable, "-m", "fleetops.bootstrap_user"],
        env=os.environ | environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_separate_command_creates_argon2_user_and_refuses_overwrite(
    bootstrap_environment,
    migrator_connection,
):
    environment = bootstrap_environment
    result = run_bootstrap(environment)
    assert result.returncode == 0, result.stderr
    row = migrator_connection.execute(select(users)).mappings().one()
    migrator_connection.rollback()
    assert row["org_id"] == ORGANIZATION_ID
    assert row["actor_id"] == ACTOR_ID
    assert row["actor_type"] == "HUMAN"
    assert row["id"].version == 7
    assert PASSWORD_HASHER.verify(row["password_hash"], environment["FLEETOPS_BOOTSTRAP_PASSWORD"])
    assert environment["FLEETOPS_BOOTSTRAP_PASSWORD"] not in row["password_hash"]
    assert environment["FLEETOPS_BOOTSTRAP_PASSWORD"] not in result.stdout + result.stderr
    assert row["password_hash"] not in result.stdout + result.stderr
    for changes in (
        {"FLEETOPS_BOOTSTRAP_PASSWORD": secrets.token_urlsafe(24)},
        {"FLEETOPS_BOOTSTRAP_USERNAME": "different-name"},
    ):
        result = run_bootstrap(environment | changes)
        assert result.returncode != 0
        assert migrator_connection.execute(select(users)).mappings().one() == row
        migrator_connection.rollback()


def test_concurrent_bootstraps_create_exactly_one_user(bootstrap_environment, migrator_connection):
    def attempt():
        try:
            return bootstrap_from_environment(bootstrap_environment)
        except BootstrapRefused:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: attempt(), range(2)))
    assert sum(value is not None for value in results) == 1
    assert len(migrator_connection.execute(select(users)).all()) == 1


def test_bootstrap_refuses_inactive_seeded_actor(bootstrap_environment, migrator_connection):
    migrator_connection.execute(actors.update().where(actors.c.id == ACTOR_ID).values(active=False))
    migrator_connection.commit()
    with pytest.raises(BootstrapRefused):
        bootstrap_from_environment(bootstrap_environment)
    assert migrator_connection.execute(select(users)).all() == []


@pytest.mark.parametrize(
    "field",
    [
        "FLEETOPS_MIGRATOR_URL",
        "FLEETOPS_ORG_ID",
        "FLEETOPS_BOOTSTRAP_USERNAME",
        "FLEETOPS_BOOTSTRAP_PASSWORD",
    ],
)
def test_missing_bootstrap_configuration_creates_nothing(
    field,
    bootstrap_environment,
    migrator_connection,
):
    environment = bootstrap_environment.copy()
    del environment[field]
    with pytest.raises(KeyError):
        bootstrap_from_environment(environment)
    assert migrator_connection.execute(select(users)).all() == []


def test_empty_password_creates_nothing(bootstrap_environment, migrator_connection):
    with pytest.raises(BootstrapRefused):
        bootstrap_from_environment(bootstrap_environment | {"FLEETOPS_BOOTSTRAP_PASSWORD": ""})
    assert migrator_connection.execute(select(users)).all() == []


def test_runtime_configuration_cannot_fall_back_to_privileged_role_or_default_tenant(
    database,
    monkeypatch,
):
    with pytest.raises(ValueError):
        Settings(
            database.url("fleetops_migrator"),
            ORGANIZATION_ID,
            database.url("fleetops_authenticator"),
        )
    monkeypatch.setenv(
        "FLEETOPS_APP_URL", database.url("fleetops_app").render_as_string(hide_password=False)
    )
    monkeypatch.delenv("FLEETOPS_ORG_ID", raising=False)
    with pytest.raises(KeyError):
        Settings.from_environment()
