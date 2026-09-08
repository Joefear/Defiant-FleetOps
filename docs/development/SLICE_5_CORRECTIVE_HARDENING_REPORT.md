# Slice 5 corrective hardening report

Retention note — 8 September 2026: This report preserves the 7 September pre-ADR-006 investigation and its BLOCKED result at the checkpoint named below. Its unresolved findings and working-tree inventory describe that historical candidate, not the current repository. ADR-006 and the subsequent corrective implementation closed Slice 5 at 3d1e1035f0824fa5b1c4ec7c563de723427e31dc. The original findings, probe results, and recommendations below remain unchanged.

Date: 7 September 2026. Recommendation: **BLOCKED — STOP before implementation**.

The unchanged candidate passes its existing suite, but independent PostgreSQL probes reproduce all three reported defects. The session privilege review also proves that the runtime role can mint a valid credential for another existing same-organization user. Reusing `resolve_session` alone cannot establish the stronger runtime-role trust boundary required by this gate.

1. **Starting checkpoint.** `git fetch -q origin` succeeded. Both `HEAD` and fetched `origin/main` are `e99397e6802ce7db68f58a7894302553b39bf8f1`. The existing Slice 5 candidate remains uncommitted.
2. **Baseline result.** `.\.venv\Scripts\python.exe -m pytest -v`: **495 passed in 175.94 seconds**. The first sandboxed attempt failed on environment permissions; the complete run with Docker access succeeded.
3. **Original Actor-spoof reproduction.** A real `fleetops_app` connection resolved Actor A's bearer token, established the returned organization, then directly invoked `transition_asset` with active same-org Actor B. The function succeeded and returned immutable history attributed to B at `result_version = 2`.
4. **Original descriptive-attribution reproduction.** Owner setup established prior updater B and timestamp `2000-01-01T00:00:00+00:00`. After resolving A's bearer token, `fleetops_app` executed an UPDATE specifying only description. Description changed; updater B and the old timestamp remained unchanged.
5. **Original initial-insert reproduction.** Ordinary migrator INSERT accepted all four combinations: `(1, RECEIVED)`, `(1, READY)`, `(2, RECEIVED)`, `(2, READY)`. No trigger was disabled and no database invariant bypass was used.
6. **Files changed in this corrective task.** This report only. No production code, migration, existing test, ADR, handoff, or frozen architecture was changed. The disposable raw-probe script and baseline log were removed after inspection.
7. **Authenticated Actor-binding design.** No design implemented after the STOP. The existing FastAPI dependency already has an internal SHA-256 digest in `AuthenticatedIdentity.token_digest`; `resolve_session(bytea)` resolves exact matching, active, unexpired sessions linked to active users and active HUMAN Actors through same-org joins. That digest is not sufficient against the runtime role's actual privileges.
8. **Why same-org spoof is now impossible.** It is **not** impossible. The original defect remains; no corrective PASS is claimed. The HTTP service chooses the Actor correctly, but direct execution can substitute it.
9. **Session/credential trust analysis.** Live `has_table_privilege`/`has_column_privilege` checks returned true for sessions SELECT, token_digest SELECT, and sessions INSERT. A raw runtime SELECT read B's digest, and `resolve_session` resolved it to B. Independently, the runtime connection generated a new raw token, inserted its digest into a session linked to existing B's user, and `resolve_identity` authenticated that chosen token as B. B's password and original token were never supplied. Hiding digest SELECT would close enumeration but leave credential minting. Passing raw tokens instead of digests would also leave credential minting. No credential values were printed or retained.
10. **`transition_asset` changes.** None. The function still validates organization membership and active Actor status, without proof that the supplied Actor caused the operation.
11. **SECURITY DEFINER result.** Existing security tests passed in the baseline; the candidate retains migrator ownership, SECURITY DEFINER, fixed `pg_catalog, pg_temp` search path, revoked PUBLIC EXECUTE, runtime EXECUTE, and explicit tenant/active-Actor checks. Authenticated Actor binding fails the independent probe. No new helper or privilege escalation was implemented.
12. **Raw descriptive UPDATE mechanism.** No corrective trigger or function added. Existing column grants allow description/tag and attribution updates; the independent description-only UPDATE proves attribution remains optional.
13. **HTTP PATCH result.** Existing `test_reads_patch_attribution_uuid_and_receiving_boundary` passed: authenticated updater, advancing server timestamp, unchanged global version/history, and permitted descriptive edits. Existing request-authority rejection tests passed. This does not prove direct SQL attribution.
14. **Initial INSERT enforcement mechanism.** None added because implementation stopped at the authentication-boundary collision. Defaults and current constraints remain insufficient to enforce initialization.
15. **Valid initial insertion.** Ordinary owner INSERT of version 1 / RECEIVED succeeded.
16. **Invalid initial insertion.** Ordinary owner INSERT of version 1 / READY, version 2 / RECEIVED, and version 2 / READY all succeeded. All three must eventually be rejected.
17. **Existing concurrency result.** Baseline passed `test_concurrent_same_version_has_exactly_one_winner`, `test_version_is_revalidated_after_waiting_for_asset_lock`, and `test_on_hold_admission_does_not_use_history_from_before_lock_acquisition`.
18. **ADR-005 gap result.** Baseline passed `test_global_version_gap_is_valid_for_admission_and_state_reconciliation` and `test_on_hold_history_lookup_ignores_global_version_gaps`.
19. **RLS/cross-tenant result.** All existing RLS and cross-tenant tests passed, including the cross-org Actor case in `test_definer_checks_tenant_and_active_actor_explicitly`. The discovered session attacks use valid same-org rows and do not require bypassing RLS.
20. **Exact new tests.** None authored after STOP. Independent non-pytest probes were run for transition spoofing, stale descriptive attribution, four insertion combinations, digest enumeration, and session minting. Existing fixtures supplied disposable infrastructure and controlled tenant/Asset setup; the asserted attack statements were separate raw SQL, not calls to authored test functions.
21. **Focused result.** No separate focused pytest run; all existing Slice 5 tests passed within the baseline. The independent probes successfully reproduced the defects and the credential-minting problem.
22. **Full-suite result.** 495 passed. No second full-suite run was necessary because application code, schema, and tests were unchanged. This is baseline evidence, not corrective acceptance.
23. **PostgreSQL version.** `16.15 (Debian 16.15-1.pgdg13+2)`, from `SHOW server_version` in the raw-probe database. Real PostgreSQL only.
24. **Migration round trip.** Baseline passed `test_alembic_upgrade_downgrade_round_trip` and `test_asset_migration_round_trip_restores_exact_0005_and_recreates_head`. Committed migrations 0001–0005 and uncommitted 0006 remain unchanged by this task.
25. **Lint/format/pip/diff.** `ruff check .`: all checks passed. `ruff format --check .`: 73 files already formatted. `pip check`: no broken requirements. `git diff --check`: passed, with existing LF-to-CRLF notices. No Git identity/config changes.
26. **Prior regression result.** All 495 existing tests passed, including serialization, identifiers, lifecycle, evidence NULL-only, fail-closed retirement, corruption, reconciliation, API scope, and time semantics. Passing tests do not cover the three reproduced gaps adequately.
27. **Scope and cleanup.** No Slice 6 work, no production Asset creation, no commit, no push. Both disposable PostgreSQL containers were cleaned up; a final container query found no resources with the test-session label.
28. **STOP / ADR candidate.** See the precise collision and proposed decision scope below. No ADR or architecture amendment was silently made.
29. **Git status.** The original eight modified tracked files and twelve untracked Slice 5 candidate files remain, plus this report. Full paths appear below. No candidate file was staged or committed by this task.
30. **Recommendation.** **BLOCKED.** Obtain an accepted authentication privilege-boundary decision before resuming corrective implementation. All three reported defects remain unresolved.

## Exact authority collision

[ADR-002, trusted organization context](../architecture/ADR-002.md) explicitly allows login to establish configured org context, query the user normally under RLS, verify the Argon2 hash, and create a session. Its implementation summary states that login uses ordinary RLS access. The current runtime role therefore owns both normal operations and credential issuance. Database RLS cannot distinguish Python's successful password verification from a same-role raw INSERT with equivalent fields.

[ADR-001, section 1.1](../architecture/ADR-001.md) requires category-2 SECURITY DEFINER operations to be explicitly justified by an accepted implementation ADR. It states: "This does not authorize arbitrary stored procedures or any additional function." The currently approved exception is the exact-digest resolver. [ADR-002's resolver contract](../architecture/ADR-002.md) also requires it to accept only a session digest and not modify tenant-owned data; silently turning it into a password verifier/session issuer would change that contract.

Consequently, revoking digest reads alone cannot satisfy this gate. Revoking ordinary session insertion without a replacement breaks the accepted login path. Adding a protected credential-issuance function needs explicit category-2 authorization. Separating a trusted authentication issuer from `fleetops_app` also needs a deliberate privilege/role decision, including reconciliation with the handoff's two-database-role convention. A caller-settable Actor GUC supplies no authentication; a second signed-identity mechanism would introduce the second identity system this gate instructs against inventing.

The smallest decision scope is to define who may issue credentials, how password verification is durably bound to that issuance, which credential columns the operational role may read, and how a verified session proves the Actor at both the transition and descriptive UPDATE boundaries. Include login, logout, revocation, user-to-Actor linkage protection, and pool safety in that review. Preserve exact-digest resolution and ordinary descriptive UPDATE wherever compatible with the accepted decision. This is an implementation ADR issue; no conflict requiring amendment of frozen D10 has been demonstrated. ADR-004/005's attribution requirements remain intact.

## Before / after raw probe outcome

No corrective code was applied, so the final candidate is behaviorally unchanged from the independently probed candidate:

| Probe | Before | Final candidate |
| --- | --- | --- |
| A authenticates, transition supplies B | History records B | Unresolved; unchanged |
| A authenticates, description-only UPDATE | Retains B and year-2000 timestamp | Unresolved; unchanged |
| INSERT 1 / RECEIVED | Accepted | Unchanged; valid |
| INSERT 1 / READY | Accepted | Unresolved; unchanged |
| INSERT 2 / RECEIVED | Accepted | Unresolved; unchanged |
| INSERT 2 / READY | Accepted | Unresolved; unchanged |
| App reads B's digest and resolves it | Resolves B | Unresolved; unchanged |
| App mints session for existing B | Chosen raw token authenticates B | Unresolved; unchanged |

## Working tree

```text
 M README.md
 M server/fleetops/api/app.py
 M server/fleetops/db/metadata.py
 M server/tests/acceptance/test_migrations.py
 M server/tests/conftest.py
 M server/tests/slice2/test_api_auth.py
 M server/tests/slice4/test_space_cycle_migration.py
 M server/tests/slice4/test_space_migration.py
?? docs/development/SLICE_5_CORRECTIVE_HARDENING_REPORT.md
?? server/alembic/versions/0006_assets.py
?? server/fleetops/api/asset_schemas.py
?? server/fleetops/api/assets.py
?? server/fleetops/domain/assets.py
?? server/fleetops/domain/identifier_types.py
?? server/fleetops/domain/lifecycle.py
?? server/tests/slice5/conftest.py
?? server/tests/slice5/test_asset_api.py
?? server/tests/slice5/test_asset_constraints.py
?? server/tests/slice5/test_asset_migration.py
?? server/tests/slice5/test_asset_tenancy.py
?? server/tests/slice5/test_transitions.py
```
