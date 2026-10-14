"""Initial device-location extraction; source tables are read-only."""

from dataclasses import dataclass

import pyodbc

from insight_project.migrations import LOCK_SQL, TARGET_DATABASE


EXTRACT_SQL = """
SET NOCOUNT ON;
DECLARE @before BIGINT = (SELECT COUNT_BIG(*) FROM [dbo].[Insight Project]);
INSERT INTO [dbo].[Insight Project] ([Id], [DeviceLong], [DeviceLat])
SELECT source.[Id], source.[Longitude], source.[Latitude]
FROM (
    SELECT TOP (1000) [Id], [Longitude], [Latitude]
    FROM [dbo].[DeviceLocations]
    ORDER BY [Id]
) source
WHERE NOT EXISTS (
    SELECT 1 FROM [dbo].[Insight Project] target
    WHERE target.[Id] = source.[Id]
);
DECLARE @inserted BIGINT = ROWCOUNT_BIG();
SELECT @before AS BeforeRows, @inserted AS InsertedRows,
    (SELECT COUNT_BIG(*) FROM [dbo].[Insight Project]) AS TotalRows;
"""


@dataclass(frozen=True)
class ExtractionResult:
    before_rows: int
    inserted_rows: int
    total_rows: int


def extract_device_locations(
    connection: pyodbc.Connection, *, apply: bool = False
) -> ExtractionResult:
    """Insert missing location GUIDs atomically; roll back unless explicitly applied."""
    if connection.autocommit:
        raise ValueError("Extraction requires a dedicated connection with autocommit=False.")
    cursor = connection.cursor()
    try:
        database = cursor.execute("SELECT DB_NAME();").fetchone()[0]
        if database != TARGET_DATABASE:
            raise ValueError(f"Expected database {TARGET_DATABASE}; connected to {database}.")
        cursor.execute("SET XACT_ABORT ON;")
        # Share the migration lock to serialize schema changes and extraction runs.
        cursor.execute(LOCK_SQL)
        row = cursor.execute(EXTRACT_SQL).fetchone()
        if row is None:
            raise ValueError("Extraction did not return verification counts.")
        result = ExtractionResult(int(row[0]), int(row[1]), int(row[2]))
        if result.total_rows != result.before_rows + result.inserted_rows:
            raise ValueError("Extraction row counts do not reconcile.")
        if apply:
            connection.commit()
        else:
            connection.rollback()
        return result
    except BaseException:
        connection.rollback()
        raise
    finally:
        cursor.close()
