"""Stage source devices and atomically reconcile changed, new, and removed rows."""

import argparse
from pathlib import Path
import time

import pyodbc
from dotenv import dotenv_values

from pilot_azure_flat_table import ROOT, TARGET_DATABASE, open_connection


TARGET = "[dbo].[Insight Project]"


def quote(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


def stage_query(query: str) -> str:
    source = "FROM [dbo].[Devices] d"
    if query.count(source) != 1:
        raise ValueError("Preview query source changed; review batch selection.")
    return query.strip().removesuffix(";").replace(
        source,
        "FROM [dbo].[Devices] d JOIN #BatchDevices batch ON batch.[Id] = d.[Id]",
    )


def promote(
    connection: pyodbc.Connection, expected: int, apply: bool,
    *, sample_installations: int | None = None,
    batch_size: int | None = None,
) -> None:
    cursor = connection.cursor()
    try:
        cursor.execute("""
            SELECT c.name, t.name, c.max_length, c.precision, c.scale
            FROM tempdb.sys.columns c
            JOIN tempdb.sys.types t ON t.user_type_id = c.user_type_id
            WHERE c.object_id = OBJECT_ID('tempdb..#DeviceStage')
            ORDER BY c.column_id;
        """)
        columns = list(cursor.fetchall())
        names = [row[0] for row in columns]
        if not columns or len(names) != len(set(names)):
            raise ValueError("Invalid staging column metadata.")
        cursor.execute("""
            SELECT c.name, TYPE_NAME(c.user_type_id), c.max_length, c.precision, c.scale
            FROM sys.columns c WHERE c.object_id = OBJECT_ID('dbo.Insight Project');
        """)
        existing = {row[0]: tuple(row[1:]) for row in cursor.fetchall()}
        if not existing or set(existing) - set(names):
            raise ValueError("Missing target or unexpected target columns; refusing promotion.")
        for row in columns:
            if row[0] in existing and existing[row[0]] != tuple(row[1:]):
                if row[0] not in {"Longitude", "Latitude"} or existing[row[0]][0] != "int":
                    raise ValueError(f"Unexpected target type for {row[0]}; refusing promotion.")
        cursor.execute("""
            IF EXISTS (SELECT 1 FROM sys.foreign_keys
                WHERE referenced_object_id=OBJECT_ID('dbo.Insight Project'))
                THROW 51303, 'Target is referenced by foreign keys.', 1;
            IF EXISTS (SELECT 1 FROM sys.triggers
                WHERE parent_id=OBJECT_ID('dbo.Insight Project'))
                THROW 51304, 'Target has triggers; manual review required.', 1;
        """)
        before = cursor.execute(f"SELECT COUNT_BIG(*) FROM {TARGET};").fetchone()[0]
        print("Reconciling target schema.", flush=True)
        for row in columns:
            name, sql_type, length, precision, scale = row
            if sql_type in {"nvarchar", "nchar", "varchar", "char"}:
                size = "max" if length == -1 else str(length // 2 if sql_type.startswith("n") else length)
                definition = f"{sql_type}({size})"
            elif sql_type in {"decimal", "numeric"}:
                definition = f"{sql_type}({precision},{scale})"
            elif sql_type in {"uniqueidentifier", "bigint", "int", "smallint", "bit", "date", "datetime"}:
                definition = sql_type
            else:
                raise ValueError(f"Unsupported staging type for {name}: {sql_type}")
            if name not in existing:
                cursor.execute(f"ALTER TABLE {TARGET} ADD {quote(name)} {definition} NULL;")
            elif existing[name] != tuple(row[1:]):
                cursor.execute(f"ALTER TABLE {TARGET} ALTER COLUMN {quote(name)} {definition} NULL;")
        if batch_size:
            connection.commit()
            print("SCHEMA COMMITTED. Enrichment will commit in small batches.", flush=True)
        selected = ", ".join(quote(name) for name in names)
        values = ", ".join("source." + quote(name) for name in names)
        compared = [name for name in names if name != "Id"]
        assignments = ", ".join(f"target.{quote(name)} = source.{quote(name)}" for name in compared)
        old_values = ", ".join("target." + quote(name) for name in compared)
        new_values = ", ".join("source." + quote(name) for name in compared)
        updated = inserted = processed = 0
        last_id = None
        if batch_size:
            cursor.execute("""
                SELECT TOP (0) Id INTO #PromotionBatch FROM #DeviceStage;
                ALTER TABLE #PromotionBatch ADD PRIMARY KEY (Id);
            """)
        while True:
            batch_join = ""
            count = expected
            if batch_size:
                cursor.execute("TRUNCATE TABLE #PromotionBatch;")
                if last_id is None:
                    cursor.execute(
                        "INSERT #PromotionBatch SELECT TOP (?) Id FROM #DeviceStage ORDER BY Id;",
                        batch_size,
                    )
                else:
                    cursor.execute(
                        "INSERT #PromotionBatch SELECT TOP (?) Id FROM #DeviceStage WHERE Id>? ORDER BY Id;",
                        batch_size, last_id,
                    )
                count = cursor.execute("SELECT COUNT_BIG(*) FROM #PromotionBatch;").fetchone()[0]
                if not count:
                    break
                last_id = cursor.execute("SELECT TOP (1) Id FROM #PromotionBatch ORDER BY Id DESC;").fetchone()[0]
                batch_join = " JOIN #PromotionBatch batch ON batch.Id=source.Id"
            cursor.execute(f"""
                UPDATE target SET {assignments}
                FROM {TARGET} target JOIN #DeviceStage source ON source.Id = target.Id {batch_join}
                WHERE EXISTS (SELECT {new_values} EXCEPT SELECT {old_values});
                SELECT CONVERT(bigint, @@ROWCOUNT);
            """)
            updated += cursor.fetchone()[0]
            cursor.execute(f"""
                INSERT INTO {TARGET} ({selected})
                SELECT {values} FROM #DeviceStage source {batch_join}
                WHERE NOT EXISTS (SELECT 1 FROM {TARGET} target WHERE target.Id = source.Id);
                SELECT CONVERT(bigint, @@ROWCOUNT);
            """)
            inserted += cursor.fetchone()[0]
            processed += count
            if not batch_size:
                break
            connection.commit()
            if processed % 50_000 == 0 or processed == expected:
                print(
                    f"ENRICHMENT COMMITTED: {processed:,}/{expected:,}; "
                    f"updated={updated:,}, inserted={inserted:,}.",
                    flush=True,
                )
        cursor.execute(f"""
            DELETE target FROM {TARGET} target
            WHERE NOT EXISTS (SELECT 1 FROM #DeviceStage source WHERE source.Id = target.Id);
            SELECT CONVERT(bigint, @@ROWCOUNT);
        """)
        deleted = cursor.fetchone()[0]
        unchanged = expected - inserted - updated
        print(f"Deleted {deleted:,} absent rows; skipped {unchanged:,} unchanged rows.", flush=True)
        actual = cursor.execute(f"SELECT COUNT_BIG(*), COUNT_BIG(DISTINCT Id) FROM {TARGET};").fetchone()
        if tuple(actual) != (expected, expected) or unchanged < 0 or before + inserted - deleted != expected:
            raise ValueError("Target device counts do not reconcile; rolling back.")
        cursor.execute(f"""
            SELECT COUNT_BIG(*) FROM (
                SELECT {selected} FROM {TARGET}
                WHERE Id IN (SELECT TOP (1000) Id FROM #DeviceStage ORDER BY Id)
                EXCEPT SELECT {selected} FROM #DeviceStage
            ) differences;
        """)
        if cursor.fetchone()[0]:
            raise ValueError("Target sample differs from staging; rolling back.")
        if batch_size:
            connection.commit()
            print("DATA VERIFIED AND COMMITTED. Building dashboard indexes.", flush=True)
        # Useful for date-range queries without changing source-table indexes.
        for name, field in (
            ("IX_InsightQuest_AnalysisInstalledAt", "AnalysisInstalledAt"),
            ("IX_InsightQuest_PurchaseDate", "PurchaseDate"),
        ):
            cursor.execute(
                "SELECT COUNT(*) FROM sys.indexes WHERE object_id=OBJECT_ID('dbo.Insight Project') AND name=?",
                name,
            )
            if not cursor.fetchone()[0]:
                cursor.execute(f"CREATE INDEX {quote(name)} ON {TARGET} ({quote(field)});")
                if batch_size:
                    connection.commit()
        cursor.execute("""
            IF OBJECT_ID('dbo.InsightQuestRefreshRuns', 'U') IS NULL
                CREATE TABLE dbo.InsightQuestRefreshRuns (
                    Id uniqueidentifier NOT NULL PRIMARY KEY DEFAULT NEWID(),
                    CompletedAtUtc datetime2 NOT NULL DEFAULT SYSUTCDATETIME(),
                    PreviousRows bigint NOT NULL, RefreshedRows bigint NOT NULL,
                    UpdatedRows bigint NOT NULL, InsertedRows bigint NOT NULL,
                    DeletedRows bigint NOT NULL, UnchangedRows bigint NOT NULL,
                    SampleInstallations int NULL,
                    PurchaseDateSource nvarchar(80) NOT NULL
                );
            INSERT INTO dbo.InsightQuestRefreshRuns
                (PreviousRows, RefreshedRows, UpdatedRows, InsertedRows, DeletedRows,
                 UnchangedRows, SampleInstallations, PurchaseDateSource)
            VALUES (?, ?, ?, ?, ?, ?, ?, N'DeviceWarranties.StartDate (earliest nondeleted; proxy)');
        """, before, expected, updated, inserted, deleted, unchanged, sample_installations)
        counts = f"updated={updated:,}, inserted={inserted:,}, deleted={deleted:,}, unchanged={unchanged:,}"
        if apply:
            connection.commit()
            print(f"COMMITTED: {before:,} -> {expected:,} devices; {counts}.", flush=True)
        else:
            connection.rollback()
            print(f"REHEARSED: {expected:,} devices; {counts}; all changes rolled back.", flush=True)
    except BaseException:
        connection.rollback()
        raise
    finally:
        cursor.close()


def sampled_query(query: str, limit: int) -> str:
    if type(limit) is not int or not 1 <= limit <= 1_000_000:
        raise ValueError("Installation sample must be an integer between 1 and 1000000.")
    marker = "SELECT\n    d.[Id]"
    if query.count(marker) != 1:
        raise ValueError("Staging selector changed; review sampling.")
    # A bound TOP parameter makes ODBC create the temp table in an inner RPC scope.
    return query.strip().removesuffix(";").replace(marker, f"SELECT TOP ({limit})\n    d.[Id]") + """
    WHERE COALESCE(tank.[TankInstallDate], d.[InitialInstallDate]) IS NOT NULL
    ORDER BY COALESCE(tank.[TankInstallDate], d.[InitialInstallDate]) DESC, d.[Id] DESC;
    """


def prune_for_space(connection: pyodbc.Connection) -> int:
    cursor = connection.cursor()
    try:
        cursor.execute(f"""
            IF EXISTS (SELECT 1 FROM sys.foreign_keys
                WHERE referenced_object_id=OBJECT_ID('dbo.Insight Project'))
                THROW 51303, 'Target is referenced by foreign keys.', 1;
            IF EXISTS (SELECT 1 FROM sys.triggers
                WHERE parent_id=OBJECT_ID('dbo.Insight Project'))
                THROW 51304, 'Target has triggers; manual review required.', 1;
            DELETE target FROM {TARGET} target
            WHERE NOT EXISTS (SELECT 1 FROM #DeviceStage source WHERE source.Id=target.Id);
            SELECT CONVERT(bigint, @@ROWCOUNT);
        """)
        removed = cursor.fetchone()[0]
        connection.commit()
        print(f"PRUNING COMMITTED: removed {removed:,} rows outside the installation sample.", flush=True)
        connection.autocommit = True
        try:
            print("Compacting target indexes to release sparse pages after pruning.", flush=True)
            cursor.execute(f"ALTER INDEX ALL ON {TARGET} REORGANIZE;")
        finally:
            connection.autocommit = False
        return removed
    except BaseException:
        connection.rollback()
        raise
    finally:
        cursor.close()


def refresh(
    *, apply: bool, sample_installations: int | None = None,
    prune_first: bool = False, promotion_batch_size: int | None = None,
) -> None:
    if sample_installations is not None and not 1 <= sample_installations <= 1_000_000:
        raise ValueError("Installation sample must be between 1 and 1000000.")
    if prune_first and (not apply or sample_installations is None):
        raise ValueError("Committed pruning requires --apply and --sample-installations.")
    if promotion_batch_size is not None and (not apply or not 1 <= promotion_batch_size <= 50_000):
        raise ValueError("Batched promotion requires --apply and a batch size between 1 and 50000.")
    query = Path(__file__).with_name("stage_azure_devices.sql").read_text(encoding="utf-8")
    if sample_installations is not None:
        query = sampled_query(query, sample_installations)
    connection = open_connection()
    started = time.monotonic()
    try:
        connection.timeout = 1800
        cursor = connection.cursor()
        cursor.execute("SET NOCOUNT ON; SET XACT_ABORT ON; SET LOCK_TIMEOUT 30000;")
        if cursor.execute("SELECT DB_NAME();").fetchone()[0] != TARGET_DATABASE:
            raise ValueError("Unexpected database.")
        cursor.execute("""
            DECLARE @result int;
            EXEC @result=sys.sp_getapplock @Resource=N'InsightProjectMigrations',
                @LockMode=N'Exclusive', @LockOwner=N'Session', @LockTimeout=5000;
            IF @result<0 THROW 51300, 'Another refresh or migration is running.', 1;
        """)
        history = list(cursor.execute("SELECT Version,Name,Checksum FROM dbo.InsightProjectMigrations ORDER BY Version;"))
        cursor.execute("SELECT Id INTO #DeviceIds FROM dbo.Devices; ALTER TABLE #DeviceIds ADD PRIMARY KEY (Id);")
        expected = cursor.execute("SELECT COUNT_BIG(*) FROM #DeviceIds;").fetchone()[0]
        if expected < 1:
            raise ValueError("Source is empty; refusing to refresh.")
        connection.commit()
        scope = (
            f"the newest {sample_installations:,} installations from {expected:,} source devices"
            if sample_installations is not None else f"{expected:,} devices"
        )
        print(f"Staging {scope} with set-based aggregates; target unchanged.", flush=True)
        cursor.execute(query)
        cursor.execute("ALTER TABLE #DeviceStage ADD PRIMARY KEY (Id);")
        actual = cursor.execute("SELECT COUNT_BIG(*) FROM #DeviceStage;").fetchone()[0]
        if sample_installations is not None:
            expected = sample_installations
        if actual != expected:
            raise ValueError("Staged count differs from captured device IDs; target unchanged.")
        if list(cursor.execute("SELECT Version,Name,Checksum FROM dbo.InsightProjectMigrations ORDER BY Version;")) != history:
            raise ValueError("Migration history changed during staging; refusing promotion.")
        row = cursor.execute("""
            SELECT SUM(CASE WHEN PurchaseDate IS NOT NULL THEN 1 ELSE 0 END),
                SUM(CASE WHEN CompanyId IS NOT NULL THEN 1 ELSE 0 END),
                SUM(CASE WHEN TankInstallDate IS NOT NULL THEN 1 ELSE 0 END)
            FROM #DeviceStage;
        """).fetchone()
        print(f"Staging verified: purchase proxies={row[0]:,}, companies={row[1]:,}, tank dates={row[2]:,}.", flush=True)
        # Compare independently expressed preview rules with the optimized staging query.
        cursor.execute("SELECT TOP (1000) Id INTO #BatchDevices FROM #DeviceStage ORDER BY Id;")
        preview = stage_query(Path(__file__).with_name("preview_device_flat_table.sql").read_text(encoding="utf-8"))
        cursor.execute(f"SELECT * INTO #PreviewCheck FROM ({preview}) source;")
        cursor.execute("""
            SELECT COUNT_BIG(*) FROM (
                SELECT stage.* FROM #DeviceStage stage JOIN #BatchDevices sample ON sample.Id=stage.Id
                EXCEPT SELECT * FROM #PreviewCheck
            ) differences;
        """)
        if cursor.fetchone()[0]:
            raise ValueError("Optimized staging differs from preview; target unchanged.")
        cursor.close()
        connection.commit()
        if prune_first:
            prune_for_space(connection)
            # Deleted-page cleanup is asynchronous after the explicitly committed pruning.
            for attempt in range(30):
                space_cursor = connection.cursor()
                free_mb = space_cursor.execute("""
                    SELECT (CONVERT(bigint,DATABASEPROPERTYEX(DB_NAME(),'MaxSizeInBytes')) -
                        SUM(CONVERT(bigint,FILEPROPERTY(name,'SpaceUsed')))*8192)/1048576
                    FROM sys.database_files WHERE type=0;
                """).fetchone()[0]
                space_cursor.close()
                connection.commit()
                print(f"Available database capacity after pruning: {free_mb:,} MB.", flush=True)
                if free_mb >= 400:
                    break
                if attempt == 29:
                    raise ValueError("Pruning committed, but capacity remains below 400 MB; enrichment not attempted.")
                time.sleep(10)
        promote(
            connection, expected, apply, sample_installations=sample_installations,
            batch_size=promotion_batch_size,
        )
        print(f"Finished in {time.monotonic()-started:.0f}s.", flush=True)
    finally:
        connection.rollback()
        connection.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Commit after staging and validation (otherwise rehearse).")
    parser.add_argument("--sample-installations", type=int, help="Keep only the most recent N installations (maximum 1000000).")
    parser.add_argument("--prune-first", action="store_true", help="Explicitly commit sample pruning before enrichment to release storage.")
    parser.add_argument("--promotion-batch-size", type=int, help="Explicitly commit schema and this many enrichment rows per batch.")
    args = parser.parse_args()
    try:
        refresh(
            apply=args.apply, sample_installations=args.sample_installations,
            prune_first=args.prune_first, promotion_batch_size=args.promotion_batch_size,
        )
    except pyodbc.Error as error:
        state = error.args[0] if error.args else "unknown"
        detail = str(error)
        password = dotenv_values(ROOT / ".env").get("IQ_DB_PASSWORD")
        if password:
            detail = detail.replace(password, "[redacted]")
        raise SystemExit(f"Azure refresh failed (SQLSTATE {state}): {detail}") from None
