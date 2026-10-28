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

## Local device flat-table scripts

The separate [preview](scripts/preview_device_flat_table.sql) and
[population script](scripts/populate_local_flat_table.sql) were built against
the local `otodatawebportal_dev` schema. They do not change the Azure migration
history or the original 1,000-location extractor.

Run these commands from `extractor\scripts` using SQLCMD and Windows integrated
authentication (or supply your approved SQLCMD authentication options):

```powershell
Set-Location C:\Git\InsightQuest\extractor\scripts
# SELECT only; this can also run in a normal SQL editor.
sqlcmd -S . -d otodatawebportal_dev -E -b -i .\preview_device_flat_table.sql
# Rehearse creation/refresh and roll everything back.
sqlcmd -S . -d otodatawebportal_dev -E -b -v Apply=0 -i .\populate_local_flat_table.sql
# Only after reviewing the rehearsal, explicitly persist the refresh.
sqlcmd -S . -d otodatawebportal_dev -E -b -v Apply=1 -i .\populate_local_flat_table.sql
```

The population script requires SQLCMD mode for the shared `:r` query and the
explicit `Apply` variable. It is restricted to `otodatawebportal_dev`, takes a
transaction-scoped lock, and refreshes **all rows** of `dbo.[Insight Project]`
atomically. Source tables are never changed. An empty source or a target schema
that differs from the script causes an error and rollback; an existing legacy
three-column table is not silently replaced. Use a dedicated SQL session.
Even a successful rehearsal needs target create/write permissions. The counts
printed during rehearsal describe the proposed refresh, not persisted data.

### Confirmed mappings and approved selection rules

The query emits one row per `Devices.Id`, including inactive/deleted devices
with explicit status flags. There is no sample limit. Local foreign keys confirm
`DeviceLocations.Id -> Devices.Id`: the original sample IDs are already device
IDs, not unrelated location identities.

| Output | Source and selection |
| --- | --- |
| `Id`, `SerialNumber`, `DeviceType` | `Devices.Id`, `SerialNumber`, `Type` |
| `PurchaseDate`, `WarrantyStartDate` | Earliest nondeleted `DeviceWarranties.StartDate`; **a warranty-start proxy, not a confirmed purchase/shipment date** |
| `PurchaseDateSource` | Explicit label identifying that proxy |
| `DeviceInitialInstallDate` | `Devices.InitialInstallDate`, without fallback |
| `AnalysisInstalledAt` | Tank installation date, falling back to initial device installation date |
| `TankInstallDate` | Earliest non-null `Tanks.InstalledDate` through `Sensors -> TankSensors -> Tanks`; deleted sensors, links, and tanks excluded |
| `CompanyId` | Most recently created active, nondeleted owner (`IsOwner=1`); descending ownership `Id` breaks ties |
| `CompanyName`, `TerritoryId`, `Territory` | Selected owner's company, then `Companies.TerritoryId -> Territories.Id` |
| `Latitude`, `Longitude`, `LastGpsDate` | Complete coordinate pair from `DeviceLocations`, otherwise the pair and timestamp from `Devices`; no mixing of sources |
| `DeviceLat`, `DeviceLong` | Aliases preserving the original extractor's coordinate names |
| Status/count columns | Device/company active/deleted flags, eligible ownership count, distinct nondeleted linked tank count, and nondeleted warranty count |

Company/territory rows are not filtered out by status, so historical attribution
is retained; ownership selection represents the current owner, not necessarily
the purchaser at the warranty start date. Tank selection does not require active
status and is not proof that a device was attached to that tank on its install
date. Counts expose multiplicity instead of duplicating device rows.

There is no verified local order/shipment source. Shipment dates, purchase
amounts, eligible shipped units, customer-size categories, market indicators,
and inventory inputs are deliberately absent, not fabricated.

For the dashboard, select database mode and point `IQ_SOURCE_TABLE` at
`dbo.Insight Project`. Existing device/customer/date/type/coordinate defaults
match this output; additionally set `IQ_COL_TERRITORY=Territory` to enable that
breakdown. Keep unsupported mappings empty. Purchase-measure charts remain
unavailable without a confirmed numeric measure. Any analysis using
`PurchaseDate`, including Ask the Data and purchase-to-install delay, uses the
warranty proxy and must not be presented as actual order or shipping timing.

## Azure deployment preparation

Azure inspection on 2026-10-26 found approximately 4.27 million devices and
4,265,203 existing flat-table rows, rather than the local empty target.
`Latitude` and `Longitude` were `int` at inspection. Migration history contains
versions 0003-0005 whose SQL files are absent from this checkout. Do not run the
local full-refresh script or the incomplete migration runner against Azure.

The [Azure pilot](scripts/pilot_azure_flat_table.py) uses the repository root's
existing `.env` `IQ_DB_*` settings, encrypted SQL authentication, and certificate
validation. It never prints credentials and accepts only the approved Azure
server/database. Run it with the root application's environment:

```powershell
Set-Location C:\Git\InsightQuest
.\.venv\Scripts\python.exe .\extractor\scripts\pilot_azure_flat_table.py
```

It samples the first 100 devices by ID, reuses the shared preview query, checks
unique device IDs, and writes only a temporary table before always rolling back.
It does **not** rehearse altering the large target, backfill it, or update
migration history. The verified pilot returned 100 unique rows, 94 warranty
purchase-date proxies, 97 companies, and 76 tank dates.

The [Azure schema upgrade](scripts/upgrade_azure_flat_table.sql) is a separate,
**unapplied review script**, not a numbered migration. It preserves existing
rows, changes coordinates to `decimal(9,6)`, and adds nullable enrichment fields.
Its default is rollback. Coordinate conversion can rewrite millions of rows,
consume transaction-log space, and hold schema locks; schedule and rehearse this
separately rather than treating the small pilot as proof of deployment safety.
Existing integer coordinate precision can only be recovered by source backfill.

The independent refresh runner below implements the subsequently approved
deployment without altering or fabricating the missing migration history.
Recover the original migration files before using the old migration runner.

## Azure incremental reconciliation

For an analysis-ready snapshot, use the
[full refresh runner](scripts/refresh_azure_flat_table.py), not the original
sample extractor or the local SQL script:

```powershell
Set-Location C:\Git\InsightQuest
# Stage all devices, rehearse reconciliation, then roll back target changes.
.\.venv\Scripts\python.exe -u .\extractor\scripts\refresh_azure_flat_table.py
# Explicitly commit the complete refresh.
.\.venv\Scripts\python.exe -u .\extractor\scripts\refresh_azure_flat_table.py --apply
```

The runner uses `.env` credentials without printing them, requires the approved
Azure server/database, and holds the migration application lock for its session.
It captures source device IDs, stages all devices with
[set-based aggregates](scripts/stage_azure_devices.sql), and checks staging
completeness before touching the target. The first 1,000 devices are compared
with the independent shared preview query to verify the selection rules.
Source attributes are read at staging time, not necessarily when IDs were
captured. Devices created after the captured ID list are
included on the next refresh; deletion of a captured device aborts promotion.

After successful staging, a single transaction adds nullable enrichment columns,
corrects decimal coordinate types, updates only changed target rows, inserts new
devices, and deletes target IDs absent from the captured source. `EXCEPT`
comparisons detect changes including transitions to/from NULL; unchanged rows
are not updated. Soft-deleted devices remain in the dataset with their flags.
**The runner never truncates or recreates the target.** Unexpected
columns/types, foreign-key references, or triggers stop the refresh. Count and
unique-ID checks plus a 1,000-device staging/target comparison precede commit.
Installation/purchase date indexes are added for dashboard filters.
Failures roll back promotion; readers may block during schema/data changes.
The first run updates most rows to populate the newly added columns; subsequent
runs skip unchanged rows. Initial coordinate ALTERs may rewrite the large table.

Successful commits and inserted/updated/deleted/unchanged counts are recorded in
`dbo.InsightQuestRefreshRuns`. This is an
explicit snapshot deployment independent of the incomplete numbered migration
checkout: existing `InsightProjectMigrations` records are checked during staging
and never rewritten or fabricated. The missing original migrations still need
to be recovered before using the original migration runner.

Staging lives in temporary tables and is discarded when the connection closes.
A failed run restarts from the beginning; retries are not automatic. Query
timeout is 30 minutes per statement and lock timeout is 30 seconds. Run on a
reliable host with sufficient database/tempdb/log capacity. Recurring scheduling
is intentionally deferred; this command can later be invoked by a scheduler
using the same restricted connection configuration.

### Installation-selected sample under the current storage quota

The initial full deployment was rolled back after Azure error 40544: the
database was capped at 32 GB with approximately 120 MB free. The approved
alternative is the **latest 1,000,000 installations**, ordered by
`COALESCE(TankInstallDate, DeviceInitialInstallDate)` descending, then device ID
descending. Devices without either installation date are excluded.

```powershell
.\.venv\Scripts\python.exe -u .\extractor\scripts\refresh_azure_flat_table.py --apply --sample-installations 1000000 --prune-first
```

`--prune-first` is a deliberate, separately approved **two-phase** operation:
after sample staging and validation, deletion of target IDs outside the sample
is committed, then existing target indexes are reorganized to compact sparse
pages and release storage. Schema enrichment and incremental reconciliation
then run in their own transaction. Failure after pruning leaves a reduced target;
rollback cannot restore excluded rows after that first commit. Re-run enrichment
after addressing capacity/errors. No source tables are deleted. The runner waits
up to five minutes for at least 400 MB of capacity after pruning; this is a
minimum headroom guard, not a guarantee that the full operation will fit.

For subsequent sample refreshes, omit `--prune-first` to keep reconciliation
atomic if capacity permits. New sample members are inserted, changed values are
updated, unchanged devices are skipped, and IDs outside the new sample are
removed. Staging still reads source data to rank the latest installations; this
is incremental target writing, not source change-data capture.

The dashboard should use database mode with:

```dotenv
IQ_DEMO_MODE=false
IQ_COL_INSTALLED_AT=AnalysisInstalledAt
IQ_COL_TERRITORY=Territory
IQ_SAMPLE_INSTALLATIONS=1000000
IQ_PURCHASE_DATE_IS_PROXY=true
```

This enables sample/proxy notices on all analysis pages and the main dashboard.
The sample is sufficient for exercising maps, territory/product breakdowns and
exports, but **not** population-wide purchase totals, pending-device analysis,
unbiased long-term trends, or production forecasting validation. It is selected
on installation recency, not a random sample. Purchase dates remain warranty
proxies; no shipment, inventory, market or customer-size inputs are fabricated.

If the first enrichment still exceeds the quota due to a large atomic update
and SQL's persistent version store, the separately approved bounded-transaction
mode is:

```powershell
.\.venv\Scripts\python.exe -u .\extractor\scripts\refresh_azure_flat_table.py --apply --sample-installations 1000000 --promotion-batch-size 5000
```

This commits schema changes first, then each group of 5,000 reconciled devices.
It verifies the final dataset, commits data, and builds each dashboard index
separately. **The dataset can be partially enriched between commits or after a
failure.** A later run stages the latest sample and skips already-matching rows;
it does not need to undo prior successful batches. This mode is not an atomic
publication mechanism and should run while analysts are not using the table.
The default mode, without this option, remains transactional.

### Verified deployment on 2026-10-26

The bounded-transaction sample deployment completed successfully. The target
contains exactly 1,000,000 unique device IDs, spanning installation timestamps
2025-07-26 through 2026-10-05. There are 997,117 warranty purchase-date proxies,
999,742 company assignments, 995,084 tank dates, and 963,961 valid coordinate
pairs. Coordinates are now `decimal(9,6)`. The first enrichment updated all
1,000,000 retained rows; no new IDs were needed. The separately approved pruning
removed 3,265,203 out-of-sample target rows; source tables and the five original
migration-history records were preserved.

The dashboard and all six result pages passed database-mode initialization
without application errors. Missing shipment/inventory/other unsupported inputs
remain explicit readiness messages. Existing result-row caps still apply:
narrow date filters or adjust limits before interpreting totals. Forecast
optimization emitted convergence warnings, so those outputs are exploratory,
not validated forecasts.

The repository-local `.env` selects Azure sample mode and the fallback
`AnalysisInstalledAt` field. Restart an already-running Streamlit process once
to reload its environment settings. Daily scheduling remains deferred. Only
333 MB of database capacity was free immediately after deployment; monitor
capacity/version-store cleanup before repeated jobs rather than assuming the
32 GB quota has ample headroom.
