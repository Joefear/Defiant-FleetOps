# Defiant FleetOps

Slice 1 provides repository layout, database roles, an empty migration baseline,
and a PostgreSQL permission test harness. Governing decisions: D3, D18 and
Section 12 of the frozen architecture. No domain capability exists yet.

The governing source is
[Architecture & Boundary v0.1](docs/architecture/FleetOps_Architecture_Boundary_v0.1.docx).
The [Markdown transcription](docs/architecture/FleetOps_Architecture_Boundary_v0.1.md)
is subordinate to that DOCX. Implementation follows the
[v1.1 build handoff](docs/architecture/FleetOps_v0.1_Build_Handoff_v1.1.docx).
Disagreement requires STOP and an architecture amendment request, not a code workaround.

## Run Slice 1

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
.\.venv\Scripts\python.exe -m alembic downgrade base
```

Alembic connects only as an authenticated `fleetops_migrator` on PostgreSQL 16.
Migration connections use a catalog-only search path; migration DDL must name
the `fleetops` schema explicitly. Revision `0001_empty_baseline` has no-op upgrade/downgrade functions. At head,
`fleetops.alembic_version` is the only table and is migrator-owned. At base, that
bookkeeping table is empty. Downgrade deliberately retains the administrative
roles, locked schema and deny defaults; it does not drop cluster-wide roles or
restore PUBLIC privileges. The disposable test cluster is removed separately.

Later migrations call `grant_immutable_history(connection, table)` from
`fleetops.db.grants` in their DDL transaction. It removes existing table and column
grants to PUBLIC/the app and grants only SELECT and INSERT to `fleetops_app`.
It grants no sequence access or projection updates. Future tenant-owned records
must carry the handoff's required organization scope; Slice 1 creates none.
Future privileged functions must have a safe fixed search path, qualified object
references and explicit EXECUTE grants. The only such function here is test-only.
See PostgreSQL 16 documentation for
[default privileges](https://www.postgresql.org/docs/16/sql-alterdefaultprivileges.html)
and [SECURITY DEFINER functions](https://www.postgresql.org/docs/16/sql-createfunction.html).

Slice closure still requires reviewer approval. Do not begin Slice 2 or amend a
controlled architectural section as part of this foundation.
