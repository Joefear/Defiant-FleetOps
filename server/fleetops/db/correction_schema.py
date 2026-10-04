"""Explicit correction metadata shared by current typed Asset history mappings."""

from sqlalchemy import CheckConstraint, Column, DateTime, Index, Integer, Text, Uuid, text


def extend_asset_history(table, root_column):
    """Retain ordinary records while making administrative pair roles unambiguous.

    A deferred database trigger checks completeness and root/generation agreement;
    these local constraints reject invalid scalar values and duplicate members.
    """
    for column in (
        Column("correction_role", Text, nullable=False, server_default=text("'NONE'")),
        Column("correction_pair_id", Uuid),
        Column("correction_generation", Integer, nullable=False, server_default=text("0")),
        Column("correction_occurred_at", DateTime(timezone=True)),
    ):
        table.append_column(column)
    table.append_constraint(
        CheckConstraint(
            "correction_role IN ('NONE', 'REVERSAL', 'CORRECTED') "
            "AND correction_generation >= 0 "
            "AND (correction_occurred_at IS NULL OR isfinite(correction_occurred_at))",
            name=f"ck_{table.name}_correction_values",
        )
    )
    for name, columns in (
        ("pair_role", ("org_id", "correction_pair_id", "correction_role")),
        (
            "root_generation_role",
            ("org_id", root_column, "correction_generation", "correction_role"),
        ),
    ):
        Index(
            f"uq_{table.name}_{name}",
            *(table.c[column] for column in columns),
            unique=True,
            postgresql_where=text("correction_role <> 'NONE'"),
        )
