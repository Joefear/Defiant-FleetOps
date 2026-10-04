"""Administrative correction pairs and effective history (ADR-013).

Revision ID: 0011_corrections
Revises: 0010_receiving
"""

import importlib.util
from pathlib import Path

from sqlalchemy import text

from alembic import op

revision = "0011_corrections"
down_revision = "0010_receiving"
branch_labels = None
depends_on = None

# Fixed schema declarations only. No caller selects a table, column, or history class.
HISTORIES = (
    (
        "transition",
        "asset_transitions",
        "corrects_transition_id",
        "current_state",
        ("from_state",),
        ("to_state",),
        ("text",),
        None,
    ),
    (
        "movement",
        "asset_movements",
        "corrects_movement_id",
        "current_location_id",
        ("from_location_id",),
        ("to_location_id",),
        ("uuid",),
        "initial_location_id",
    ),
    (
        "custody",
        "asset_custody_changes",
        "corrects_custody_change_id",
        "custodian_party_id",
        ("from_custodian_party_id",),
        ("to_custodian_party_id",),
        ("uuid",),
        "initial_custodian_party_id",
    ),
    (
        "ownership",
        "asset_ownership_changes",
        "corrects_ownership_change_id",
        "owner_party_id",
        ("from_owner_party_id",),
        ("to_owner_party_id",),
        ("uuid",),
        "initial_owner_party_id",
    ),
    (
        "assignment",
        "asset_assignment_events",
        "corrects_assignment_event_id",
        "current_assignment_id",
        ("from_assignee_type", "from_assignee_id"),
        ("to_assignee_type", "to_assignee_id"),
        ("text", "uuid"),
        None,
    ),
)


def historical(filename):
    """Read frozen predecessor definitions for exact restoration, never mutate them."""
    spec = importlib.util.spec_from_file_location(filename, Path(__file__).parent / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ordinary_functions():
    """Return frozen SQL for correction-aware upgrade and exact predecessor restoration."""
    assets = historical("0006_assets.py")
    facts = historical("0007_asset_fact_history.py")
    assignments = historical("0008_assignment_configuration.py")
    return [
        assets.TRANSITION_SQL,
        *(facts._operation_sql(*operation) for operation in facts.OPERATIONS),
        *(assignments.assignment_sql(name) for name in ("assign_asset", "unassign_asset")),
    ]


def malformed_sql(history, root):
    """Expose malformed authority without hiding it behind a latest-row query.

    Count/max detects generation gaps without a series proportional to an untrusted
    counter. Composite joins prevent a definer's owner privileges crossing tenants.
    """
    declaration = next(value for value in HISTORIES if value[1] == history)
    fact_fields = (*declaration[4], *declaration[5], "occurred_at")
    cancelled = ",".join("h." + field for field in fact_fields)
    previous = ",".join("p." + field for field in fact_fields)
    return f"""
      SELECT h.asset_id FROM fleetops.{history} h LEFT JOIN fleetops.{history} r
        ON r.org_id=h.org_id AND r.asset_id=h.asset_id AND r.id=h.{root}
      WHERE (h.correction_role='NONE' AND (h.{root} IS NOT NULL
        OR h.correction_pair_id IS NOT NULL OR h.correction_generation<>0
        OR h.correction_occurred_at IS NOT NULL))
        OR (h.correction_role<>'NONE' AND (h.{root} IS NULL OR r.id IS NULL
        OR r.correction_role<>'NONE' OR h.correction_pair_id IS NULL
        OR h.correction_generation<1 OR h.correction_occurred_at IS NULL
        OR h.reason !~ '[^[:space:]]' OR length(h.reason)>4000
        OR h.reason<>fleetops.normalize_correction_reason(h.reason)))
      UNION ALL
      SELECT affected.asset_id FROM fleetops.{history} affected JOIN (
      SELECT h.org_id,h.correction_pair_id FROM fleetops.{history} h
      WHERE h.correction_role<>'NONE' GROUP BY h.org_id,h.correction_pair_id
      HAVING count(*)<>2 OR count(DISTINCT h.asset_id)<>1
        OR count(DISTINCT h.{root})<>1 OR count(DISTINCT h.correction_generation)<>1
        OR count(*) FILTER (WHERE h.correction_role='REVERSAL')<>1
        OR count(*) FILTER (WHERE h.correction_role='CORRECTED')<>1 OR count(DISTINCT h.reason)<>1
        OR count(DISTINCT h.actor_id)<>1 OR count(DISTINCT h.correction_occurred_at)<>1
        OR max(h.result_version) FILTER (WHERE h.correction_role='CORRECTED')
           <>max(h.result_version) FILTER (WHERE h.correction_role='REVERSAL')+1
      ) bad ON bad.org_id=affected.org_id AND bad.correction_pair_id=affected.correction_pair_id
      UNION ALL
      SELECT h.asset_id FROM fleetops.{history} h WHERE h.correction_role='CORRECTED'
      GROUP BY h.org_id,h.asset_id,h.{root} HAVING count(*)<>max(h.correction_generation) OR
        count(DISTINCT h.correction_generation)<>max(h.correction_generation)
      UNION ALL
      SELECT h.asset_id FROM fleetops.{history} h JOIN fleetops.{history} p
        ON p.org_id=h.org_id AND p.asset_id=h.asset_id
        AND ((h.correction_generation=1 AND p.id=h.{root}) OR
          (h.correction_generation>1 AND p.{root}=h.{root}
          AND p.correction_generation=h.correction_generation-1 AND p.correction_role='CORRECTED'))
      WHERE h.correction_role='REVERSAL' AND (h.result_version<>p.result_version+1
        OR ROW({cancelled}) IS DISTINCT FROM ROW({previous}))
    """ + source_coherence_sql(history, root)


def source_coherence_sql(history, root):
    """Validate each replacement against pre-root effective authority, including old generations."""
    _kind, _, _, _, sources, targets, _types, baseline = next(
        declaration for declaration in HISTORIES if declaration[1] == history
    )
    actual = ",".join(f"c.{field}" for field in sources)
    if baseline:
        expected = f"CASE WHEN p.id IS NULL THEN b.{baseline} ELSE p.{targets[0]} END"
    else:
        expected = ",".join(f"p.{field}" for field in targets)
    return f"""
      UNION ALL
      SELECT c.asset_id FROM fleetops.{history} c
      JOIN fleetops.{history} r ON r.org_id=c.org_id AND r.asset_id=c.asset_id AND r.id=c.{root}
      LEFT JOIN fleetops.asset_initial_facts b ON b.org_id=c.org_id AND b.asset_id=c.asset_id
      LEFT JOIN LATERAL (
        SELECT h.* FROM fleetops.effective_{history} h
        WHERE h.org_id=c.org_id AND h.asset_id=c.asset_id AND h.result_version<r.result_version
        ORDER BY h.result_version DESC LIMIT 1
      ) p ON true
      WHERE c.correction_role='CORRECTED' AND ROW({actual}) IS DISTINCT FROM ROW({expected})
    """


def signature(kind, types):
    return (
        f"fleetops.correct_{kind}(uuid,uuid,integer,{','.join(types)},"
        "text,timestamptz,timestamptz,uuid,uuid,uuid)"
    )


def correction_sql(kind, history, root, projection, sources, targets, types, baseline):
    """Five typed Category 1 boundaries pair immutable history with one projection.

    Python owns lifecycle legality. The database owns the common lock, credential,
    global head, complete pair, and source/projection integrity. REVERSAL copies the
    cancelled assertion; it never calls a physical inverse operation.
    """
    params = ",".join(f"p_{field} {typ}" for field, typ in zip(targets, types, strict=True))
    declarations = ";".join(f"prior_{i} {typ}" for i, typ in enumerate(types)) + ";"
    prior = ";".join(f"prior_{i}:=previous.{field}" for i, field in enumerate(targets)) + ";"
    if baseline:
        fallback = f"""SELECT b.{baseline} INTO prior_0 FROM fleetops.asset_initial_facts b
          WHERE b.org_id=trusted_org AND b.asset_id=p_asset_id;
          IF NOT FOUND THEN RAISE EXCEPTION USING
            ERRCODE='P0001',MESSAGE='Missing physical witness'; END IF;"""
    elif kind == "assignment":
        fallback = """PERFORM 1 FROM fleetops.asset_initial_assignment_facts b
          WHERE b.org_id=trusted_org AND b.asset_id=p_asset_id;
          IF NOT FOUND THEN RAISE EXCEPTION USING
            ERRCODE='P0001',MESSAGE='Missing assignment witness'; END IF;"""
    else:
        fallback = "RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Creation is not correctable';"
    current = (
        "CASE WHEN head.to_assignee_id IS NULL THEN NULL ELSE head.id END"
        if kind == "assignment"
        else f"head.{targets[0]}"
    )
    final = (
        "CASE WHEN produced.to_assignee_id IS NULL THEN NULL ELSE produced.id END"
        if kind == "assignment"
        else f"produced.{targets[0]}"
    )
    check = ""
    if kind in ("movement", "custody", "ownership"):
        target_table = "locations" if kind == "movement" else "parties"
        check = f"""IF p_{targets[0]} IS NOT NULL AND NOT EXISTS (SELECT 1 FROM
          fleetops.{target_table} t WHERE t.org_id=trusted_org AND t.id=p_{targets[0]}) THEN
          RAISE EXCEPTION USING ERRCODE='23503',MESSAGE='Invalid physical target'; END IF;"""
    if kind == "ownership":
        check += (
            "IF p_to_owner_party_id IS NULL THEN RAISE EXCEPTION USING "
            "ERRCODE='23502',MESSAGE='Owner required'; END IF;"
        )
    if kind == "transition":
        check = (
            "IF p_to_state='RETIRED' THEN RAISE EXCEPTION USING "
            "ERRCODE='23514',MESSAGE='Retirement requires evidence'; END IF;"
        )
    if kind == "assignment":
        check = """
          IF (p_to_assignee_type IS NULL)<>(p_to_assignee_id IS NULL)
            OR (p_to_assignee_id IS NULL AND prior_1 IS NULL) THEN
            RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Invalid assignment correction'; END IF;
          FOR endpoint IN SELECT prior_0 AS kind,prior_1 AS id
            UNION ALL SELECT p_to_assignee_type,p_to_assignee_id LOOP
            IF endpoint.id IS NOT NULL AND NOT (
              (endpoint.kind='ACTOR' AND EXISTS (SELECT 1 FROM fleetops.actors t
                WHERE t.org_id=trusted_org AND t.id=endpoint.id)) OR
              (endpoint.kind='LOCATION' AND EXISTS (SELECT 1 FROM fleetops.locations t
                WHERE t.org_id=trusted_org AND t.id=endpoint.id)) OR
              (endpoint.kind='PARTY' AND EXISTS (SELECT 1 FROM fleetops.parties t
                WHERE t.org_id=trusted_org AND t.id=endpoint.id))) THEN
              RAISE EXCEPTION USING ERRCODE='23503',MESSAGE='Invalid assignment target'; END IF;
          END LOOP;
        """
    fields = ",".join((*sources, *targets))
    cancelled = ",".join(f"head.{field}" for field in (*sources, *targets))
    replacement = ",".join(
        [*(f"prior_{i}" for i in range(len(sources))), *(f"p_{field}" for field in targets)]
    )
    return f"""
      CREATE FUNCTION fleetops.correct_{kind}(p_asset_id uuid,p_root_id uuid,
        p_expected_version integer,{params},p_reason text,p_correction_occurred_at timestamptz,
        p_occurred_at timestamptz,p_pair_id uuid,p_reversal_id uuid,p_corrected_id uuid)
      RETURNS fleetops.{history} LANGUAGE plpgsql VOLATILE SECURITY DEFINER
      SET search_path=pg_catalog,pg_temp AS $correct$
      DECLARE
        trusted_org uuid; performer uuid; generation integer; normalized_reason text;
        locked_asset fleetops.assets%ROWTYPE; original fleetops.{history}%ROWTYPE;
        head fleetops.{history}%ROWTYPE; previous fleetops.{history}%ROWTYPE;
        produced fleetops.{history}%ROWTYPE; endpoint record; {declarations}
      BEGIN
        trusted_org:=NULLIF(current_setting('fleetops.org_id',true),'')::uuid;
        IF trusted_org IS NULL OR NULLIF(current_setting('fleetops.actor_id',true),'') IS NOT NULL
          THEN
          RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='Trusted credential context required'; END
            IF;
        performer:=fleetops.current_authenticated_actor();
        SELECT a.* INTO locked_asset FROM fleetops.assets a
          WHERE a.org_id=trusted_org AND a.id=p_asset_id FOR UPDATE;
        IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='P0002',MESSAGE='Asset not found'; END IF;
        IF p_expected_version IS NULL OR p_expected_version<>locked_asset.version THEN
          RAISE EXCEPTION USING ERRCODE='40001',MESSAGE='Stale Asset version'; END IF;
        PERFORM fleetops.assert_asset_correction_authority(p_asset_id);
        SELECT r.* INTO original FROM fleetops.{history} r
          WHERE r.org_id=trusted_org AND r.asset_id=p_asset_id AND r.id=p_root_id;
        IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='P0002',MESSAGE='History not found'; END IF;
        IF original.correction_role<>'NONE' THEN
          RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Ordinary correction root required'; END IF;
        SELECT h.* INTO head FROM fleetops.effective_{history} h
          WHERE h.org_id=trusted_org AND h.asset_id=p_asset_id
          AND (h.id=p_root_id OR h.{root}=p_root_id);
        IF NOT FOUND OR head.result_version<>locked_asset.version THEN
          RAISE EXCEPTION USING ERRCODE='40001',MESSAGE='Root is not effective global head'; END IF;
        IF locked_asset.{projection} IS DISTINCT FROM ({current}) THEN
          RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='Projection disagrees with history'; END IF;
        SELECT h.* INTO previous FROM fleetops.effective_{history} h
          WHERE h.org_id=trusted_org AND h.asset_id=p_asset_id
          AND h.result_version<original.result_version ORDER BY h.result_version DESC LIMIT 1;
        IF FOUND THEN {prior} ELSE {fallback} END IF;
        {check}
        normalized_reason:=fleetops.normalize_correction_reason(p_reason);
        IF normalized_reason IS NULL OR length(normalized_reason) NOT BETWEEN 1 AND 4000
          OR p_correction_occurred_at IS NULL OR NOT isfinite(p_correction_occurred_at)
          OR p_pair_id IS NULL OR p_reversal_id IS NULL OR p_corrected_id IS NULL THEN
          RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Correction metadata required'; END IF;
        generation:=head.correction_generation+1;
        -- Reserve capacity for both D6 entries before writing either. Integer
        -- overflow must reject atomically, never wrap or reuse a global version.
        PERFORM locked_asset.version+2;
        INSERT INTO fleetops.{history}
          (id,org_id,asset_id,result_version,{fields},reason,actor_id,occurred_at,{root},
           correction_role,correction_pair_id,correction_generation,correction_occurred_at)
        VALUES (p_reversal_id,trusted_org,p_asset_id,locked_asset.version+1,{cancelled},
          normalized_reason,performer,head.occurred_at,p_root_id,'REVERSAL',p_pair_id,
          generation,p_correction_occurred_at);
        INSERT INTO fleetops.{history}
          (id,org_id,asset_id,result_version,{fields},reason,actor_id,occurred_at,{root},
           correction_role,correction_pair_id,correction_generation,correction_occurred_at)
        VALUES (p_corrected_id,trusted_org,p_asset_id,locked_asset.version+2,{replacement},
          normalized_reason,performer,COALESCE(p_occurred_at,head.occurred_at),p_root_id,'CORRECTED',
          p_pair_id,generation,p_correction_occurred_at) RETURNING * INTO produced;
        UPDATE fleetops.assets SET {projection}={final},version=locked_asset.version+2,
          updated_by_actor_id=performer,updated_at=statement_timestamp()
          WHERE org_id=trusted_org AND id=p_asset_id;
        RETURN produced;
      END $correct$;
    """


def upgrade():
    """Append correction structure without rewriting original domain facts."""
    connection = op.get_bind()
    # Match the request boundary's Unicode whitespace contract independently of DB locale.
    whitespace = (
        *range(9, 14),
        *range(28, 33),
        133,
        160,
        5760,
        *range(8192, 8203),
        8232,
        8233,
        8239,
        8287,
        12288,
    )
    trim_characters = " || ".join(f"chr({code})" for code in whitespace)
    run_sql(
        connection,
        f"""
      CREATE FUNCTION fleetops.normalize_correction_reason(p_value text) RETURNS text
      LANGUAGE sql IMMUTABLE STRICT SECURITY INVOKER SET search_path=pg_catalog,pg_temp
      AS $normalize$ SELECT pg_catalog.btrim(p_value, {trim_characters}) $normalize$;
      REVOKE ALL ON FUNCTION fleetops.normalize_correction_reason(text)
        FROM PUBLIC,fleetops_app,fleetops_authenticator;
      GRANT EXECUTE ON FUNCTION fleetops.normalize_correction_reason(text) TO fleetops_app;
    """,
    )
    for _, history, root, *_ in HISTORIES:
        run_sql(
            connection,
            f"""
          ALTER TABLE fleetops.{history}
            ADD COLUMN correction_role text NOT NULL DEFAULT 'NONE',
            ADD COLUMN correction_pair_id uuid,
            ADD COLUMN correction_generation integer NOT NULL DEFAULT 0,
            ADD COLUMN correction_occurred_at timestamptz,
            ADD CONSTRAINT ck_{history}_correction_values CHECK (
              correction_role IN ('NONE','REVERSAL','CORRECTED') AND correction_generation>=0
              AND (correction_occurred_at IS NULL OR isfinite(correction_occurred_at)));
          CREATE UNIQUE INDEX uq_{history}_pair_role ON fleetops.{history}
            (org_id,correction_pair_id,correction_role) WHERE correction_role<>'NONE';
          CREATE UNIQUE INDEX uq_{history}_root_generation_role ON fleetops.{history}
            (org_id,{root},correction_generation,correction_role) WHERE correction_role<>'NONE';
          CREATE VIEW fleetops.effective_{history} WITH (security_invoker=true) AS
            SELECT h.* FROM fleetops.{history} h WHERE h.correction_role<>'REVERSAL'
            AND NOT EXISTS (SELECT 1 FROM fleetops.{history} c
              WHERE c.org_id=h.org_id AND c.asset_id=h.asset_id AND c.{root}=COALESCE(h.{root},h.id)
              AND c.correction_role='CORRECTED' AND
                c.correction_generation>h.correction_generation);
          GRANT SELECT ON fleetops.effective_{history} TO fleetops_app;
          CREATE FUNCTION fleetops.check_{history}_pairs() RETURNS trigger
          LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,pg_temp AS $pair$
          BEGIN
            IF EXISTS (SELECT 1 FROM ({malformed_sql(history, root)}) bad
                       WHERE bad.asset_id=NEW.asset_id) THEN
              RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Malformed correction history'; END IF;
            RETURN NULL;
          END $pair$;
          REVOKE ALL ON FUNCTION fleetops.check_{history}_pairs()
            FROM PUBLIC,fleetops_app,fleetops_authenticator;
          CREATE CONSTRAINT TRIGGER correction_pair_complete AFTER INSERT ON fleetops.{history}
            DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
            EXECUTE FUNCTION fleetops.check_{history}_pairs();
        """,
        )
    projection_checks = []
    for kind, history, _root, projection, _sources, targets, _types, baseline in HISTORIES:
        if kind == "transition":
            expected = """(SELECT to_state FROM fleetops.effective_asset_transitions h WHERE
              h.org_id=a.org_id AND h.asset_id=a.id ORDER BY result_version DESC LIMIT 1)"""
        elif kind == "assignment":
            expected = """(SELECT CASE WHEN to_assignee_id IS NULL THEN NULL ELSE id END FROM
              fleetops.effective_asset_assignment_events h WHERE h.org_id=a.org_id AND
                h.asset_id=a.id ORDER BY result_version DESC LIMIT 1)"""
        else:
            expected = f"""(SELECT CASE WHEN h.id IS NULL THEN b.{baseline} ELSE h.{targets[0]}
              END FROM fleetops.asset_initial_facts b LEFT JOIN LATERAL (SELECT id,{targets[0]}
                FROM fleetops.effective_{history} h WHERE h.org_id=a.org_id AND h.asset_id=a.id
                  ORDER BY result_version DESC LIMIT 1) h ON true WHERE b.org_id=a.org_id AND
                    b.asset_id=a.id)"""
        projection_checks.append(f"a.{projection} IS DISTINCT FROM {expected}")
    projection_invalid = " OR ".join(projection_checks)
    malformed = " UNION ALL ".join(malformed_sql(h, r) for _, h, r, *_ in HISTORIES)
    raw = " UNION ALL ".join(
        f"SELECT org_id,asset_id,result_version FROM fleetops.{h}" for _, h, *_ in HISTORIES
    )
    run_sql(
        connection,
        f"""
      CREATE VIEW fleetops.asset_correction_anomalies WITH (security_invoker=true) AS {malformed};
      GRANT SELECT ON fleetops.asset_correction_anomalies TO fleetops_app;
      CREATE FUNCTION fleetops.assert_asset_correction_authority(p_asset_id uuid) RETURNS void
      LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,pg_temp AS $authority$
      DECLARE a fleetops.assets%ROWTYPE;
      BEGIN
        SELECT * INTO a FROM fleetops.assets WHERE id=p_asset_id
          AND org_id=NULLIF(current_setting('fleetops.org_id',true),'')::uuid;
        IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='P0002',MESSAGE='Asset not found'; END IF;
        IF ({projection_invalid})
          OR EXISTS (SELECT 1 FROM fleetops.asset_correction_anomalies WHERE asset_id=a.id)
          OR NOT EXISTS (SELECT 1 FROM fleetops.asset_initial_facts b
            WHERE b.org_id=a.org_id AND b.asset_id=a.id)
          OR NOT EXISTS (SELECT 1 FROM fleetops.asset_initial_assignment_facts b
            WHERE b.org_id=a.org_id AND b.asset_id=a.id)
          OR (SELECT count(*)<>a.version OR count(DISTINCT result_version)<>a.version
              OR min(result_version)<>1 OR max(result_version)<>a.version
              FROM ({raw}) e WHERE e.org_id=a.org_id AND e.asset_id=a.id) THEN
          RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='Asset authority incomplete'; END IF;
      END $authority$;
      REVOKE ALL ON FUNCTION fleetops.assert_asset_correction_authority(uuid)
        FROM PUBLIC,fleetops_app,fleetops_authenticator;
      GRANT EXECUTE ON FUNCTION fleetops.assert_asset_correction_authority(uuid) TO fleetops_app;
    """,
    )
    records = historical("../correction_records_0011.py")
    records.upgrade(
        connection, historical("../correction_schema_0011.py"), historical("0010_receiving.py")
    )
    historical("../correction_evaluation_0011.py").upgrade(connection)
    historical("../correction_health_0011.py").upgrade(connection)
    run_sql(connection, records.receiving_sql(historical("0010_receiving.py")))
    for declaration in HISTORIES:
        run_sql(connection, correction_sql(*declaration))
        call = signature(declaration[0], declaration[6])
        run_sql(
            connection,
            f"REVOKE ALL ON FUNCTION {call} FROM PUBLIC,fleetops_app,fleetops_authenticator",
        )
        run_sql(connection, f"GRANT EXECUTE ON FUNCTION {call} TO fleetops_app")
    # D54/D55: adapt frozen predecessor SQL without editing old migrations;
    # downgrade must restore the exact originals. Ordinary producers must read
    # effective facts and reject malformed correction authority while holding
    # their Asset lock. Health reports corruption; admission must not treat it
    # as physical truth or silently repair it.
    for sql in ordinary_functions():
        sql = sql.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)
        for _, history, *_ in HISTORIES:
            sql = sql.replace(f"FROM fleetops.{history} ", f"FROM fleetops.effective_{history} ")
        for alias in ("h", "t"):
            sql = sql.replace(
                f"    SELECT {alias}.* INTO latest",
                "    IF EXISTS (SELECT 1 FROM fleetops.asset_correction_anomalies "
                "WHERE asset_id=p_asset_id) THEN RAISE EXCEPTION USING "
                "ERRCODE='P0001',MESSAGE='Malformed correction authority'; END IF;\n"
                f"    SELECT {alias}.* INTO latest",
            )
        run_sql(connection, sql)


def downgrade():
    """Refuse to discard accepted corrections; restore exact ordinary predecessor SQL."""
    connection = op.get_bind()
    for _, history, *_ in HISTORIES:
        if run_sql(
            connection,
            f"SELECT EXISTS (SELECT 1 FROM fleetops.{history} WHERE correction_role<>'NONE')",
        ).scalar_one():
            raise RuntimeError("Cannot downgrade accepted correction history")
    historical("../correction_records_0011.py").downgrade(
        connection, historical("0010_receiving.py")
    )
    for sql in ordinary_functions():
        run_sql(connection, sql.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1))
    for declaration in reversed(HISTORIES):
        run_sql(connection, f"DROP FUNCTION {signature(declaration[0], declaration[6])}")
    run_sql(connection, "DROP FUNCTION fleetops.assert_asset_correction_authority(uuid)")
    run_sql(connection, "DROP VIEW fleetops.asset_correction_anomalies")
    for _, history, *_ in reversed(HISTORIES):
        run_sql(
            connection,
            f"""
          DROP VIEW fleetops.effective_{history};
          DROP TRIGGER correction_pair_complete ON fleetops.{history};
          DROP FUNCTION fleetops.check_{history}_pairs();
          ALTER TABLE fleetops.{history} DROP COLUMN correction_role,
            DROP COLUMN correction_pair_id,DROP COLUMN correction_generation,DROP COLUMN
              correction_occurred_at;
        """,
        )

    run_sql(connection, "DROP FUNCTION fleetops.normalize_correction_reason(text)")


def run_sql(connection, statement):
    """Compile SQL literals so PL/pgSQL percent tokens are not driver placeholders."""
    return connection.execute(text(statement))
