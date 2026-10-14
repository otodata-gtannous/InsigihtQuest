import argparse
from getpass import getpass
import os
import sys

import pyodbc

from insight_project.migrations import TARGET_DATABASE, migrate
from insight_project.extraction import extract_device_locations


def get_connection_string() -> str:
    configured = os.environ.get("INSIGHT_PROJECT_CONNECTION_STRING")
    if configured and configured.strip():
        return configured
    if not sys.stdin.isatty():
        raise ValueError(
            "Set INSIGHT_PROJECT_CONNECTION_STRING or launch in an interactive terminal."
        )
    username = input("SQL username [insightquest]: ").strip() or "insightquest"
    password = getpass("SQL password (not saved): ")
    if not password:
        raise ValueError("SQL password cannot be empty.")
    # ODBC braced values escape closing braces and safely contain semicolons.
    escaped_username = username.replace("}", "}}")
    escaped_password = password.replace("}", "}}")
    return (
        "DRIVER={ODBC Driver 18 for SQL Server};"
        "SERVER=sql-neevo-insight-quest2026.database.windows.net,1433;"
        f"DATABASE={TARGET_DATABASE};"
        f"UID={{{escaped_username}}};PWD={{{escaped_password}}};"
        "Encrypt=yes;TrustServerCertificate=no;"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Insight Project data extraction.")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser(
        "run", help="Apply migrations and insert missing device locations (default)."
    )
    migration_parser = commands.add_parser(
        "migrate", help=f"Run migrations against {TARGET_DATABASE}; roll back by default."
    )
    migration_parser.add_argument(
        "--apply", action="store_true", help="Persist migrations instead of rolling back."
    )
    args = parser.parse_args()
    connection = None
    try:
        connection_string = get_connection_string()
        connection = pyodbc.connect(connection_string, autocommit=False, timeout=30)
        running = args.command in (None, "run")
        names = migrate(connection, apply=running or args.apply)
        for name in names:
            print(f"  {name}")
        if running:
            result = extract_device_locations(connection, apply=True)
            print(
                f"Extraction committed in {TARGET_DATABASE}: "
                f"{result.inserted_rows} inserted, {result.total_rows} total rows "
                f"({result.before_rows} before)."
            )
        else:
            mode = "Applied" if args.apply else "Rehearsed and rolled back"
            print(f"{mode} {len(names)} migration(s) in {TARGET_DATABASE}.")
    except (pyodbc.Error, ValueError, OSError, EOFError) as error:
        # Driver errors may contain connection details; never print credentials.
        if isinstance(error, pyodbc.Error):
            state = str(error.args[0]) if error.args else "unknown"
            print(f"Database operation failed (SQLSTATE {state}). No success reported.", file=sys.stderr)
        else:
            print(f"Project run failed: {error}", file=sys.stderr)
        return 1
    finally:
        if connection is not None:
            connection.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
