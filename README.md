# Defiant FleetOps

Slice 6 adds immutable initial physical facts, movement, custody and ownership
history to Assets and controlled lifecycle transitions. All four history classes
share one Asset version and row lock. It honours D1, D3, D7, D8, D10, D11, D12,
D18, ADR-001 through ADR-007, and Build Handoff v1.3.

The governing source is
[Architecture & Boundary v0.1](docs/architecture/FleetOps_Architecture_Boundary_v0.1.docx).
The [Markdown transcription](docs/architecture/FleetOps_Architecture_Boundary_v0.1.md)
is subordinate to that DOCX. Implementation follows the
[v1.3 build handoff](docs/architecture/FleetOps_v0.1_Build_Handoff_v1.3.docx).
Disagreement requires STOP and an architecture amendment request, not a code workaround.

## Run checks

Requirements: Python 3.13+ and a running Docker engine that supports Linux containers.
From this repository in PowerShell:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -c requirements-dev.lock -e '.[test,dev]'
.\.venv\Scripts\python.exe -m pytest -v
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
```

Tests create a fresh PostgreSQL 16.15 cluster from a digest-pinned official image,
with a fresh database per session, random passwords, SCRAM host authentication,
a random localhost-only port, and ephemeral storage. Docker must be permitted in
the execution environment. The first run may download the image. Tests stop and
remove their own container after the session, including on failure. They fail
with STOP when real PostgreSQL 16 cannot be provided; they never skip or use SQLite.
The suite runs actual Alembic CLI upgrade, downgrade and metadata checks and
connects separately as each database role. Test objects are removed and their
absence is checked before the container is stopped.

## Database foundation

The administrative bootstrap runs `server/fleetops/db/bootstrap_001_roles.sql`,
then `bootstrap_002_authenticator.sql`, in one transaction on a new, empty,
dedicated database/cluster. The first artifact creates the migrator/app roles and
`fleetops` schema; the forward second artifact creates the independent login-only
`fleetops_authenticator` role. Fresh bootstrap refuses preexisting FleetOps roles
or user objects. Role creation stays outside Alembic.

`fleetops_migrator` owns the schema and all subsequent Alembic DDL.
`fleetops_app` and `fleetops_authenticator` receive only explicit database CONNECT
and schema USAGE initially. All three passwords must be distinct and nonempty.
None of these roles is superuser, inherits other roles, creates roles/databases, replicates,
or bypasses row security. PUBLIC loses database access/TEMP and public-schema
access. Migrator-created objects grant PUBLIC and the app role nothing by default,
including removal of PostgreSQL's default PUBLIC function EXECUTE.

For a separately provisioned empty PostgreSQL 16 database, load the variables
shown in `.env.example` into your shell (the file is not loaded automatically):

```powershell
.\.venv\Scripts\python.exe -m fleetops.db.bootstrap
.\.venv\Scripts\python.exe -m alembic upgrade head
```

For an existing installation that already applied bootstrap 001, provision an
independent authenticator password and run the forward admin step before upgrading:

```powershell
.\.venv\Scripts\python.exe -m fleetops.db.bootstrap --authenticator-only
.\.venv\Scripts\python.exe -m alembic upgrade head
```

This command requires the explicit admin bootstrap URL and authenticator password;
it leaves the existing roles and credentials intact. Alembic owns subsequent object
grants. Downgrading to 0005 restores historical object privileges while retaining
the externally bootstrapped authenticator role.

Alembic connects only as an authenticated `fleetops_migrator` on PostgreSQL 16.
Migration connections use a catalog-only search path; migration DDL must name
the `fleetops` schema explicitly. Revision `0001_empty_baseline` has no-op
upgrade/downgrade functions. At that revision,
`fleetops.alembic_version` is the only table and is migrator-owned. At base, that
bookkeeping table is empty. Head is `0007_asset_fact_history` and includes the
Slice 2 through Slice 6 tables described below.
Downgrade deliberately retains the administrative
roles, locked schema and deny defaults; it does not drop cluster-wide roles or
restore PUBLIC privileges. The disposable test cluster is removed separately.

Later migrations call `grant_immutable_history(connection, table)` from
`fleetops.db.grants` in their DDL transaction. It removes existing table and column
grants to PUBLIC/the app and grants only SELECT and INSERT to `fleetops_app`.
It grants no sequence access or projection updates. Future tenant-owned records
must carry the handoff's required organization scope; Slice 1 creates none.
Future privileged functions must have a safe fixed search path, qualified object
references and explicit EXECUTE grants. Slice 2 adds the accepted authentication
resolver described below.
See PostgreSQL 16 documentation for
[default privileges](https://www.postgresql.org/docs/16/sql-alterdefaultprivileges.html)
and [SECURITY DEFINER functions](https://www.postgresql.org/docs/16/sql-createfunction.html).

## Slice 2 identity and authentication

Revision `0002_identity_auth` adds only `organizations`, `actors`, `parties`,
`party_roles`, `users`, and `sessions`. Runtime metadata and the migration's
frozen schema snapshot are checked with Alembic. Downgrade to
`0001_empty_baseline` removes these tables and `resolve_session(bytea)`, retaining
the Slice 1 roles, schema, bookkeeping table, and default-deny posture.

Every entity uses a Python-generated UUIDv7. All six tables have non-null organization
scope and RLS. The organization root has a generated `org_id = id`: its identity is
its own scope, so it uses the same fail-closed policy as its children. Cross-tenant
references use composite `(org_id, target_id)` foreign keys. Users also have a typed
foreign key requiring a HUMAN actor; changing that actor into a DEVICE is rejected.

A party's roles form a unique set of membership rows, allowing vendor and manufacturer
roles on one party without separate vendor/customer tables. Actor and party creation
records the performer from authenticated context. SYSTEM, DEVICE, and INTEGRATION
actors exist independently of users; this slice adds no machine-login or integration
workflow. Creation times and session expiry are server-side UTC timestamps.

### Database authority

At corrected Slice 5 head, `fleetops_app` has no direct table or column privileges
on `users` or `sessions`. It cannot enumerate credentials, create users or sessions,
or directly revoke them. It retains organization SELECT and Actor/Party/role
SELECT/INSERT. The historical 0002 grants are corrected forward by 0006.

`fleetops_authenticator` reads only the user/Actor columns needed for password and
active HUMAN checks, under its own restrictive tenant RLS policies. It cannot
mutate domain rows, read sessions, create users, assume another role, or execute
Asset transitions. Only this role may call the migrator-owned `issue_session`:
the function validates the configured org, active HUMAN user, 32-byte digest and
future expiry capped at 24 hours, and inserts one session with database creation
time. Argon2 verification and random token generation remain in Python.

The migration helper replaces table and live-column ACLs, enables RLS, and installs
matching USING/WITH CHECK predicates. A restrictive tenant policy ensures an additional
permissive policy cannot bypass the boundary. Missing or cleared context sees no rows;
there is no default tenant. The setting is always transaction-local, including login
and bootstrap, and disappears on commit/rollback before a connection is reused.

Some adversarial tests temporarily grant UPDATE/DELETE inside the disposable database
to prove RLS independently of the narrower production grants. They assert hidden-row
rowcount zero and visible wrong-tenant rejection, then restore the production grants.
They do not change runtime privileges to make an endpoint work.

`resolve_session(bytea)` is the Slice 2 authentication SECURITY DEFINER function. It is owned
by the migrator, uses qualified tables and `search_path = pg_catalog, pg_temp`, and
grants EXECUTE only to the runtime role (besides the owner's inherent access). PUBLIC
has no EXECUTE. An exact valid SHA-256 digest returns only `org_id, user_id, actor_id`.
Unknown/expired/inactive credentials, users, and actors return no row. The resolver
writes no data and sets no tenant context; the trusted request dependency sets the
returned org inside the transaction before ordinary RLS access. ADR-006 also sets
the presented digest in a transaction-local setting using bound parameters and
redacted SQL logging. `current_authenticated_actor()` re-resolves that credential
and requires matching org scope. An Actor UUID setting conveys no authority.

Reusable invoker triggers reject creator mismatches on runtime INSERTs and derive
Item/Asset updater Actor and database time even for direct descriptive SQL. They
test the original `session_user`, including inside definer functions, so only
independent admin setup bypasses runtime attribution. `revoke_current_session()`
revokes exactly the presented credential and takes no session/user selector.

### Create the initial user and run the API

The migration seeds one Defiant organization, one INTERNAL party (and its role
membership), and one bootstrap HUMAN actor. It creates no password-bearing user.
Set the administrative and runtime variables described in `.env.example`; that
file is documentation and is not loaded automatically. The organization identifier
below is an explicit trusted configuration value, not a fallback used by RLS.

For a fresh installation, run the administrative role bootstrap and Alembic upgrade
as described above. Then set `FLEETOPS_MIGRATOR_URL` and invoke the separate initial-user
command. Enter the password interactively; do not put a real password in a script,
migration, fixture, README, or example environment file.

```powershell
$env:FLEETOPS_ORG_ID = '01a0744a-17c4-7497-a69b-9da2aa8403da'
$env:FLEETOPS_BOOTSTRAP_USERNAME = Read-Host 'Initial username'
$env:FLEETOPS_BOOTSTRAP_PASSWORD = [System.Net.NetworkCredential]::new(
    '', (Read-Host 'Initial password' -AsSecureString)
).Password
try {
    .\.venv\Scripts\python.exe -m fleetops.bootstrap_user
} finally {
    Remove-Item Env:FLEETOPS_BOOTSTRAP_PASSWORD
}
.\.venv\Scripts\python.exe -m uvicorn fleetops.api.app:create_app --factory --host 127.0.0.1 --port 8000
```

The API requires both `FLEETOPS_APP_URL` and `FLEETOPS_AUTHENTICATOR_URL`; neither
has a fallback. Each pool validates its actual PostgreSQL login. Login uses only
the authenticator pool; authenticated domain requests use the app pool. Initial-user
bootstrap uses the separate deployment engine and does not require either API URL.

The command requires the configured migration-seeded organization and active bootstrap
HUMAN actor. It hashes the password with Argon2id and refuses any existing initial user
rather than replacing credentials. A transaction lock serializes concurrent bootstrap
commands; uniqueness constraints provide an additional database boundary. The command
prints only the created user ID.

Usernames are exact and case-sensitive within the configured organization. Login has
no organization selector. `FLEETOPS_SESSION_SECONDS` defaults to 43,200 (12 hours) and
must be between 1 and 86,400. Runtime and migration connections verify their actual
PostgreSQL 16 login identities; neither falls back to a privileged role.

| Endpoint | Authentication and behavior |
| --- | --- |
| POST /auth/login | Sole unauthenticated write. JSON username/password; verifies Argon2id under the configured org's RLS context and returns access_token, token_type, expires_at. Failure is 401 with no database write. |
| GET /auth/me | Bearer required; returns the trusted org/user/actor tuple. |
| POST /auth/logout | Bearer required; deactivates only the presented session. |
| POST /actors | Bearer required; accepts type and display_name. Performer and org come from the session. |
| GET /actors | Bearer required; lists only the resolved tenant's actors. |
| POST /parties | Bearer required; accepts display_name and a nonempty role set. |
| GET /parties | Bearer required; lists only the resolved tenant's parties and their roles. |

Passwords use Argon2id because human passwords need deliberately expensive hashing.
Bearer tokens use 32 CSPRNG bytes (256 bits of source entropy); the raw token is returned
only on issuance, while the database stores its SHA-256 digest. Applying Argon2 to each
bearer lookup would add password-hashing cost without addressing the bearer threat model.
Bearer credentials belong in the Authorization header. JSON org/performer fields are
rejected; arbitrary headers and query parameters do not confer authority. No password
reset, role hierarchy, audit table, or org-management UI is included.

### Focused acceptance checks

```powershell
.\.venv\Scripts\python.exe -m pytest -v server/tests/slice2
```

The focused suite covers migrations and seeds, all six tables' RLS, INSERT/UPDATE
tenant-safe references, HUMAN-only user linkage, DEVICE attribution, actual connection
reuse, resolver ACLs and invalid credentials, password verification, token entropy and
storage, HTTP spoofing, logout, and the actual bootstrap command including concurrent
attempts. The complete suite also retains the historical Slice 1 baseline and all
default-deny/history-permission proofs. AnyIO 4.10.0 is pinned for compatibility with
the existing Starlette 0.47.3 TestClient while retaining warnings-as-errors.

## Slice 3 catalog and external references

Revision `0003_catalog` adds only `items` and `external_references`, their constraints,
indexes, and RLS/grants. Downgrade to `0002_identity_auth` removes the catalog structures;
Slice 2 rows, policies, privileges, and `resolve_session` remain intact. Each migration
owns a frozen schema snapshot; the complete suite checks head against runtime metadata.

Items have permanent UUIDv7 identities. The organization/manufacturer/MPN/revision tuple
prevents duplicate catalog entries using PostgreSQL 16 `UNIQUE NULLS NOT DISTINCT`.
Two absent revisions therefore compare as equivalent for duplicate prevention.
Different revisions and different manufacturers may share an MPN. That tuple never
replaces the FleetOps ID.

The manufacturer is a same-organization Party with a MANUFACTURER role. A generated
fixed role discriminator and composite FK reference the existing party-role membership.
PostgreSQL rejects a vendor-only or cross-tenant manufacturer and prevents removal or
retyping of a membership still used by an item. Slice 2 definitions and permissions
are unchanged.

Item UOM is the current catalog default, using TEXT plus a CHECK for:
EA, M, MM, CM, IN, FT, G, MG, KG, ML, L. Packaging forms are excluded.
Later PO/receipt records must retain their captured UOM under D13; no historical
transactional tables are introduced in this slice. Export classification is optional
text, and the controlled flag may be true while classification is still unknown.
These fields record catalog facts; no export-control workflow is implemented.

[ADR-003](docs/architecture/ADR-003.md) governs reference cardinality. Only an identical
attachment to the same explicit entity is a duplicate:
`(org_id, entity_type, entity_id, system, reference_type, external_value)`.
ITEM and PARTY are the only supported target types. The service verifies target
existence and visibility on its authenticated runtime connection under RLS before
attaching or listing references. The registry can be extended when later targets
are introduced, with a corresponding CHECK migration.

External-reference search matches system, reference type, and value exactly, including
case and surrounding whitespace, and returns every matching attachment with its FleetOps
target ID. Zero, one, and multiple results are all valid. It is never a unique selector
for a write. Catalog descriptive text is trimmed at the API boundary.

All endpoints below require the existing bearer session and trusted transaction-local
organization context. Missing/cross-tenant targets return 404; duplicate catalog entries
or attachments return 409; invalid input or manufacturer relationships return 422.
Missing or invalid authentication returns 401.

| Endpoint | Behavior |
| --- | --- |
| POST /items | Create from manufacturer_party_id, manufacturer_part_number, description, uom, serialized; optional revision, export_classification, export_controlled, active. |
| GET /items | List the authenticated tenant's catalog, including inactive entries. |
| GET /items/{item_id} | Get by FleetOps UUID only. |
| PATCH /items/{item_id} | Update catalog descriptive/default fields and active flag. Omitted fields remain unchanged; only revision and export_classification may be explicitly null. |
| DELETE /items/{item_id} | Deactivate via active=false; preserve the row, identity, and attached references. |
| POST /external-references | Attach system, reference_type, external_value to explicit entity_type and entity_id. |
| GET /external-references | List references using required entity_type and entity_id query parameters. |
| GET /external-references/search | Search using required system, reference_type, external_value query parameters; returns a collection of attachments and target IDs. |

Creation records the session's actor and database creation time. Item updates record
the current authenticated actor and database update time while preserving original
creation attribution. These are current catalog metadata, not an operational history
ledger. Request bodies cannot supply IDs, org scope, performers, or timestamps.

Both new tables use ADR-002's fail-closed RLS and composite tenant-safe ordinary FKs.
Runtime grants are SELECT/INSERT on both, plus UPDATE only on mutable item fields and
update attribution/time. Identity, organization, and original creation fields have no
UPDATE grant. There is no runtime physical DELETE or TRUNCATE grant on either table.

Run the catalog acceptance and adversarial proofs with:

```powershell
.\.venv\Scripts\python.exe -m pytest -v server/tests/slice3
```

The tests cover catalog duplicates including NULL revisions, manufacturer-role membership
and removal, exact shared-reference cardinality, supported target validation, scoped API
operations, spoofing, runtime RLS semantics, pool reuse, minimum grants, UOM/classification,
and migration preservation of Slice 2 data and security. Prior revision proofs inspect
their actual historical revision and restore head; their original boundaries remain intact.


## Slice 4 facilities and locations

Revision `0004_space` adds only `facilities` and `locations`, indexes, constraints,
and RLS/grants. It honours D10, D11, D18, ADR-001, ADR-002, and Build Handoff v1.2.
Downgrade to `0003_catalog` removes space tables; prior rows, constraints, indexes,
ownership, policies, grants, and the authentication resolver remain intact.

Both entities use server-assigned UUIDv7 identity, authenticated organization and Actor
attribution, an active flag, and server-side timezone-aware creation timestamps.
Facility timezone is IANA metadata, validated using Python's local `zoneinfo` database
and the explicitly pinned `tzdata` fallback. Validation performs no network access.
Invalid keys are rejected before insertion. Facility timezone never changes the
connection timezone or converts persisted timestamps into local wall time.

Locations belong to exactly one facility. The composite `(org_id, facility_id)` FK
prevents referencing another tenant's facility. The parent FK carries
`(org_id, facility_id, parent_location_id)`, targeting a unique
`(org_id, facility_id, id)` key. A root has a null parent. PostgreSQL rejects missing,
cross-tenant, and cross-facility parents. Location kind is TEXT plus a CHECK for exactly
SITE, ROOM, RACK, BIN, STATION, DOCK, VEHICLE, OTHER.

Codes are unique within `(org_id, facility_id)`; the same code may occur in different
facilities. API text is trimmed at the boundary and code case is preserved.
The database rejects direct self-parenting and deeper cycles, including cycles
submitted in one multi-row INSERT. Creation assigns a fresh server UUID and
requires any parent to exist; there is no parent reassignment operation.

Every route below requires the existing bearer session and transaction-local RLS context.
Bodies reject caller-supplied identity, organization, performer, and timestamps.

| Endpoint | Behavior |
| --- | --- |
| POST /facilities | Create from name, timezone, and optional active (default true). |
| GET /facilities | List visible facilities, including inactive entries. |
| POST /locations | Create from facility_id, code, name, kind, optional parent_location_id (default null), and optional active (default true). |
| GET /locations | List visible locations, including inactive entries. |
| GET /locations/{location_id}/path | Return full location records in deterministic root-to-target order. |

Path resolution uses one recursive PostgreSQL CTE on the runtime RLS connection.
Each recursive join requires the same organization and facility. A visited-ID array
detects cycles, and a remaining unresolved parent is an error. No arbitrary depth limit
silently truncates ancestry. Missing and invisible targets both return 404; cyclic or
incomplete ancestry returns 409 without partial records. Duplicate facility-local codes
return 409; invalid values or relationships return 422; invalid credentials return 401.

Runtime privileges are SELECT and INSERT only on both space tables. UPDATE, DELETE,
and TRUNCATE are denied, including column-level UPDATE. The active field has no
deactivation workflow in this slice. Corrective revision `0005_space_cycle_guard`
adds a non-deferrable AFTER INSERT constraint trigger. Its VOLATILE SECURITY INVOKER
function sees the complete statement, follows same-tenant/facility ancestry under RLS,
and rejects cycles atomically with SQLSTATE 23514 / `ck_locations_acyclic`.
It grants no runtime EXECUTE or additional table privileges. Downgrade removes only
the guard and its function, restoring historical 0004 behavior. Parent-changing
operations remain deferred and require renewed concurrency analysis. Path resolution
retains defensive cycle detection for deliberately corrupted or legacy data.

Run the focused PostgreSQL acceptance proofs with:

```powershell
.\.venv\Scripts\python.exe -m pytest server/tests/slice4 -v
```

Tests cover creation and attribution, local timezone validation, root/child paths, all
eight kinds, database constraints, same-code reuse, strict inputs, authentication,
fail-closed RLS, adversarial UPDATE/DELETE under temporary test grants, WITH CHECK,
pool reuse, production privileges, and Slice 3/4 migration round trips. Malformed-path
tests use actual persisted rows; test-only constraint changes are restored in cleanup.

External-reference targets remain ITEM and PARTY. Space records represent where things
can be; assets, movement, custody, receiving, and inventory remain deferred.

Slice closure requires independent reviewer approval. No commit, push, or Slice 5 work
is part of this implementation.


## Slice 5 Assets and controlled lifecycle transitions

Revision `0006_assets` adds `assets`, `asset_identifiers`, `asset_transitions`,
the `transition_asset` function, and one Item uniqueness constraint supporting
serialized eligibility. Migrations 0001–0005 remain frozen. Downgrade restores
0005, including removal of the added Item constraint, while retaining the Location
cycle guard and prior schema/security.

Assets use permanent server-generated UUIDv7 identity. The editable Asset tag is
unique within the organization. Owner is required; custodian and location may be
unknown. Revision 0006 constrains assignment to NULL; Slice 7 activates its event FK.
All existing tenant-owned
references use composite organization keys. A fixed TRUE discriminator and Item FK
prevent an Asset from referencing a nonserialized Item, including later attempts
to change an Item already backing an Asset to nonserialized.

Identifiers have one of MANUFACTURER_SERIAL, PCB_SERIAL, MAC, IMEI or OTHER.
Readable values are unique per organization/type through a partial unique index.
An unreadable marker uses NULL value and a nonblank reason; it never substitutes
the string "UNREADABLE" for a missing serial. Multiple unreadable markers are valid.

Production creation belongs to receiving in Slice 9. Slice 5 exposes no Asset
creation service, onboarding command, identifier-write endpoint or deletion route.
Only controlled tests create the initial Asset/transition pair: Asset version 1
and RECEIVED, with result version 1 and NULL → RECEIVED. Slice 6 fixtures also
declare the matching immutable initial physical facts in that same transaction.
Slice 7 fixtures also declare the separate initially-unassigned witness there.
Later transition from-state
must be non-null. Asset and transition versions must be positive. An INSERT-only
database trigger requires every ordinary Asset INSERT, including owner setup, to
begin at version 1 / RECEIVED. It creates no history and does not restrict later
transition updates.

`assets.version` is one global optimistic-concurrency token under ADR-005. The
function resolves the authenticated Actor from organization and credential context,
then locks the tenant-scoped Asset with SELECT FOR UPDATE before comparing its
version to the expected version. It validates authoritative latest history,
requested from-state and the state projection. No Actor argument or old overload
remains; the additional transition UUID is generated by Python. A successful operation inserts one
transition with result version N+1 and updates only state/version atomically. The
lock persists through transaction completion. Stale operations and inconsistent
history reject without partial writes or silent repair.

State history is ordered by global `result_version`, with uniqueness on
`(org_id, asset_id, result_version)`. The latest state transition may have a lower
version than the Asset after future orthogonal operations. There is no contiguous
transition counter, per-projection version or shared version-register table.
Reconciliation reports missing history, state mismatch and history ahead of the
Asset; a lower state-history version alone is valid.

Python owns the lifecycle graph. ON_HOLD exits use the entry state in authoritative
history, held stable by the same Asset row lock while Python validates the dynamic
edge and SQL admits it. State transitions leave owner, custodian, location and
assignment unchanged. RETIRED remains terminal with legal incoming graph edges,
but production execution fails closed until disposal evidence is verifiable.
The nullable evidence parameter exists now; a database CHECK rejects every non-null
`evidence_ref` until Slice 11. Correction and client-operation fields are retained
as seams only; no correction or idempotency workflow is implemented.

The SQL function accepts asset ID, expected version, from-state, to-state, reason,
occurrence time, evidence reference, client operation ID, and an additional
transition UUIDv7 generated by the Python service. It takes no Actor argument. Its owner is fleetops_migrator,
its search path is fixed to pg_catalog/pg_temp, and PUBLIC EXECUTE is revoked.
Only fleetops_app receives runtime EXECUTE. The request body cannot supply the
Actor, organization, result version, recording time, evidence or generated ID.
Occurrence time must be timezone-aware and is preserved as the reported instant;
recording time comes from PostgreSQL and never orders authoritative history.

Runtime table grants are SELECT only on all three new tables, plus column UPDATE
on Asset tag, description and update attribution/time. History INSERT is performed
only by the controlled function, which pairs it with its projection. No direct
projection UPDATE, history UPDATE/DELETE, creation INSERT or TRUNCATE is granted.
All tables retain ADR-002 RLS USING/WITH CHECK and the restrictive tenant boundary.

| Endpoint | Behavior |
| --- | --- |
| GET /assets | List the authenticated tenant's Assets. |
| GET /assets/{asset_id} | Read by FleetOps UUID only. |
| PATCH /assets/{asset_id} | Edit non-null asset_tag/description; record authenticated updater and database time without incrementing operational version. |
| GET /assets/{asset_id}/identifiers | Read identifiers captured in controlled fixtures until receiving exists. |
| POST /assets/{asset_id}/transitions | Accept expected_version, from_state, to_state, reason and aware occurred_at; return the immutable transition. |
| GET /assets/{asset_id}/transitions | Return result_version ASC, regardless of timestamp/UUID order. |
| GET /health/assets/reconciliation | Report this tenant's state discrepancies; never repair them. |

All seven routes require bearer authentication. Missing/invisible targets return
404, stale or inconsistent state/version and duplicate tags return 409, and illegal
lifecycle edges or invalid inputs return 422. Strict request models reject caller
identity, attribution, projection and timestamp authority.

Run focused proofs with:

```powershell
.\.venv\Scripts\python.exe -m pytest -v server/tests/slice5
```

The suite covers all executable lifecycle edges; ON_HOLD return-state history;
retirement fail-closed; initial-history checks; readable/unreadable identifiers;
serialized Item and tenant-safe references; strict API authority; immutable history
and projection grants; state/missing-history/impossible-version discrepancies;
timestamp-independent ordering; future global-version gaps; exact migration round
trips and Alembic metadata checks. Concurrency proofs use separate runtime logins
and PostgreSQL-observed blocking to prove exactly one same-version winner and
post-lock version validation, including lock retention after function return.

## Slice 6 movement, custody and ownership history

Revision `0007_asset_fact_history` adds `asset_initial_facts`, `asset_movements`,
`asset_custody_changes`, `asset_ownership_changes`, and three atomic functions.
Migrations 0001–0006 and the accepted architectural documents remain unchanged.
Downgrade removes only the new objects, preserving the authentication boundaries,
Asset/lifecycle structures and Location cycle guard.

Under [ADR-007](docs/architecture/ADR-007.md), each Asset has one immutable creation
baseline, identified by `asset_id` with a unique `(org_id, asset_id)` constraint.
It records initial owner, nullable custodian and nullable location, performing
Actor, claimed occurrence time and database recording time. It has no
`result_version` and does not increment the Asset version. The initial lifecycle
transition remains the sole producer of global version 1.

The baseline is independent historical evidence. Before the first change of a
physical fact, its locked Asset projection must agree with the corresponding
baseline value, using NULL-safe comparison. Missing baseline or disagreement
rejects without writes. Later changes derive the prior fact from the latest row
of that class by `result_version`, checking projection agreement and rejecting
history ahead of the Asset. Legitimate later facts need not match creation values.
The baseline never changes with movement, custody, ownership, state or descriptive edits.

There is no migration backfill and no lazy baseline creation from Asset projections.
Controlled fixtures declare Asset, initial transition and baseline together;
incomplete-history tests explicitly opt out. Existing pre-Slice-6 Assets without
baselines remain incomplete and cannot admit a first physical change. Production
creation remains Slice 9 receiving; no Asset or baseline creation endpoint is exposed.

`move_asset`, `change_custody` and `change_ownership` reuse the Slice 5 protocol:
resolve trusted org and credential-bound Actor, lock the tenant-scoped Asset with
`SELECT FOR UPDATE`, compare expected version after locking, validate authoritative
prior fact, append N+1, and update only the relevant projection and global version.
The lock remains held until the caller's transaction completes. Custody never
implies ownership, and no physical change alters lifecycle, assignment, another
physical projection, or the Location hierarchy.

Each function is migrator-owned, SECURITY DEFINER, and uses a fixed
`pg_catalog, pg_temp` search path with qualified tables and explicit tenant checks.
Only `fleetops_app` receives runtime EXECUTE; PUBLIC and the authenticator are denied.
No Actor argument exists. All four new tables have composite tenant-safe references,
RLS USING/WITH CHECK and SELECT-only app grants. Runtime INSERT, UPDATE, DELETE and
TRUNCATE are denied, including baseline writes and direct Asset projection updates.

All six routes below require bearer authentication. POST accepts only
`expected_version`, the listed destination, nonblank `reason`, and timezone-aware
`occurred_at`. The service assigns UUIDv7; PostgreSQL derives Actor and `recorded_at`.
GET returns `result_version ASC`, independent of occurrence or recording timestamps.
Both timestamps and attributed Actor are exposed on each history row.

| Endpoints | Destination fact |
| --- | --- |
| POST /assets/{asset_id}/movements; GET /assets/{asset_id}/movements | `to_location_id`, required field; explicit NULL records unknown location. |
| POST /assets/{asset_id}/custody-changes; GET /assets/{asset_id}/custody-changes | `to_custodian_party_id`, required field; explicit NULL records unknown custody. |
| POST /assets/{asset_id}/ownership-changes; GET /assets/{asset_id}/ownership-changes | `to_owner_party_id`, required non-null Party. |

Movement permits both known-to-unknown and unknown-to-known facts, preserving
nullable location truth. Both ownership endpoints' history values remain non-null.
Missing or invisible Assets return the same 404; stale versions, missing baseline
and inconsistent history/projection return 409; invalid targets or claims return 422.
Correction references and `client_op_id` are nullable history seams. Normal API
writes leave them NULL and reject them as input. SQL client-operation IDs provide
correlation only, with no idempotency or correction workflow.

`GET /health/assets/reconciliation` retains Slice 5 state discrepancy fields and
categories, and adds missing baseline, initial/latest physical projection mismatch,
per-class history ahead, missing global version, duplicate global version and
combined history ahead. One PostgreSQL statement checks a consistent snapshot
under tenant RLS and never repairs data. The Slice 6 combined sequence includes
transitions, movements, custody changes and ownership changes; Slice 7 extends it
with assignment events. Both creation witnesses and configurations are excluded.
Every version from 1 through `assets.version` must occur exactly once
across those histories. Individual histories may have gaps.

Missing versions are reported as inclusive `[start, end]` pairs in
`global_missing_version_ranges`; duplicates and versions ahead of the Asset appear
in `global_duplicate_versions` and `global_ahead_versions`. Gap computation uses
observed event versions, so a corrupt very large Asset version does not require
expanding every absent integer. No second version counter or register is introduced.

Run the focused proofs with:

```powershell
.\.venv\Scripts\python.exe -m pytest -v server/tests/slice6
```

The proofs reconstruct all three physical facts from baseline plus five changes,
reject first/later corruption without repair, exercise runtime permissions and
credential spoofing, and reconcile a real six-version mixed history. Independent
PostgreSQL connections prove same-class and cross-class contention, post-lock
version checks and lock retention after function return. Migration tests compare
fresh and populated 0006 snapshots through downgrade/re-upgrade, prove no backfill,
and check exact schema/security restoration and Alembic metadata agreement.

Receiving, evidence, corrections and offline capture remain deferred.

## Slice 7 assignment and configuration records

Slice 7 revision `0008_assignment_configuration` follows `0007_asset_fact_history`.
It implements [ADR-008](docs/architecture/ADR-008.md) using three new tables and
two narrow assignment functions. Migrations 0001–0007 remain unchanged.

`asset_initial_assignment_facts` positively records that an Asset began unassigned.
Its `asset_id` primary key permits one witness, with tenant, credential-attributed
Actor, claimed `occurred_at`, and database `recorded_at`. It has no produced version.
Controlled fixtures declare the witness alongside the Asset, version-1 lifecycle
transition and initial physical facts in one transaction. Existing Assets receive
no backfill; missing witnesses remain incomplete. Production creation remains
Slice 9 receiving, with no Asset or witness creation endpoint here.

`asset_assignment_events` records immutable prior/resulting assignee pairs, Actor,
occurrence and recording times, reason, and produced global `result_version`.
Each pair is wholly NULL or a type/UUID; supported types are ACTOR, LOCATION and
PARTY. The atomic boundary validates both endpoints against the corresponding
same-tenant table. An event from unassigned to assigned is ASSIGN, assigned to
unassigned is UNASSIGN, and assigned to assigned is REASSIGN. Unassigned to
unassigned is rejected. Reassignment appends exactly once and consumes one version.

`assign_asset` automatically assigns or reassigns; `unassign_asset` ends the current
assignment. Both resolve the existing credential-bound Actor, lock the same
tenant-scoped Asset with `SELECT FOR UPDATE`, then check expected global version.
Before the first event, admission requires the positive witness and NULL projection.
Later admission uses the latest assignment event by result version, rejects history
ahead of the Asset, and verifies the projection. Failure appends nothing and repairs
nothing. Success appends N+1 and changes only assignment projection/global version;
the lock remains held through transaction completion.

While assigned, `current_assignment_id` references the latest establishing event.
The composite FK includes tenant and Asset identity. After UNASSIGN it is NULL.
No runtime projection UPDATE, event INSERT/UPDATE/DELETE/TRUNCATE, or witness write
is granted. Both functions are migrator-owned SECURITY DEFINER with fixed
`pg_catalog, pg_temp` search paths, explicit tenant checks, app-only runtime EXECUTE,
and no Actor argument. All three new tables retain fail-closed tenant RLS.

Assignment reads return the independent witness, events in `result_version ASC`,
and derived assignee intervals. Each establishing event's occurrence claim supplies
`started_at`; the next event's claim supplies `ended_at`, or NULL while open.
Stored events are never closed by UPDATE. Disagreeing clocks can produce an end
claim earlier than its start; the original claims remain intact. ON_HOLD preserves
assignment and produces no assignment event.

`asset_configurations` preserves image name/version, configuration profile, notes,
credential-derived `applied_by`, claimed `applied_at`, database `recorded_at`, and a
durably NULL-only evidence seam. Configuration appends change no Asset projection
or version and require no expected version. They use ordinary RLS-protected INSERT,
with column grants excluding attribution, recording time and ordering authority.
Database defaults and the existing attribution trigger enforce these values even
on raw runtime SQL. No additional elevated configuration function is installed.
UPDATE, DELETE and TRUNCATE are denied.

An internal globally allocated BIGINT `GENERATED ALWAYS AS IDENTITY` supplies
`configuration_seq`, with uniqueness, positive values and CACHE 1. Runtime roles
have no sequence privileges and cannot supply or reset this column. History uses
ascending sequence; current configuration uses the greatest visible sequence.
Allocation order can differ from commit order; rollback and per-Asset gaps are valid.
Concurrent configuration appends may both succeed. Ordinary FK locks still apply,
but no Asset optimistic-version protocol is added to configuration writes.

The sequence is omitted from request and response models, avoiding API disclosure
of global allocation gaps. Runtime SQL can read its own tenant's row sequences;
those gaps can reflect other Assets, tenants or aborted transactions, not an exact
other-tenant record count. The sequence itself and other tenants' rows are denied.
No per-tenant sequence registry or second Asset-version system is introduced.

All routes require bearer authentication and forbid extra request fields:

| Endpoint | Behavior |
| --- | --- |
| POST /assets/{asset_id}/assignments | Accept expected_version, assignee_type, assignee_id, reason and aware occurred_at; return one ASSIGN/REASSIGN event. |
| POST /assets/{asset_id}/unassignment | Accept expected_version, reason and aware occurred_at; return one UNASSIGN event. |
| GET /assets/{asset_id}/assignments | Return witness (NULL when missing), ordered events and derived intervals. |
| POST /assets/{asset_id}/configurations | Accept image_name, image_version, config_profile, optional notes and aware applied_at; return the immutable record. |
| GET /assets/{asset_id}/configurations | Return configuration history in internal sequence order. |
| GET /assets/{asset_id}/configurations/current | Return greatest visible sequence, or NULL if no configuration exists. |

Missing and invisible Assets return the same 404; stale or inconsistent assignment
history returns 409; invalid business inputs/targets return 422. No request may
choose tenant, performing Actor, prior assignment, result version, recorded time,
sequence, evidence, correction or client-operation authority. Correction pointers
and SQL `client_op_id` remain nullable seams without correction or apply-once behavior.

The existing reconciliation endpoint now checks all five version-producing classes.
It independently reports missing assignment witnesses, invalid/pre-history
projection targets, latest-event disagreement and ahead-of-Asset assignment history.
Global missing, duplicate and ahead versions remain detectable; both witnesses and
configurations are excluded. It never repairs discrepancies.

Run the Slice 7 PostgreSQL and API proofs with:

```powershell
.\.venv\Scripts\python.exe -m pytest -v server/tests/slice7
```

Tests cover reconstruction, strict inputs, attribution, permissions/RLS, corruption,
an eight-version five-class history, observed same-class/cross-class lock contention,
post-lock stale rejection, reversed configuration commit order and aborted gaps.
Fresh and populated 0007 migration round trips compare schema, rows, functions,
triggers, roles, policies, column ACLs and identity/sequence ownership; Alembic checks
metadata agreement. Downgrade restores the 0007 NULL-only projection constraint
before removing any Slice 7 objects, and fails atomically if assignments remain active.
It does not synthesize unassignment to make a downgrade succeed.

Slice 7 is the accepted baseline for Slice 8.

## Slice 8 procurement expectations

Migration head `0009_procurement` follows `0008_assignment_configuration`.
Purchase orders record supplier expectations. A same-tenant VENDOR Party supplies
the order; the Item manufacturer remains a separate catalog fact. Opaque UUIDs
identify orders and each permanent line version. `po_number` is display context
and may repeat; a positive `line_number` groups versions within one order.

Orders begin DRAFT. Draft PO metadata and line Item, quantity, price and expected
date may be edited. Creation identity, original attribution, lineage and the UOM
snapshot remain protected. The explicit issue operation changes DRAFT to ISSUED,
records the credential-derived issuer and server UTC time, and freezes the order
and every existing line in one transaction. No minimum-line approval workflow is
introduced, so an empty draft may be issued. CLOSED and CANCELLED remain vocabulary
values without transition endpoints or amendment authority in this slice.

Issued rows reject UPDATE and DELETE, including ordinary migrator DML. An amendment
inserts a new line with `supersedes_line_id` pointing to the exact active predecessor
in the same tenant, PO and logical line number. It changes no predecessor column,
timestamp or attribution. Each successor is immediately frozen. Partial unique
indexes enforce one root per logical line and one successor per predecessor. An
immediate composite foreign key, existing-predecessor check and immutable pointers
prevent cycles. Line writes and issuance hold a conflicting PO row lock through
commit; competing roots or amendments cannot both win. No PO version or global
procurement sequence is used. Distinct lineages on one PO serialize at this boundary.

Active status means no successor exists; superseded status means a successor exists.
Neither is stored. History returns every version ordered by logical line number and
root-to-leaf pointers, independent of UUID or timestamp order. A future receipt can
reference the exact permanent line UUID without dynamically retargeting an amendment.

Every new line version copies its selected Item's current `items.uom` under an Item
row lock. The caller cannot supply UOM. For example, an EA root remains EA after the
Item default changes to M; a subsequent valid version snapshots M. Draft Item edits
also retain the original line snapshot. The governed UOM vocabulary is unchanged.
Quantity is positive finite PostgreSQL NUMERIC; unit price is nonnegative finite
NUMERIC, with no imposed scale or silent scale rounding. Price remains procurement
context. Optional `expected_date` is a finite calendar date, not a receipt event time.

All nine routes require bearer authentication and reject extra request fields:

| Endpoint | Behavior |
| --- | --- |
| POST /purchase-orders | Create a DRAFT from vendor_party_id, po_number and optional notes. |
| GET /purchase-orders | List tenant-visible orders. |
| GET /purchase-orders/{po_id} | Read one order. |
| PATCH /purchase-orders/{po_id} | Edit draft vendor, number or notes. |
| POST /purchase-orders/{po_id}/issue | Issue atomically; accepts no authority fields. |
| POST /purchase-orders/{po_id}/lines | Create a draft root from line_number, item_id, quantity, unit_price and optional expected_date. |
| GET /purchase-orders/{po_id}/lines | Read all immutable versions and derived active/superseded status. |
| PATCH /purchase-orders/{po_id}/lines/{line_id} | Edit permitted draft line inputs. |
| POST /purchase-orders/{po_id}/lines/{line_id}/supersede | Append an amendment from item_id, quantity, unit_price and optional expected_date. |

Missing and invisible targets return 404, stale/inadmissible workflow state returns
409, and invalid business inputs or relationships return 422. Actor IDs, organization,
server timestamps, UOM, lineage/active markers, receipt and accounting authority are
excluded from requests. Existing ADR-006 creator/updater triggers bind direct runtime
SQL as well as HTTP. Two narrow SECURITY INVOKER guards enforce procurement invariants;
no new SECURITY DEFINER function or authentication authority is added. Tenant RLS and
same-tenant foreign keys cover both tables. Runtime DELETE and TRUNCATE are denied.

Procurement leaves Asset versions, histories, projections and reconciliation unchanged.
It creates no Assets or receipt records, adds no receiving or accounting workflow,
and changes no external-reference attachment/search behavior. Procurement amendments
express changed expectations; true record correction remains Slice 10. Slice 9 remains
unopened. Run the dedicated real PostgreSQL proofs with:

```powershell
.\.venv\Scripts\python.exe -m pytest -v server/tests/slice8
```

The proofs include raw runtime SQL, active-guard migrator updates, tenant/credential
attacks, observed concurrency, and fresh/populated 0008 downgrade/re-upgrade comparisons
of schema, data, functions, triggers, policies, privileges and the existing sequence.
Slice 8 awaits independent review; no commit or push is included.
