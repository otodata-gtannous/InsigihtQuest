# Insight Project

Python core for a data extraction and restructuring pipeline. The initial test
extracts 1,000 device locations; interpretation is not implemented yet.

## Current schema

Target database: `Neevo_InsightQuest2026_D` on
`sql-neevo-insight-quest2026.database.windows.net,1433`.

`[dbo].[Insight Project]` contains:

| Column | SQL Server type | Behavior |
| --- | --- | --- |
| Id | uniqueidentifier | Non-null primary key; NEWID() default |
| DeviceLong | decimal(9,6) | Nullable; copied from DeviceLocations.Longitude |
| DeviceLat | decimal(9,6) | Nullable; copied from DeviceLocations.Latitude |

The runner also manages `[dbo].[InsightProjectMigrations]` for migration versions,
file names, SHA-256 checksums, and UTC application timestamps. Existing
application tables are read-only.

## Setup (PowerShell)

Requires Python 3.11+ and Microsoft ODBC Driver 18 for SQL Server.

```powershell
Set-Location C:\git\insight-project
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

VS Code's database connection is not automatically shared with Python. Without
a connection string, an interactive launch asks for the SQL username (default
`insightquest`) and a hidden password. Credentials are not saved.
Alternatively, set a connection string in the current terminal. For Microsoft Entra authentication,
when supported for your database account:

```powershell
$env:INSIGHT_PROJECT_CONNECTION_STRING = 'DRIVER={ODBC Driver 18 for SQL Server};SERVER=sql-neevo-insight-quest2026.database.windows.net,1433;DATABASE=Neevo_InsightQuest2026_D;Authentication=ActiveDirectoryInteractive;UID=your-work-email;Encrypt=yes;TrustServerCertificate=no;'
```

For another authentication method, supply your own valid ODBC connection string
through the same environment variable. Never commit credentials. The account
needs permission to create tables, alter future target schema, insert migration
records, select DeviceLocations, insert Insight Project rows, and acquire an
application lock.

## Launch and extract

Open this project folder in VS Code and press **F5**, choosing
**Insight Project: extract device locations** (requires the Python Debugger
extension). Or launch from PowerShell:

```powershell
Set-Location C:\git\insight-project
.\.venv\Scripts\python.exe -m insight_project
# Equivalent explicit command:
.\.venv\Scripts\python.exe -m insight_project run
```

Launching **commits pending migrations**, then **commits extraction** in a
separate transaction. If extraction fails, its inserts roll back, but completed
schema migrations remain applied. Success reports inserted and total row counts.

The test sample is the first 1,000 `DeviceLocations` rows ordered by `Id`, not
1,000 new rows per launch. Each output row uses the source `Id` as its GUID.
Nulls and exact decimal precision are preserved; this source table has no
`IsDeleted` column. SQL Server executes the copy locally under Python's
transaction control, without downloading GPS data to your machine.

Repeated launches insert only missing GUIDs from that sample. They do not
duplicate, update, delete, or refresh existing rows, even if source coordinates
later change. If source GUIDs change, the sample can change and introduce new
rows. This is a temporary test mapping, not the final analysis schema.

## Run a migration

First rehearse the pending migrations. They execute in a transaction and **roll
back by default**, including creation of the migration-history table:

```powershell
.\.venv\Scripts\python.exe -m insight_project migrate
```

After reviewing the schema, explicitly commit:

```powershell
.\.venv\Scripts\python.exe -m insight_project migrate --apply
```

Use a dedicated connection: the runner owns its transaction. All pending
migrations and history records commit together or roll back together. A
database-name guard prevents accidentally targeting another database. A
transaction-scoped application lock serializes concurrent migration runs.
Failures return a nonzero exit code; driver errors report their SQLSTATE without
echoing credentials.

The initial migration can adopt an already-created empty GUID-only table if
its schema matches. It does not drop or replace an existing object.

## Add the next migration

Add a UTF-8 SQL file such as
`insight_project\sql\0003_add_analysis_columns.sql`. Use ascending four-digit
versions and lowercase names. Do not edit migrations after applying them.

Write SQL without `GO`, transaction control, or result sets: the Python runner
owns transactions. Keep changes scoped to this project's tables. Schema/data
requirements will be defined before implementing the next migration. Data
updates to existing Otodata entities must follow their `UpdatedBy` audit rules.

Run the rollback rehearsal before `--apply`. Applied files are checksum-checked;
missing, edited, or out-of-order migration history stops the run. Checksums
normalize line endings, so Windows/Linux Git checkouts produce the same hash.

## Tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Tests use mocked connections and do not modify a real database. Live schema
verification should compare the sample's source GUIDs and coordinate values to
the output, and confirm repeated extraction inserts zero rows. The extraction
function also supports `apply=False` (its default) for a rollback rehearsal
against an already-migrated database.
