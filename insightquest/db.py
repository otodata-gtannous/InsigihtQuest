from __future__ import annotations

from typing import Protocol
from urllib.parse import quote_plus

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine

from insightquest.config import AppConfig


class _QueryConnection(Protocol):
    timeout: int


def create_db_engine(config: AppConfig, *, query_timeout_seconds: int | None = None) -> Engine:
    if query_timeout_seconds is not None and query_timeout_seconds < 1:
        raise ValueError("Query timeout must be positive.")
    auth_mode = (config.db_auth_mode or "auto").strip().lower()
    if auth_mode not in {"auto", "sql", "windows"}:
        raise ValueError("IQ_DB_AUTH_MODE must be one of: auto, sql, windows.")

    connection_parts = [
        f"DRIVER={{{config.db_driver}}}",
        f"SERVER={config.db_server},{config.db_port}",
        f"DATABASE={config.db_name}",
        "Encrypt=yes",
        f"TrustServerCertificate={'yes' if config.db_trust_cert else 'no'}",
    ]

    use_windows_auth = auth_mode == "windows" or (
        auth_mode == "auto" and not config.db_user.strip()
    )

    if use_windows_auth:
        connection_parts.append("Trusted_Connection=yes")
    else:
        if not config.db_user.strip():
            raise ValueError("IQ_DB_USER is required when using SQL authentication.")
        if not config.db_password:
            raise ValueError(
                "IQ_DB_PASSWORD is required for SQL authentication. "
                "Set IQ_DB_AUTH_MODE=windows to use integrated auth."
            )
        connection_parts.extend([f"UID={config.db_user}", f"PWD={config.db_password}"])

    raw_connection = ";".join(connection_parts) + ";"
    engine = create_engine(
        f"mssql+pyodbc:///?odbc_connect={quote_plus(raw_connection)}",
        pool_pre_ping=True,
    )
    if query_timeout_seconds is not None:
        @event.listens_for(engine, "connect")
        def set_query_timeout(
            connection: _QueryConnection, connection_record: object,
        ) -> None:
            connection.timeout = query_timeout_seconds
    return engine
