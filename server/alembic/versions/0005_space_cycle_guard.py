"""Enforce the Slice 4 no-ancestor-cycle invariant for direct multi-row runtime inserts.

Revision ID: 0005_space_cycle_guard
Revises: 0004_space

A create-only API is not a database invariant: one INSERT can introduce a whole
cycle. This frozen corrective revision adds only an invoker trigger function and
a non-deferrable constraint trigger; historical table definitions stay untouched.
"""

from alembic import op

revision = "0005_space_cycle_guard"
down_revision = "0004_space"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Check completed INSERT statements without crossing the runtime RLS boundary.

    PostgreSQL AFTER ROW triggers see every row from the statement when their
    function is VOLATILE. BEFORE timing can miss forward references in a batch.
    The visited array terminates even on legacy corruption; no row is repaired.

    With immutable existing parent edges and immediate composite FKs, concurrent
    inserts cannot close a cycle through another transaction's uncommitted rows.
    Parent-changing operations would require a fresh concurrency design and are
    not authorized here. No UPDATE privilege or trigger event is introduced.
    """
    connection = op.get_bind()
    connection.exec_driver_sql("""
        CREATE FUNCTION fleetops.enforce_location_acyclic()
        RETURNS trigger
        LANGUAGE plpgsql
        VOLATILE
        SECURITY INVOKER
        SET search_path = pg_catalog, pg_temp
        AS $guard$
        BEGIN
            IF EXISTS (
                WITH RECURSIVE ancestry AS (
                    SELECT l.id, l.parent_location_id, ARRAY[l.id] AS visited,
                           false AS is_cycle
                    FROM fleetops.locations l
                    WHERE l.id = NEW.id
                      AND l.org_id = NEW.org_id AND l.facility_id = NEW.facility_id
                    UNION ALL
                    SELECT p.id, p.parent_location_id, a.visited || p.id,
                           p.id = ANY(a.visited)
                    FROM ancestry a
                    JOIN fleetops.locations p ON p.id = a.parent_location_id
                    WHERE p.org_id = NEW.org_id AND p.facility_id = NEW.facility_id
                      AND NOT a.is_cycle
                )
                SELECT 1 FROM ancestry WHERE is_cycle
            ) THEN
                RAISE EXCEPTION USING
                    ERRCODE = '23514',
                    MESSAGE = 'Location hierarchy cycle is not permitted',
                    CONSTRAINT = 'ck_locations_acyclic',
                    SCHEMA = 'fleetops',
                    TABLE = 'locations';
            END IF;
            RETURN NULL;
        END
        $guard$
    """)
    # Trigger execution needs no runtime EXECUTE grant. The migrator creates the
    # trigger as owner; ordinary INSERT still checks ancestry with the caller's RLS.
    connection.exec_driver_sql(
        "REVOKE ALL ON FUNCTION fleetops.enforce_location_acyclic() FROM PUBLIC, fleetops_app"
    )
    connection.exec_driver_sql("""
        CREATE CONSTRAINT TRIGGER ck_locations_acyclic
        AFTER INSERT ON fleetops.locations
        NOT DEFERRABLE INITIALLY IMMEDIATE
        FOR EACH ROW WHEN (NEW.parent_location_id IS NOT NULL)
        EXECUTE FUNCTION fleetops.enforce_location_acyclic()
    """)


def downgrade() -> None:
    """Restore committed 0004 behavior by removing only the guard and its function."""
    connection = op.get_bind()
    connection.exec_driver_sql("DROP TRIGGER ck_locations_acyclic ON fleetops.locations")
    connection.exec_driver_sql("DROP FUNCTION fleetops.enforce_location_acyclic()")
