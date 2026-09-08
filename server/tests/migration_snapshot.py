"""Exact migration-owned data and catalog snapshots for real PostgreSQL round trips."""

from sqlalchemy import select

from fleetops.db.metadata import metadata


def schema_snapshot(connection):
    "Capture definitions instead of OIDs so equivalent recreated objects compare exactly."
    connection.exec_driver_sql("SET LOCAL search_path = pg_catalog")
    result = {}
    names = (
        connection.exec_driver_sql(
            "SELECT tablename FROM pg_tables WHERE schemaname='fleetops' ORDER BY tablename"
        )
        .scalars()
        .all()
    )
    for name in names:
        qualified = f"fleetops.{name}"
        result[name] = {
            "rows": (
                connection.execute(
                    select(metadata.tables[qualified]).order_by(metadata.tables[qualified].c.id)
                ).all()
                if name != "alembic_version"
                else connection.exec_driver_sql("SELECT * FROM fleetops.alembic_version").all()
            ),
            "table": connection.exec_driver_sql(
                (
                    "SELECT pg_get_userbyid(relowner), relrowsecurity, relacl::text FROM "
                    "pg_class WHERE oid=%s::regclass"
                ),
                (qualified,),
            ).one(),
            "columns": connection.exec_driver_sql(
                (
                    "SELECT attname, attnotnull, attacl::text, attgenerated, "
                    "atttypid::regtype::text FROM pg_attribute WHERE attrelid=%s::regclass "
                    "AND attnum>0 AND NOT attisdropped ORDER BY attnum"
                ),
                (qualified,),
            ).all(),
            "defaults": connection.exec_driver_sql(
                (
                    "SELECT a.attname, pg_get_expr(d.adbin, d.adrelid) FROM pg_attrdef d "
                    "JOIN pg_attribute a ON a.attrelid=d.adrelid AND a.attnum=d.adnum "
                    "WHERE d.adrelid=%s::regclass ORDER BY a.attname"
                ),
                (qualified,),
            ).all(),
            "constraints": connection.exec_driver_sql(
                (
                    "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint WHERE "
                    "conrelid=%s::regclass ORDER BY conname"
                ),
                (qualified,),
            ).all(),
            "indexes": connection.exec_driver_sql(
                (
                    "SELECT indexname, indexdef FROM pg_indexes WHERE "
                    "schemaname='fleetops' AND tablename=%s ORDER BY indexname"
                ),
                (name,),
            ).all(),
            "policies": connection.exec_driver_sql(
                (
                    "SELECT policyname, permissive, roles, cmd, qual, with_check FROM "
                    "pg_policies WHERE schemaname='fleetops' AND tablename=%s ORDER BY "
                    "policyname"
                ),
                (name,),
            ).all(),
        }
    result["functions"] = connection.exec_driver_sql(
        "SELECT pg_get_functiondef(p.oid), pg_get_userbyid(proowner), "
        "proacl::text FROM pg_proc p JOIN pg_namespace n ON "
        "n.oid=p.pronamespace WHERE n.nspname='fleetops' ORDER BY proname"
    ).all()
    result["triggers"] = connection.exec_driver_sql(
        "SELECT pg_get_triggerdef(t.oid) FROM pg_trigger t JOIN pg_class c ON "
        "c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE "
        "n.nspname='fleetops' AND NOT t.tgisinternal ORDER BY "
        "c.relname,t.tgname"
    ).all()
    connection.rollback()
    return result
