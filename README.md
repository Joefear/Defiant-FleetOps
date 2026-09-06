# Defiant FleetOps

Slice 4 adds tenant-owned facilities, hierarchical locations, and complete ancestry
resolution to the existing identity, tenancy, and catalog foundation. It honours
D10, D11, D18, ADR-001, ADR-002, and Build Handoff v1.2.

The governing source is
[Architecture & Boundary v0.1](docs/architecture/FleetOps_Architecture_Boundary_v0.1.docx).
The [Markdown transcription](docs/architecture/FleetOps_Architecture_Boundary_v0.1.md)
is subordinate to that DOCX. Implementation follows the
[v1.2 build handoff](docs/architecture/FleetOps_v0.1_Build_Handoff_v1.2.docx).
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

The versioned administrative migration
`server/fleetops/db/bootstrap_001_roles.sql` creates the two login roles and the
`fleetops` schema in one transaction on a new, empty, dedicated database/cluster.
It refuses preexisting FleetOps roles or user objects. An administrative connection
runs this bootstrap once; neither FleetOps role gets role-creation privileges.

`fleetops_migrator` owns the schema and all subsequent Alembic DDL.
`fleetops_app` receives only explicit database CONNECT and schema USAGE initially.
Neither role is superuser, inherits other roles, creates roles/databases, replicates,
or bypasses row security. PUBLIC loses database access/TEMP and public-schema
access. Migrator-created objects grant PUBLIC and the app role nothing by default,
including removal of PostgreSQL's default PUBLIC function EXECUTE.

For a separately provisioned empty PostgreSQL 16 database, load the variables
shown in `.env.example` into your shell (the file is not loaded automatically):

```powershell
.\.venv\Scripts\python.exe -m fleetops.db.bootstrap
.\.venv\Scripts\python.exe -m alembic upgrade head
```

Alembic connects only as an authenticated `fleetops_migrator` on PostgreSQL 16.
Migration connections use a catalog-only search path; migration DDL must name
the `fleetops` schema explicitly. Revision `0001_empty_baseline` has no-op
upgrade/downgrade functions. At that revision,
`fleetops.alembic_version` is the only table and is migrator-owned. At base, that
bookkeeping table is empty. Head includes the Slice 2, Slice 3, and Slice 4 tables described below.
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

`fleetops_app` gets SELECT on the organization root and SELECT/INSERT on the other
five tables. Session UPDATE is restricted to `active` for logout. It cannot replace
a session digest, change its user, extend expiry, update other tenant tables, delete
tenant rows, or TRUNCATE any tenant table. Bootstrap uses the same runtime role.

The migration helper replaces table and live-column ACLs, enables RLS, and installs
matching USING/WITH CHECK predicates. A restrictive tenant policy ensures an additional
permissive policy cannot bypass the boundary. Missing or cleared context sees no rows;
there is no default tenant. The setting is always transaction-local, including login
and bootstrap, and disappears on commit/rollback before a connection is reused.

Some adversarial tests temporarily grant UPDATE/DELETE inside the disposable database
to prove RLS independently of the narrower production grants. They assert hidden-row
rowcount zero and visible wrong-tenant rejection, then restore the production grants.
They do not change runtime privileges to make an endpoint work.

`resolve_session(bytea)` is the sole production SECURITY DEFINER function. It is owned
by the migrator, uses qualified tables and `search_path = pg_catalog, pg_temp`, and
grants EXECUTE only to the runtime role (besides the owner's inherent access). PUBLIC
has no EXECUTE. An exact valid SHA-256 digest returns only `org_id, user_id, actor_id`.
Unknown/expired/inactive credentials, users, and actors return no row. The resolver
writes no data and sets no tenant context; the trusted request dependency sets the
returned org inside the transaction before ordinary RLS access.

### Create the initial user and run the API

The migration seeds one Defiant organization, one INTERNAL party (and its role
membership), and one bootstrap HUMAN actor. It creates no password-bearing user.
Set the administrative and runtime variables described in `.env.example`; that
file is documentation and is not loaded automatically. The organization identifier
below is an explicit trusted configuration value, not a fallback used by RLS.

For a fresh installation, run the administrative role bootstrap and Alembic upgrade
as described above. Then set the runtime URL and invoke the separate initial-user
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
The database rejects direct self-parenting. Creation assigns a fresh server UUID and
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
deactivation workflow in this slice. Adding parent-changing operations in a later task
requires full durable recursive cycle prevention. Arbitrary multi-row SQL can construct
a deeper cycle despite the direct-self CHECK; the current API cannot issue such writes,
and path resolution explicitly detects corrupt cycles.

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
