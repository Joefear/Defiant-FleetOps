"""Empty baseline: no FleetOps domain tables or functions.

Revision ID: 0001_empty_baseline
Revises: None
Role provisioning is bootstrap_001_roles.sql; it needs administrative credentials
that the DDL owner deliberately does not hold. Cluster roles survive downgrade.
"""

revision = "0001_empty_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
