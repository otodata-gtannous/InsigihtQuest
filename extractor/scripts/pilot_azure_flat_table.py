"""Read Azure source rows into a temporary pilot table; always roll back."""

from pathlib import Path

from dotenv import dotenv_values
import pyodbc


ROOT = Path(__file__).resolve().parents[2]
TARGET_DATABASE = "Neevo_InsightQuest2026_D"
TARGET_SERVER = "sql-neevo-insight-quest2026.database.windows.net"


def open_connection() -> pyodbc.Connection:
    settings = dotenv_values(ROOT / ".env")
    if settings.get("IQ_DB_NAME") != TARGET_DATABASE:
        raise ValueError("The .env database must be the approved Azure target.")
    server = settings.get("IQ_DB_SERVER", "")
    if server.casefold() != TARGET_SERVER:
        raise ValueError("The .env server must be the approved Azure server.")

    def quoted(value: str) -> str:
        return "{" + value.replace("}", "}}") + "}"

    user = settings.get("IQ_DB_USER")
    password = settings.get("IQ_DB_PASSWORD")
    if not user or not password:
        raise ValueError("Set IQ_DB_USER and IQ_DB_PASSWORD in the existing .env.")
    connection_string = ";".join([
        "DRIVER=" + quoted(settings.get("IQ_DB_DRIVER") or "ODBC Driver 18 for SQL Server"),
        "SERVER=" + quoted(server + "," + (settings.get("IQ_DB_PORT") or "1433")),
        "DATABASE=" + quoted(TARGET_DATABASE),
        "UID=" + quoted(user),
        "PWD=" + quoted(password),
        "Encrypt=yes",
        "TrustServerCertificate=no",
    ]) + ";"
    return pyodbc.connect(connection_string, autocommit=False, timeout=30)


def main() -> None:
    query = Path(__file__).with_name("preview_device_flat_table.sql").read_text(encoding="utf-8")
    source = "FROM [dbo].[Devices] d"
    if query.count(source) != 1:
        raise ValueError("Preview query source changed; review pilot sampling.")
    query = query.replace(
        source, "FROM (SELECT TOP (100) * FROM [dbo].[Devices] ORDER BY [Id]) d"
    ).strip().removesuffix(";")
    connection = open_connection()
    try:
        connection.timeout = 120
        cursor = connection.cursor()
        cursor.execute("SET XACT_ABORT ON; SET LOCK_TIMEOUT 5000;")
        if cursor.execute("SELECT DB_NAME();").fetchone()[0] != TARGET_DATABASE:
            raise ValueError("Unexpected connected database.")
        # Only tempdb is written: no locks from altering the four-million-row target.
        cursor.execute("SELECT * INTO #AzureDevicePilot FROM (" + query + ") source;")
        cursor.execute("ALTER TABLE #AzureDevicePilot ADD PRIMARY KEY ([Id]);")
        row = cursor.execute("""
            SELECT COUNT_BIG(*), COUNT_BIG(DISTINCT [Id]),
                SUM(CASE WHEN [PurchaseDate] IS NOT NULL THEN 1 ELSE 0 END),
                SUM(CASE WHEN [CompanyId] IS NOT NULL THEN 1 ELSE 0 END),
                SUM(CASE WHEN [TankInstallDate] IS NOT NULL THEN 1 ELSE 0 END)
            FROM #AzureDevicePilot;
        """).fetchone()
        if row is None or row[0] != 100 or row[1] != 100:
            raise ValueError("Pilot must return exactly 100 distinct devices.")
        print(
            f"Pilot: {row[0]} rows, {row[1]} unique devices, "
            f"{row[2]} purchase proxies, {row[3]} companies, {row[4]} tank dates."
        )
        cursor.close()
    finally:
        connection.rollback()
        connection.close()
    print("Pilot rolled back. Azure target and migration history were not changed.")


if __name__ == "__main__":
    try:
        main()
    except pyodbc.Error as error:
        state = error.args[0] if error.args else "unknown"
        raise SystemExit(f"Azure pilot failed (SQLSTATE {state}); no success reported.") from None
