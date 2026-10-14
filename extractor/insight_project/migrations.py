"""Transactional, ordered SQL migrations; no extraction policy lives here."""

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import re

import pyodbc


TARGET_DATABASE = "Neevo_InsightQuest2026_D"
MIGRATION_DIRECTORY = Path(__file__).parent / "sql"

BOOTSTRAP_SQL = """
IF OBJECT_ID(N'dbo.InsightProjectMigrations', N'U') IS NULL
BEGIN
    CREATE TABLE [dbo].[InsightProjectMigrations] (
        [Version] INT NOT NULL PRIMARY KEY,
        [Name] NVARCHAR(255) NOT NULL,
        [Checksum] CHAR(64) NOT NULL,
        [AppliedAtUtc] DATETIME2(7) NOT NULL DEFAULT SYSUTCDATETIME()
    );
END;
"""

LOCK_SQL = """
IF @@TRANCOUNT = 0 BEGIN TRANSACTION;
DECLARE @result INT;
EXEC @result = sys.sp_getapplock
    @Resource = N'InsightProjectMigrations',
    @LockMode = N'Exclusive',
    @LockOwner = N'Transaction',
    @LockTimeout = 10000;
IF @result < 0
    THROW 51003, 'Could not acquire the Insight Project migration lock.', 1;
"""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str
    checksum: str


def load_migrations(directory: Path = MIGRATION_DIRECTORY) -> list[Migration]:
    migrations = []
    versions = set()
    for path in sorted(directory.glob("*.sql")):
        match = re.fullmatch(r"(\d{4})_[a-z0-9_]+\.sql", path.name)
        if not match:
            raise ValueError(f"Invalid migration filename: {path.name}")
        version = int(match.group(1))
        if version < 1 or version in versions:
            raise ValueError(f"Invalid or duplicate migration version: {version}")
        sql = path.read_text(encoding="utf-8-sig")
        if not sql.strip() or re.search(r"^\s*GO(?:\s|$)", sql, re.MULTILINE | re.IGNORECASE):
            raise ValueError(f"Migration must be nonempty SQL without GO batches: {path.name}")
        versions.add(version)
        migrations.append(
            Migration(version, path.name, sql, sha256(sql.encode("utf-8")).hexdigest())
        )
    if not migrations:
        raise ValueError(f"No migrations found in {directory}")
    return migrations


def pending_migrations(
    migrations: list[Migration], applied: dict[int, tuple[str, str]]
) -> list[Migration]:
    known_versions = {migration.version for migration in migrations}
    if applied.keys() - known_versions:
        raise ValueError("Database contains migration versions missing from this project.")
    pending = []
    for migration in migrations:
        previous = applied.get(migration.version)
        if previous is None:
            pending.append(migration)
        elif previous != (migration.name, migration.checksum):
            raise ValueError(f"Applied migration was modified: {migration.name}")
        elif pending:
            raise ValueError("Migration history has a gap; refusing out-of-order execution.")
    return pending


def migrate(
    connection: pyodbc.Connection,
    *,
    apply: bool = False,
    directory: Path = MIGRATION_DIRECTORY,
) -> list[str]:
    """Apply all pending migrations atomically, or rehearse and roll back."""
    if connection.autocommit:
        raise ValueError("Migrations require a dedicated connection with autocommit=False.")
    migrations = load_migrations(directory)
    cursor = connection.cursor()
    try:
        database = cursor.execute("SELECT DB_NAME();").fetchone()[0]
        if database != TARGET_DATABASE:
            raise ValueError(f"Expected database {TARGET_DATABASE}; connected to {database}.")
        cursor.execute("SET XACT_ABORT ON;")
        cursor.execute(LOCK_SQL)
        cursor.execute(BOOTSTRAP_SQL)
        rows = cursor.execute(
            "SELECT [Version], [Name], [Checksum] FROM [dbo].[InsightProjectMigrations] "
            "ORDER BY [Version];"
        ).fetchall()
        applied = {row[0]: (row[1], row[2]) for row in rows}
        pending = pending_migrations(migrations, applied)
        for migration in pending:
            cursor.execute(migration.sql)
            cursor.execute(
                "INSERT INTO [dbo].[InsightProjectMigrations] "
                "([Version], [Name], [Checksum]) VALUES (?, ?, ?);",
                migration.version, migration.name, migration.checksum,
            )
        if apply:
            connection.commit()
        else:
            connection.rollback()
        return [migration.name for migration in pending]
    except BaseException:
        connection.rollback()
        raise
    finally:
        cursor.close()
