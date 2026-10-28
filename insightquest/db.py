from __future__ import annotations

import logging
from typing import Protocol
from urllib.parse import quote_plus

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import URL, Connection, Engine

from insightquest.config import AppConfig

logger = logging.getLogger(__name__)

POOL_RECYCLE_SECONDS = 1800
POOL_MAX_OVERFLOW = 5
_ODBC_SPECIAL = set(";{}=")


class _QueryConnection(Protocol):
    timeout: int


def _odbc_value(value: str) -> str:
    """Brace-escape an ODBC connection-string value when it holds delimiters."""
    if _ODBC_SPECIAL.intersection(value) or value != value.strip():
        return "{" + value.replace("}", "}}") + "}"
    return value


def build_odbc_connection_string(config: AppConfig) -> str:
    """Build the raw ODBC connection string. Contains the password; never log it."""
    auth_mode = (config.db_auth_mode or "auto").strip().lower()
    if auth_mode not in {"auto", "sql", "windows"}:
        raise ValueError("IQ_DB_AUTH_MODE must be one of: auto, sql, windows.")

    driver = config.db_driver.replace("}", "}}")
    parts = [
        f"DRIVER={{{driver}}}",
        f"SERVER={_odbc_value(f'{config.db_server},{config.db_port}')}",
        f"DATABASE={_odbc_value(config.db_name)}",
        "Encrypt=yes",
        f"TrustServerCertificate={'yes' if config.db_trust_cert else 'no'}",
    ]

    use_windows_auth = auth_mode == "windows" or (
        auth_mode == "auto" and not config.db_user.strip()
    )
    if use_windows_auth:
        parts.append("Trusted_Connection=yes")
    else:
        if not config.db_user.strip():
            raise ValueError("IQ_DB_USER is required when using SQL authentication.")
        if not config.db_password:
            raise ValueError(
                "IQ_DB_PASSWORD is required for SQL authentication. "
                "Set IQ_DB_AUTH_MODE=windows to use integrated auth."
            )
        parts.append(f"UID={_odbc_value(config.db_user)}")
        parts.append(f"PWD={_odbc_value(config.db_password)}")
    return ";".join(parts) + ";"


def build_engine_url(config: AppConfig) -> URL:
    # The pyodbc dialect unquotes odbc_connect itself, so the value stays quoted here.
    return URL.create(
        "mssql+pyodbc",
        query={"odbc_connect": quote_plus(build_odbc_connection_string(config))},
    )


def create_db_engine(config: AppConfig, *, query_timeout_seconds: int | None = None) -> Engine:
    """Create the pooled engine. Dashboard queries default to IQ_DB_QUERY_TIMEOUT_SECONDS."""
    if query_timeout_seconds is None:
        query_timeout_seconds = config.db_query_timeout_seconds
    if query_timeout_seconds < 1:
        raise ValueError("Query timeout must be positive.")
    engine = create_engine(
        build_engine_url(config),
        pool_size=config.db_pool_size,
        max_overflow=POOL_MAX_OVERFLOW,
        pool_timeout=config.db_pool_timeout_seconds,
        pool_recycle=POOL_RECYCLE_SECONDS,
        pool_pre_ping=True,
    )

    @event.listens_for(engine, "connect")
    def set_query_timeout(
        connection: _QueryConnection, connection_record: object,
    ) -> None:
        connection.timeout = query_timeout_seconds

    return engine


def check_db_read_only(engine: Engine, source_table: str | None = None) -> bool | None:
    """Best-effort check that the login cannot write to the source table.

    Returns True when INSERT, UPDATE and DELETE are all denied, False when any is
    granted, and None when it cannot be determined. Never raises.
    """
    from insightquest.data import _quote_table_name

    try:
        if source_table is None:
            from insightquest.config import get_config

            source_table = get_config().source_table
        object_name = _quote_table_name(source_table)
    except Exception:
        logger.warning("Could not resolve the source table for the read-only check.")
        return None

    try:
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT HAS_PERMS_BY_NAME(:obj, 'OBJECT', 'INSERT'), "
                    "HAS_PERMS_BY_NAME(:obj, 'OBJECT', 'UPDATE'), "
                    "HAS_PERMS_BY_NAME(:obj, 'OBJECT', 'DELETE')"
                ),
                {"obj": object_name},
            ).one()
            values = list(row)
            if any(value is None for value in values):
                values = _permissions_from_function(connection, object_name)
    except Exception as error:
        logger.warning("Read-only permission check failed: %s", type(error).__name__)
        return None

    if values is None:
        return None
    return not any(values)


def _permissions_from_function(connection: Connection, object_name: str) -> list[int] | None:
    rows = connection.execute(
        text("SELECT permission_name FROM sys.fn_my_permissions(:obj, 'OBJECT')"),
        {"obj": object_name},
    ).fetchall()
    if not rows:
        return None
    granted = {str(row[0]).upper() for row in rows}
    return [int(name in granted) for name in ("INSERT", "UPDATE", "DELETE")]
