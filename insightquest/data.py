from __future__ import annotations

import math
import re
import threading
import time as _time
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import NoSuchTableError

from insightquest.config import AppConfig, resolve_market_csv_path

_COORDINATE_REGEX = re.compile(
    r"^\s*(-?\d+(?:\.\d+)?)\s*[,;]\s*(-?\d+(?:\.\d+)?)\s*$"
)
# Used with fullmatch only; ']' (and any quoting/control character) is never admitted.
_SAFE_NAME = re.compile(r"[A-Za-z0-9_ ]+")


def _quote_name(name: str) -> str:
    trimmed = (name or "").strip()
    if not trimmed:
        raise ValueError("Empty SQL identifier is not allowed.")
    if not _SAFE_NAME.fullmatch(trimmed):
        raise ValueError(f"Unsafe SQL identifier: '{name}'")
    return f"[{trimmed}]"


def _quote_table_name(table_name: str) -> str:
    parts = [part.strip() for part in table_name.split(".") if part.strip()]
    if len(parts) == 1:
        return _quote_name(parts[0])
    if len(parts) == 2:
        return f"{_quote_name(parts[0])}.{_quote_name(parts[1])}"
    raise ValueError(f"Invalid table name format: '{table_name}'")


def _to_datetime_bounds(from_date: date, to_date: date) -> tuple[datetime, datetime]:
    start_dt = datetime.combine(from_date, time.min)
    end_dt = datetime.combine(to_date, time.max)
    return start_dt, end_dt


def _ensure_flat_mode(config: AppConfig) -> None:
    if config.source_mode != "flat-table":
        raise ValueError(
            "IQ_SOURCE_MODE must be 'flat-table'. "
            "This app is configured to use the consolidated flat table for all datasets."
        )


def _require_engine(engine: Engine | None) -> Engine:
    if engine is None:
        raise ValueError("A database engine is required when demo mode is disabled.")
    return engine


_DEMO_CSV_CACHE: dict[tuple[str, int], pd.DataFrame] = {}
_COLUMN_CACHE_TTL_SECONDS = 300.0
_COLUMN_CACHE: dict[tuple[str, int, str, str], tuple[float, frozenset[str]]] = {}
_COLUMN_CACHE_LOCK = threading.Lock()


def clear_column_cache() -> None:
    """Forget cached schema lookups and demo CSV contents."""
    with _COLUMN_CACHE_LOCK:
        _COLUMN_CACHE.clear()
        _DEMO_CSV_CACHE.clear()


def _read_demo_csv(config: AppConfig) -> pd.DataFrame:
    path = config.demo_csv_path
    if not path.is_file():
        raise FileNotFoundError(
            f"Demo CSV '{path}' does not exist. "
            "Set IQ_DEMO_CSV to a valid CSV path or disable IQ_DEMO_MODE."
        )
    key = (str(path), path.stat().st_mtime_ns)
    with _COLUMN_CACHE_LOCK:
        cached = _DEMO_CSV_CACHE.get(key)
        if cached is None:
            _DEMO_CSV_CACHE.clear()
            cached = _DEMO_CSV_CACHE[key] = pd.read_csv(path)
    return cached.copy()


def _split_table_name(table_name: str) -> tuple[str, str]:
    parts = [part.strip() for part in table_name.split(".") if part.strip()]
    if len(parts) == 1:
        return "dbo", parts[0]
    if len(parts) == 2:
        return parts[0], parts[1]
    raise ValueError(f"Invalid table name format: '{table_name}'")


def fetch_flat_table_columns(engine: Engine | None, config: AppConfig) -> set[str]:
    """Return column names from the configured flat table without reading rows.

    Database lookups are cached in-process for a few minutes (one catalog round trip
    instead of several per query); call clear_column_cache() to force a refresh.
    """
    _ensure_flat_mode(config)
    if config.demo_mode:
        return set(_read_demo_csv(config).columns)
    if engine is None:
        raise ValueError("A database engine is required when demo mode is disabled.")
    schema, table = _split_table_name(config.source_table)
    key = (config.db_server, config.db_port, config.db_name, f"{schema}.{table}".casefold())
    now = _time.monotonic()
    with _COLUMN_CACHE_LOCK:
        cached = _COLUMN_CACHE.get(key)
        if cached and cached[0] > now:
            return set(cached[1])

    try:
        columns = frozenset(
            column["name"] for column in inspect(engine).get_columns(table, schema=schema)
        )
    except NoSuchTableError:
        columns = frozenset()
    if not columns:
        raise ValueError(f"Configured flat table '{schema}.{table}' does not exist or is not visible.")
    with _COLUMN_CACHE_LOCK:
        _COLUMN_CACHE[key] = (now + _COLUMN_CACHE_TTL_SECONDS, columns)
    return set(columns)


def fetch_flat_table_rows(
    engine: Engine | None,
    config: AppConfig,
    fields: dict[str, str],
    date_field: str | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
    limit: int = 100_000,
) -> pd.DataFrame:
    """Read selected mapped columns from the configured flat table."""
    _ensure_flat_mode(config)
    if not fields:
        raise ValueError("At least one flat-table field mapping is required.")
    if limit < 1:
        raise ValueError("Row limit must be greater than zero.")
    if (from_date is None) != (to_date is None):
        raise ValueError("Both date bounds must be provided together.")
    if from_date is not None and from_date > to_date:
        raise ValueError("From date must be earlier than or equal to To date.")

    if config.demo_mode:
        frame = _read_demo_csv(config)
        column_names = {name.casefold(): name for name in frame.columns}
        configured = dict(fields)
        if date_field:
            configured["filter_date"] = date_field

        selected = {}
        for alias, attribute in configured.items():
            column_name = getattr(config, attribute, "")
            if not column_name:
                raise ValueError(f"No column is configured for '{attribute}'.")
            actual_name = column_names.get(column_name.casefold())
            if actual_name is None:
                raise ValueError(
                    f"Configured demo column '{column_name}' for '{attribute}' "
                    f"was not found in '{config.demo_csv_path.name}'."
                )
            if alias != "filter_date":
                selected[alias] = actual_name

        if date_field:
            date_column = column_names[getattr(config, date_field).casefold()]
            dates = pd.to_datetime(frame[date_column], errors="coerce")
            frame = frame.loc[dates.notna()].copy()
            frame[date_column] = dates.loc[dates.notna()]
            if from_date is not None and to_date is not None:
                start = pd.Timestamp(from_date)
                end = pd.Timestamp(to_date + timedelta(days=1))
                frame = frame.loc[
                    (frame[date_column] >= start) & (frame[date_column] < end)
                ]
            frame = frame.sort_values(date_column, ascending=False)

        return frame[list(selected.values())].rename(
            columns={actual: alias for alias, actual in selected.items()}
        ).head(limit).reset_index(drop=True)

    available = {name.casefold() for name in fetch_flat_table_columns(engine, config)}
    configured = dict(fields)
    if date_field:
        configured["filter_date"] = date_field

    selected_columns: list[str] = []
    for alias, attribute in configured.items():
        column_name = getattr(config, attribute, "")
        if not column_name:
            raise ValueError(f"No column is configured for '{attribute}'.")
        if column_name.casefold() not in available:
            raise ValueError(
                f"Configured column '{column_name}' for '{attribute}' was not found "
                f"in flat table '{config.source_table}'."
            )
        if alias != "filter_date":
            selected_columns.append(f"{_quote_name(column_name)} AS {_quote_name(alias)}")

    table_name = _quote_table_name(config.source_table)
    where_clause = ""
    params: dict[str, object] = {"limit": limit}
    if date_field:
        date_column = _quote_name(getattr(config, date_field))
        if from_date is not None and to_date is not None:
            where_clause = (
                f"WHERE {date_column} >= :start_dt "
                f"AND {date_column} < :end_exclusive"
            )
            params["start_dt"] = datetime.combine(from_date, time.min)
            params["end_exclusive"] = datetime.combine(
                to_date + timedelta(days=1), time.min
            )
        else:
            where_clause = f"WHERE {date_column} IS NOT NULL"
        order_clause = f"ORDER BY {date_column} DESC"
    else:
        order_clause = ""

    stmt = text(
        f"SELECT TOP (:limit) {', '.join(selected_columns)} "
        f"FROM {table_name} {where_clause} {order_clause}"
    )
    engine = _require_engine(engine)
    with engine.connect() as connection:
        return pd.read_sql(stmt, con=connection, params=params)


def _parse_location_as_coordinates(df: pd.DataFrame) -> pd.DataFrame:
    if "device_location" not in df.columns:
        return df

    if "latitude" not in df.columns:
        df["latitude"] = pd.NA
    if "longitude" not in df.columns:
        df["longitude"] = pd.NA

    missing = df["latitude"].isna() | df["longitude"].isna()
    if not missing.any():
        return df

    parsed = df.loc[missing, "device_location"].astype(str).str.extract(_COORDINATE_REGEX)
    df.loc[missing, "latitude"] = pd.to_numeric(parsed[0], errors="coerce")
    df.loc[missing, "longitude"] = pd.to_numeric(parsed[1], errors="coerce")
    return df


def _build_flat_map_query(config: AppConfig) -> str:
    table_name = _quote_table_name(config.source_table)
    col_device = _quote_name(config.col_device)
    col_customer = _quote_name(config.col_customer)
    location_select = (
        f"{_quote_name(config.col_location)} AS device_location"
        if config.col_location.strip()
        else "CAST(NULL AS nvarchar(255)) AS device_location"
    )
    col_installed_at = _quote_name(config.col_installed_at)
    col_latitude = _quote_name(config.col_latitude)
    col_longitude = _quote_name(config.col_longitude)

    return f"""
        SELECT TOP (:limit)
            {col_device} AS device_name,
            {col_customer} AS customer_name,
            {location_select},
            {col_installed_at} AS installed_at,
            CAST({col_latitude} AS float) AS latitude,
            CAST({col_longitude} AS float) AS longitude
        FROM {table_name}
        WHERE {col_installed_at} BETWEEN :start_dt AND :end_dt
        ORDER BY {col_installed_at} DESC
    """


def _build_flat_trend_query(config: AppConfig) -> str:
    table_name = _quote_table_name(config.source_table)
    col_customer = _quote_name(config.col_customer)
    col_installed_at = _quote_name(config.col_installed_at)

    return f"""
        SELECT TOP (:limit)
            {col_installed_at} AS installed_at,
            {col_customer} AS customer_name
        FROM {table_name}
        WHERE {col_installed_at} BETWEEN :start_dt AND :end_dt
        ORDER BY {col_installed_at} ASC
    """


def _build_flat_territory_query(config: AppConfig) -> str:
    table_name = _quote_table_name(config.source_table)
    col_installed_at = _quote_name(config.col_installed_at)
    col_territory = _quote_name(config.col_territory)

    return f"""
        SELECT TOP (:limit)
            {col_installed_at} AS installed_at,
            COALESCE(NULLIF(LTRIM(RTRIM({col_territory})), ''), 'Unknown Territory') AS territory_name
        FROM {table_name}
        WHERE {col_installed_at} BETWEEN :start_dt AND :end_dt
        ORDER BY {col_installed_at} ASC
    """


def fetch_install_map_data(
    engine: Engine | None,
    config: AppConfig,
    from_date: date,
    to_date: date,
    limit: int,
) -> pd.DataFrame:
    _ensure_flat_mode(config)
    if config.demo_mode:
        fields = {
            "device_name": "col_device",
            "customer_name": "col_customer",
            "installed_at": "col_installed_at",
            "latitude": "col_latitude",
            "longitude": "col_longitude",
        }
        if config.col_location:
            fields["device_location"] = "col_location"
        df = fetch_flat_table_rows(
            engine, config, fields, "col_installed_at", from_date, to_date, limit
        )
        if "device_location" not in df.columns:
            df["device_location"] = pd.NA
        df = _parse_location_as_coordinates(df)
        df["latitude"] = pd.to_numeric(df["latitude"], errors="coerce")
        df["longitude"] = pd.to_numeric(df["longitude"], errors="coerce")
        df = df.dropna(subset=["latitude", "longitude"]).copy()
        return df[
            df["latitude"].between(-90, 90) & df["longitude"].between(-180, 180)
        ]
    start_dt, end_dt = _to_datetime_bounds(from_date, to_date)

    stmt = text(_build_flat_map_query(config))

    engine = _require_engine(engine)
    with engine.connect() as connection:
        df = pd.read_sql(
            stmt,
            con=connection,
            params={"start_dt": start_dt, "end_dt": end_dt, "limit": limit},
        )

    if df.empty:
        return df

    df = _parse_location_as_coordinates(df)
    df["latitude"] = pd.to_numeric(df.get("latitude"), errors="coerce")
    df["longitude"] = pd.to_numeric(df.get("longitude"), errors="coerce")
    df = df.dropna(subset=["latitude", "longitude"]).copy()
    df = df[(df["latitude"].between(-90, 90)) & (df["longitude"].between(-180, 180))]
    return df


def fetch_install_trend_data(
    engine: Engine | None,
    config: AppConfig,
    from_date: date,
    to_date: date,
    limit: int,
) -> pd.DataFrame:
    _ensure_flat_mode(config)
    if config.demo_mode:
        return fetch_flat_table_rows(
            engine,
            config,
            {"installed_at": "col_installed_at", "customer_name": "col_customer"},
            "col_installed_at",
            from_date,
            to_date,
            limit,
        )
    start_dt, end_dt = _to_datetime_bounds(from_date, to_date)

    stmt = text(_build_flat_trend_query(config))

    engine = _require_engine(engine)
    with engine.connect() as connection:
        df = pd.read_sql(
            stmt,
            con=connection,
            params={"start_dt": start_dt, "end_dt": end_dt, "limit": limit},
        )

    if df.empty:
        return df

    df["installed_at"] = pd.to_datetime(df["installed_at"], errors="coerce")
    if "customer_name" not in df.columns:
        df["customer_name"] = "Unknown"
    df["customer_name"] = df["customer_name"].fillna("Unknown").astype(str)
    df = df.dropna(subset=["installed_at"]).copy()
    return df


def fetch_sales_data(
    engine: Engine | None,
    config: AppConfig,
    from_date: date,
    to_date: date,
) -> pd.DataFrame:
    _ensure_flat_mode(config)
    if config.demo_mode:
        data = fetch_flat_table_rows(
            engine,
            config,
            {"purchase_date": "col_sales_date", "sales": "col_sales_amount"},
            "col_sales_date",
            from_date,
            to_date,
        )
        data["sales"] = pd.to_numeric(data["sales"], errors="coerce").fillna(0)
        return (
            data.groupby("purchase_date", as_index=False)["sales"]
            .sum()
            .sort_values("purchase_date")
            .reset_index(drop=True)
        )
    start_dt = datetime.combine(from_date, time.min)
    end_exclusive = datetime.combine(to_date + timedelta(days=1), time.min)
    table_name = _quote_table_name(config.source_table)
    col_sales_date = _quote_name(config.col_sales_date)
    col_sales_amount = _quote_name(config.col_sales_amount)
    stmt = text(
        f"""
        SELECT
            CAST({col_sales_date} AS date) AS purchase_date,
            SUM(COALESCE({col_sales_amount}, 0)) AS sales
        FROM {table_name}
        WHERE
            {col_sales_date} >= :start_dt
            AND {col_sales_date} < :end_exclusive
        GROUP BY CAST({col_sales_date} AS date)
        ORDER BY purchase_date
        """
    )

    engine = _require_engine(engine)
    with engine.connect() as connection:
        return pd.read_sql(
            stmt,
            con=connection,
            params={"start_dt": start_dt, "end_exclusive": end_exclusive},
        )


def fetch_install_territory_trend_data(
    engine: Engine | None,
    config: AppConfig,
    from_date: date,
    to_date: date,
    limit: int,
) -> pd.DataFrame:
    _ensure_flat_mode(config)
    if config.demo_mode:
        data = fetch_flat_table_rows(
            engine,
            config,
            {"installed_at": "col_installed_at", "territory_name": "col_territory"},
            "col_installed_at",
            from_date,
            to_date,
            limit,
        )
        data["installed_at"] = pd.to_datetime(data["installed_at"], errors="coerce")
        data["territory_name"] = (
            data["territory_name"].fillna("Unknown Territory").astype(str)
        )
        return data.dropna(subset=["installed_at"]).reset_index(drop=True)
    start_dt, end_dt = _to_datetime_bounds(from_date, to_date)
    stmt = text(_build_flat_territory_query(config))

    engine = _require_engine(engine)
    with engine.connect() as connection:
        df = pd.read_sql(
            stmt,
            con=connection,
            params={"start_dt": start_dt, "end_dt": end_dt, "limit": limit},
        )

    if df.empty:
        return df

    df["installed_at"] = pd.to_datetime(df["installed_at"], errors="coerce")
    df["territory_name"] = df["territory_name"].fillna("Unknown Territory").astype(str)
    df = df.dropna(subset=["installed_at"]).copy()
    return df


def fetch_data_source_diagnostics(
    engine: Engine | None,
    config: AppConfig,
    from_date: date,
    to_date: date,
) -> pd.DataFrame:
    _ensure_flat_mode(config)
    if config.demo_mode:
        data = fetch_flat_table_rows(
            engine,
            config,
            {
                "customer_name": "col_customer",
                "installed_at": "col_installed_at",
                "latitude": "col_latitude",
                "longitude": "col_longitude",
            },
            "col_installed_at",
            from_date,
            to_date,
        )
        return pd.DataFrame(
            [
                {
                    "total_devices_in_range": len(data),
                    "with_coordinates": int(
                        (
                            data["latitude"].notna()
                            & data["longitude"].notna()
                        ).sum()
                    ),
                    "with_customer": int(data["customer_name"].notna().sum()),
                    "from_initial_install_date": len(data),
                    "from_detected_install_date": 0,
                    "from_tanksensor_created_date": 0,
                }
            ]
        )
    start_dt, end_dt = _to_datetime_bounds(from_date, to_date)
    table_name = _quote_table_name(config.source_table)
    col_customer = _quote_name(config.col_customer)
    col_installed_at = _quote_name(config.col_installed_at)
    col_latitude = _quote_name(config.col_latitude)
    col_longitude = _quote_name(config.col_longitude)
    stmt = text(
        f"""
        SELECT
            COUNT(*) AS total_devices_in_range,
            SUM(CASE WHEN {col_latitude} IS NOT NULL AND {col_longitude} IS NOT NULL THEN 1 ELSE 0 END) AS with_coordinates,
            SUM(CASE WHEN {col_customer} IS NOT NULL AND LTRIM(RTRIM(CAST({col_customer} AS nvarchar(255)))) <> '' THEN 1 ELSE 0 END) AS with_customer,
            SUM(CASE WHEN {col_installed_at} IS NOT NULL THEN 1 ELSE 0 END) AS from_initial_install_date,
            CAST(0 AS bigint) AS from_detected_install_date,
            CAST(0 AS bigint) AS from_tanksensor_created_date
        FROM {table_name}
        WHERE {col_installed_at} BETWEEN :start_dt AND :end_dt
        """
    )
    engine = _require_engine(engine)
    with engine.connect() as connection:
        return pd.read_sql(stmt, con=connection, params={"start_dt": start_dt, "end_dt": end_dt})


# ---------------------------------------------------------------------------
# Aggregate fetchers
#
# Every fetcher below aggregates inside SQL Server (parameterized, validated
# bracketed identifiers) and returns a small frame. Demo mode runs the same
# semantics through a pandas reference implementation over demo_data.csv, and
# both paths share the same normalisation/sort step so results are comparable.
# ---------------------------------------------------------------------------

EVENT_COLUMNS = {"installs": "col_installed_at", "purchases": "col_sales_date"}
GRAINS = ("day", "week", "month")
DIMENSION_COLUMNS = {
    "territory": "col_territory",
    "customer": "col_customer",
    "product_type": "col_product_type",
    "customer_size": "col_customer",
}
UNKNOWN_LABELS = {
    "territory": "Unknown Territory",
    "customer": "Unknown",
    "product_type": "Unknown Product",
    "customer_size": "Unknown",
}


def _check_choice(value: str, allowed: Iterable[str], label: str) -> str:
    allowed = tuple(allowed)
    if value not in allowed:
        raise ValueError(f"{label} must be one of: {', '.join(allowed)}.")
    return value


def _validate_range(from_date: date | None, to_date: date | None) -> None:
    if (from_date is None) != (to_date is None):
        raise ValueError("Both date bounds must be provided together.")
    if from_date is not None and from_date > to_date:
        raise ValueError("From date must be earlier than or equal to To date.")


def _range_params(from_date: date | None, to_date: date | None) -> dict[str, object]:
    if from_date is None or to_date is None:
        return {}
    return {
        "start_dt": datetime.combine(from_date, time.min),
        "end_exclusive": datetime.combine(to_date + timedelta(days=1), time.min),
    }


def _validated_edges(config: AppConfig) -> tuple[int, ...]:
    edges = tuple(config.customer_size_edges)
    if (
        not edges
        or any(not isinstance(edge, int) or edge < 1 for edge in edges)
        or any(later <= earlier for earlier, later in zip(edges, edges[1:]))
    ):
        raise ValueError("Customer size edges must be positive, strictly ascending integers.")
    return edges


def customer_size_labels(edges: tuple[int, ...]) -> tuple[str, ...]:
    """Bucket labels for ascending edges: (10, 100) -> Small (<10), Medium, Large (100+)."""
    if len(edges) == 1:
        return ("Small", "Large")
    if len(edges) == 2:
        return ("Small", "Medium", "Large")
    labels = [f"<{edges[0]}"]
    labels += [f"{low}-{high - 1}" for low, high in zip(edges, edges[1:])]
    labels.append(f"{edges[-1]}+")
    return tuple(labels)


def customer_size_order(config: AppConfig) -> tuple[str, ...]:
    """Display order of customer-size segments, including the Unknown bucket."""
    return customer_size_labels(_validated_edges(config)) + (UNKNOWN_LABELS["customer_size"],)


def _label_expr(column: str) -> str:
    return f"COALESCE(NULLIF(LTRIM(RTRIM(CAST({column} AS nvarchar(255)))), ''), :unknown_label)"


def _period_expr(grain: str, column: str) -> str:
    if grain == "day":
        return f"CAST({column} AS date)"
    if grain == "week":
        # 1900-01-01 is a Monday, so weeks start Monday regardless of DATEFIRST.
        return f"DATEADD(day, -(DATEDIFF(day, '19000101', {column}) % 7), CAST({column} AS date))"
    return f"DATEFROMPARTS(YEAR({column}), MONTH({column}), 1)"


def _base_relation(
    config: AppConfig, date_columns: dict[str, str], dimension: str | None,
) -> tuple[list[str], str, dict[str, object]]:
    """Return (CTE definitions, relation name, params) exposing aliased columns + segment."""
    table = _quote_table_name(config.source_table)
    select = [f"{_quote_name(getattr(config, attr))} AS {alias}" for alias, attr in date_columns.items()]
    params: dict[str, object] = {}
    if dimension is None:
        return [f"base AS (SELECT {', '.join(select)} FROM {table})"], "base", params

    column = _quote_name(getattr(config, DIMENSION_COLUMNS[dimension]))
    params["unknown_label"] = UNKNOWN_LABELS[dimension]
    if dimension != "customer_size":
        select.append(f"{_label_expr(column)} AS segment")
        return [f"base AS (SELECT {', '.join(select)} FROM {table})"], "base", params

    # Company size = device count per company across ALL devices (never date-filtered).
    edges = _validated_edges(config)
    labels = customer_size_labels(edges)
    select.append(f"NULLIF(LTRIM(RTRIM(CAST({column} AS nvarchar(255)))), '') AS company_key")
    branches = ["WHEN b.company_key IS NULL THEN :unknown_label"]
    for index, edge in enumerate(edges):
        params[f"edge_{index}"] = edge
        params[f"size_label_{index}"] = labels[index]
        branches.append(f"WHEN s.company_devices < :edge_{index} THEN :size_label_{index}")
    params[f"size_label_{len(edges)}"] = labels[-1]
    outer = "".join(f"b.{alias}, " for alias in date_columns)
    ctes = [
        f"base AS (SELECT {', '.join(select)} FROM {table})",
        "company_sizes AS (SELECT company_key, COUNT(*) AS company_devices "
        "FROM base WHERE company_key IS NOT NULL GROUP BY company_key)",
        f"sized AS (SELECT {outer}b.company_key, CASE {' '.join(branches)} "
        f"ELSE :size_label_{len(edges)} END AS segment "
        "FROM base b LEFT JOIN company_sizes s ON s.company_key = b.company_key)",
    ]
    return ctes, "sized", params


def _with(ctes: list[str]) -> str:
    return "WITH " + ",\n".join(ctes)


def _date_range_sql(column: str, from_date: date | None) -> str:
    if from_date is None:
        return f"{column} IS NOT NULL"
    return f"{column} >= :start_dt AND {column} < :end_exclusive"


def build_event_counts_query(
    config: AppConfig,
    event: str,
    grain: str = "month",
    group_by: str | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
) -> tuple[str, dict[str, object]]:
    """Return (SQL, params) counting installs/purchases per period [x segment]."""
    _check_choice(event, EVENT_COLUMNS, "event")
    _check_choice(grain, GRAINS, "grain")
    if group_by is not None:
        _check_choice(group_by, DIMENSION_COLUMNS, "group_by")
    _validate_range(from_date, to_date)
    ctes, relation, params = _base_relation(config, {"event_at": EVENT_COLUMNS[event]}, group_by)
    params.update(_range_params(from_date, to_date))
    segment = ", segment" if group_by else ""
    ctes.append(
        f"bucketed AS (SELECT {_period_expr(grain, 'event_at')} AS period{segment} "
        f"FROM {relation} WHERE {_date_range_sql('event_at', from_date)})"
    )
    sql = (
        f"{_with(ctes)}\n"
        f"SELECT period{segment}, COUNT(*) AS events FROM bucketed "
        f"GROUP BY period{segment} ORDER BY period{segment}"
    )
    return sql, params


def build_segment_counts_query(
    config: AppConfig,
    dimension: str,
    from_date: date | None = None,
    to_date: date | None = None,
    top_n: int | None = None,
    include_purchased: bool | None = None,
) -> tuple[str, dict[str, object]]:
    """include_purchased=None means 'when col_sales_date is mapped'; False yields purchased = 0."""
    _check_choice(dimension, DIMENSION_COLUMNS, "dimension")
    _validate_range(from_date, to_date)
    if top_n is not None and top_n < 1:
        raise ValueError("top_n must be greater than zero.")
    if include_purchased is None:
        include_purchased = bool((config.col_sales_date or "").strip())
    date_columns = {"installed_at": "col_installed_at"}
    if include_purchased:
        date_columns = {"purchased_at": "col_sales_date", **date_columns}
    ctes, relation, params = _base_relation(config, date_columns, dimension)
    params.update(_range_params(from_date, to_date))
    in_range = "{c} >= :start_dt AND {c} < :end_exclusive"
    if from_date is None:
        purchased, installed = "COUNT(purchased_at)", "COUNT(installed_at)"
    else:
        purchased = f"SUM(CASE WHEN {in_range.format(c='purchased_at')} THEN 1 ELSE 0 END)"
        installed = f"SUM(CASE WHEN {in_range.format(c='installed_at')} THEN 1 ELSE 0 END)"
    if not include_purchased:
        purchased = "CAST(0 AS int)"
    top = ""
    if top_n is not None:
        top = "TOP (:top_n) "
        params["top_n"] = top_n
    sql = (
        f"{_with(ctes)}\n"
        f"SELECT {top}segment, COUNT(*) AS devices, {purchased} AS purchased, "
        f"{installed} AS installed, "
        "SUM(CASE WHEN installed_at IS NULL THEN 1 ELSE 0 END) AS not_installed "
        f"FROM {relation} GROUP BY segment ORDER BY devices DESC, segment"
    )
    return sql, params


def build_customer_size_distribution_query(config: AppConfig) -> tuple[str, dict[str, object]]:
    ctes, relation, params = _base_relation(config, {}, "customer_size")
    sql = (
        f"{_with(ctes)}\n"
        "SELECT segment, COUNT(DISTINCT company_key) AS companies, COUNT(*) AS devices "
        f"FROM {relation} GROUP BY segment"
    )
    return sql, params


def build_shipping_lag_query(
    config: AppConfig, from_date: date | None = None, to_date: date | None = None,
) -> tuple[str, dict[str, object]]:
    _validate_range(from_date, to_date)
    ctes, relation, params = _base_relation(
        config, {"shipped_at": "col_shipped_at", "installed_at": "col_installed_at"}, None,
    )
    params.update(_range_params(from_date, to_date))
    where = "shipped_at IS NOT NULL AND installed_at IS NOT NULL"
    if from_date is not None:
        where += " AND installed_at >= :start_dt AND installed_at < :end_exclusive"
    ctes.append(
        f"lagged AS (SELECT DATEDIFF(day, shipped_at, installed_at) AS lag_days FROM {relation} WHERE {where})"
    )
    sql = (
        f"{_with(ctes)}\n"
        "SELECT lag_days, COUNT(*) AS devices FROM lagged GROUP BY lag_days ORDER BY lag_days"
    )
    return sql, params


def _validated_windows(trailing_days: Iterable[int]) -> tuple[int, ...]:
    windows = sorted(set(trailing_days))
    if not windows or any(not isinstance(days, int) or not 1 <= days <= 3650 for days in windows):
        raise ValueError("trailing_days must contain integers between 1 and 3650.")
    return tuple(windows)


def build_inventory_summary_query(
    config: AppConfig, as_of: date, trailing_days: Iterable[int] = (30, 90),
) -> tuple[str, dict[str, object]]:
    windows = _validated_windows(trailing_days)
    ctes, relation, params = _base_relation(
        config, {"installed_at": "col_installed_at"}, "product_type",
    )
    params["as_of_exclusive"] = datetime.combine(as_of + timedelta(days=1), time.min)
    trailing = []
    for index, days in enumerate(windows):
        params[f"win_start_{index}"] = datetime.combine(as_of - timedelta(days=days - 1), time.min)
        trailing.append(
            f"SUM(CASE WHEN installed_at >= :win_start_{index} AND installed_at < :as_of_exclusive "
            f"THEN 1 ELSE 0 END) AS installs_{days}d"
        )
    sql = (
        f"{_with(ctes)}\n"
        "SELECT segment AS product_type, COUNT(*) AS total_devices, "
        "SUM(CASE WHEN installed_at IS NULL THEN 1 ELSE 0 END) AS on_hand, "
        f"{', '.join(trailing)} "
        f"FROM {relation} GROUP BY segment ORDER BY segment"
    )
    return sql, params


# -- shared execution + normalisation --------------------------------------


def _read_aggregate(engine: Engine | None, sql: str, params: dict[str, object]) -> pd.DataFrame:
    engine = _require_engine(engine)
    with engine.connect() as connection:
        return pd.read_sql(text(sql), con=connection, params=params)


def _resolve_columns(
    available: Iterable[str], config: AppConfig, attributes: Iterable[str],
) -> dict[str, str]:
    lookup = {name.casefold(): name for name in available}
    resolved: dict[str, str] = {}
    for attribute in dict.fromkeys(attributes):
        configured = (getattr(config, attribute, "") or "").strip()
        if not configured:
            raise ValueError(f"No column is configured for '{attribute}'.")
        actual = lookup.get(configured.casefold())
        if actual is None:
            if config.demo_mode:
                raise ValueError(
                    f"Configured demo column '{configured}' for '{attribute}' "
                    f"was not found in '{config.demo_csv_path.name}'."
                )
            raise ValueError(
                f"Configured column '{configured}' for '{attribute}' was not found "
                f"in flat table '{config.source_table}'."
            )
        resolved[attribute] = actual
    return resolved


def _validate_db_columns(engine: Engine | None, config: AppConfig, attributes: Iterable[str]) -> None:
    _resolve_columns(fetch_flat_table_columns(engine, config), config, attributes)


def _has_column(available: Iterable[str], config: AppConfig, attribute: str) -> bool:
    configured = (getattr(config, attribute, "") or "").strip().casefold()
    return bool(configured) and configured in {name.casefold() for name in available}


def _attributes_for(dimension: str | None, *extra: str) -> list[str]:
    return ([DIMENSION_COLUMNS[dimension]] if dimension else []) + list(extra)


def _order_segments(frame: pd.DataFrame, config: AppConfig, dimension: str | None) -> pd.Series:
    if dimension == "customer_size":
        order = {label: index for index, label in enumerate(customer_size_order(config))}
        return frame["segment"].map(order).fillna(len(order))
    return frame["segment"]


def _finish_event_counts(frame: pd.DataFrame, config: AppConfig, group_by: str | None) -> pd.DataFrame:
    columns = ["period"] + (["segment"] if group_by else []) + ["events"]
    if frame.empty:
        return pd.DataFrame({
            "period": pd.Series(dtype="datetime64[ns]"),
            **({"segment": pd.Series(dtype="object")} if group_by else {}),
            "events": pd.Series(dtype="int64"),
        })
    result = frame[columns].copy()
    result["period"] = pd.to_datetime(result["period"])
    result["events"] = result["events"].astype("int64")
    if group_by:
        result["segment"] = result["segment"].astype(str)
        result = result.assign(_order=_order_segments(result, config, group_by))
        result = result.sort_values(["period", "_order", "segment"]).drop(columns="_order")
    else:
        result = result.sort_values("period")
    return result.reset_index(drop=True)


def _finish_counts(frame: pd.DataFrame, columns: list[str], int_columns: list[str]) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame({name: pd.Series(dtype="int64" if name in int_columns else "object") for name in columns})
    result = frame[columns].copy()
    for name in int_columns:
        result[name] = pd.to_numeric(result[name]).fillna(0).astype("int64")
    return result


# -- pandas reference implementation (demo mode) ---------------------------


def _label_value(value: object, unknown: str) -> str:
    if value is None or pd.isna(value):
        return unknown
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip() or unknown


def _reference_segments(frame: pd.DataFrame, config: AppConfig, column: str, dimension: str) -> pd.Series:
    unknown = UNKNOWN_LABELS[dimension]
    labels = frame[column].map(lambda value: _label_value(value, unknown))
    if dimension != "customer_size":
        return labels
    edges = _validated_edges(config)
    names = customer_size_labels(edges)
    known = labels != unknown
    sizes = labels.map(labels[known].value_counts())

    def bucket(size: float) -> str:
        for index, edge in enumerate(edges):
            if size < edge:
                return names[index]
        return names[-1]

    return pd.Series(
        [bucket(size) if flag else unknown for size, flag in zip(sizes, known)],
        index=labels.index, dtype="object",
    )


def _reference_period(dates: pd.Series, grain: str) -> pd.Series:
    if grain == "day":
        return dates.dt.normalize()
    if grain == "week":
        return dates.dt.normalize() - pd.to_timedelta(dates.dt.dayofweek, unit="D")
    return dates.dt.to_period("M").dt.to_timestamp()


def _reference_dates(frame: pd.DataFrame, column: str) -> pd.Series:
    return pd.to_datetime(frame[column], errors="coerce")


def _in_range(dates: pd.Series, from_date: date | None, to_date: date | None) -> pd.Series:
    mask = dates.notna()
    if from_date is not None and to_date is not None:
        mask &= dates >= pd.Timestamp(from_date)
        mask &= dates < pd.Timestamp(to_date + timedelta(days=1))
    return mask


def reference_event_counts(
    frame: pd.DataFrame,
    config: AppConfig,
    event: str,
    grain: str = "month",
    group_by: str | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
) -> pd.DataFrame:
    """Pandas implementation of fetch_event_counts' semantics over a flat-table frame."""
    _check_choice(event, EVENT_COLUMNS, "event")
    _check_choice(grain, GRAINS, "grain")
    if group_by is not None:
        _check_choice(group_by, DIMENSION_COLUMNS, "group_by")
    _validate_range(from_date, to_date)
    cols = _resolve_columns(frame.columns, config, _attributes_for(group_by, EVENT_COLUMNS[event]))
    dates = _reference_dates(frame, cols[EVENT_COLUMNS[event]])
    work = pd.DataFrame({"period": _reference_period(dates, grain)})
    if group_by:
        work["segment"] = _reference_segments(frame, config, cols[DIMENSION_COLUMNS[group_by]], group_by)
    work = work.loc[_in_range(dates, from_date, to_date)]
    keys = ["period"] + (["segment"] if group_by else [])
    counts = work.groupby(keys, as_index=False).size().rename(columns={"size": "events"})
    return _finish_event_counts(counts, config, group_by)


def reference_segment_counts(
    frame: pd.DataFrame,
    config: AppConfig,
    dimension: str,
    from_date: date | None = None,
    to_date: date | None = None,
    top_n: int | None = None,
) -> pd.DataFrame:
    _check_choice(dimension, DIMENSION_COLUMNS, "dimension")
    _validate_range(from_date, to_date)
    has_purchased = _has_column(frame.columns, config, "col_sales_date")
    cols = _resolve_columns(
        frame.columns, config, _attributes_for(dimension, "col_installed_at"),
    )
    installed = _reference_dates(frame, cols["col_installed_at"])
    if has_purchased:
        purchased = _in_range(
            _reference_dates(frame, _resolve_columns(frame.columns, config, ["col_sales_date"])["col_sales_date"]),
            from_date, to_date,
        ).astype(int)
    else:
        purchased = pd.Series(0, index=frame.index)
    work = pd.DataFrame({
        "segment": _reference_segments(frame, config, cols[DIMENSION_COLUMNS[dimension]], dimension),
        "purchased": purchased,
        "installed": _in_range(installed, from_date, to_date).astype(int),
        "not_installed": installed.isna().astype(int),
    })
    grouped = work.groupby("segment", as_index=False).agg(
        devices=("segment", "size"), purchased=("purchased", "sum"),
        installed=("installed", "sum"), not_installed=("not_installed", "sum"),
    )
    return _finish_segment_counts(grouped, config, dimension, top_n, has_purchased)


def _finish_segment_counts(
    frame: pd.DataFrame, config: AppConfig, dimension: str, top_n: int | None,
    purchased_available: bool = True,
) -> pd.DataFrame:
    result = _finish_segment_counts_frame(frame, config, dimension, top_n)
    result.attrs["purchased_available"] = purchased_available
    return result


def _finish_segment_counts_frame(
    frame: pd.DataFrame, config: AppConfig, dimension: str, top_n: int | None,
) -> pd.DataFrame:
    result = _finish_counts(
        frame, ["segment", "devices", "purchased", "installed", "not_installed"],
        ["devices", "purchased", "installed", "not_installed"],
    )
    if result.empty:
        return result
    result["segment"] = result["segment"].astype(str)
    result = result.assign(_order=_order_segments(result, config, dimension))
    result = result.sort_values(["devices", "_order", "segment"], ascending=[False, True, True])
    if top_n is not None:
        result = result.head(top_n)
    return result.drop(columns="_order").reset_index(drop=True)


def reference_customer_size_distribution(frame: pd.DataFrame, config: AppConfig) -> pd.DataFrame:
    cols = _resolve_columns(frame.columns, config, ["col_customer"])
    keys = frame[cols["col_customer"]].map(lambda v: _label_value(v, UNKNOWN_LABELS["customer"]))
    segments = _reference_segments(frame, config, cols["col_customer"], "customer_size")
    work = pd.DataFrame({"segment": segments, "company": keys})
    grouped = work.groupby("segment", as_index=False).agg(
        companies=("company", "nunique"), devices=("company", "size"),
    )
    unknown = grouped["segment"] == UNKNOWN_LABELS["customer_size"]
    grouped.loc[unknown, "companies"] = 0
    return _finish_size_distribution(grouped, config)


def _finish_size_distribution(frame: pd.DataFrame, config: AppConfig) -> pd.DataFrame:
    result = _finish_counts(frame, ["segment", "companies", "devices"], ["companies", "devices"])
    if result.empty:
        return result
    result["segment"] = result["segment"].astype(str)
    result = result.assign(_order=_order_segments(result, config, "customer_size"))
    return result.sort_values("_order").drop(columns="_order").reset_index(drop=True)


def reference_shipping_lag_distribution(
    frame: pd.DataFrame,
    config: AppConfig,
    from_date: date | None = None,
    to_date: date | None = None,
) -> pd.DataFrame:
    _validate_range(from_date, to_date)
    cols = _resolve_columns(frame.columns, config, ["col_shipped_at", "col_installed_at"])
    shipped = _reference_dates(frame, cols["col_shipped_at"])
    installed = _reference_dates(frame, cols["col_installed_at"])
    mask = shipped.notna() & _in_range(installed, from_date, to_date)
    lag = (installed[mask].dt.normalize() - shipped[mask].dt.normalize()).dt.days
    grouped = lag.value_counts().rename_axis("lag_days").reset_index(name="devices")
    return _finish_lag_distribution(grouped)


def _finish_lag_distribution(frame: pd.DataFrame) -> pd.DataFrame:
    result = _finish_counts(frame, ["lag_days", "devices"], ["lag_days", "devices"])
    return result.sort_values("lag_days").reset_index(drop=True)


def reference_inventory_summary(
    frame: pd.DataFrame, config: AppConfig, as_of: date, trailing_days: Iterable[int] = (30, 90),
) -> pd.DataFrame:
    windows = _validated_windows(trailing_days)
    cols = _resolve_columns(frame.columns, config, ["col_product_type", "col_installed_at"])
    installed = _reference_dates(frame, cols["col_installed_at"])
    work = pd.DataFrame({
        "product_type": _reference_segments(frame, config, cols["col_product_type"], "product_type"),
        "on_hand": installed.isna().astype(int),
    })
    end = pd.Timestamp(as_of + timedelta(days=1))
    for days in windows:
        start = pd.Timestamp(as_of - timedelta(days=days - 1))
        work[f"installs_{days}d"] = ((installed >= start) & (installed < end)).astype(int)
    aggregations = {"total_devices": ("product_type", "size"), "on_hand": ("on_hand", "sum")}
    aggregations.update({f"installs_{d}d": (f"installs_{d}d", "sum") for d in windows})
    grouped = work.groupby("product_type", as_index=False).agg(**aggregations)
    return _finish_inventory(grouped, windows)


def _finish_inventory(frame: pd.DataFrame, windows: tuple[int, ...]) -> pd.DataFrame:
    trailing = [f"installs_{days}d" for days in windows]
    result = _finish_counts(
        frame, ["product_type", "total_devices", "on_hand", *trailing],
        ["total_devices", "on_hand", *trailing],
    )
    if result.empty:
        return result
    result["product_type"] = result["product_type"].astype(str)
    return result.sort_values("product_type").reset_index(drop=True)


# -- public aggregate fetchers ---------------------------------------------


def fetch_event_counts(
    engine: Engine | None,
    config: AppConfig,
    event: str,
    grain: str = "month",
    from_date: date | None = None,
    to_date: date | None = None,
    group_by: str | None = None,
) -> pd.DataFrame:
    """Count 'installs' or 'purchases' per day/week/month, optionally per dimension.

    Columns: period (datetime64, period start; weeks start Monday), [segment], events.
    group_by: territory | customer | product_type | customer_size (or None).
    Rows with a NULL event date are excluded; blank dimension values become the
    dimension's 'Unknown ...' label. Bounds are inclusive dates.
    """
    _ensure_flat_mode(config)
    if config.demo_mode:
        return reference_event_counts(
            _read_demo_csv(config), config, event, grain, group_by, from_date, to_date,
        )
    _validate_db_columns(engine, config, _attributes_for(group_by, EVENT_COLUMNS[event]))
    sql, params = build_event_counts_query(config, event, grain, group_by, from_date, to_date)
    return _finish_event_counts(_read_aggregate(engine, sql, params), config, group_by)


def fetch_monthly_purchase_counts(
    engine: Engine | None,
    config: AppConfig,
    group_by: str = "territory",
    from_date: date | None = None,
    to_date: date | None = None,
) -> pd.DataFrame:
    """Monthly purchase counts per market dimension (month x dimension), aggregated in SQL.

    'Markets' are territories and product types, answered from purchase history.
    Columns: period (month start), segment, events. group_by: territory | product_type
    | customer | customer_size.
    """
    return fetch_event_counts(engine, config, "purchases", "month", from_date, to_date, group_by)


def pivot_monthly_counts(counts: pd.DataFrame, fill_missing: bool = True) -> pd.DataFrame:
    """Pivot fetch_event_counts(group_by=...) output to month rows x segment columns.

    With fill_missing, every month between the first and last period is present
    (zeros where nothing was purchased), which suits correlation/forecast code.
    """
    if counts.empty:
        return pd.DataFrame()
    wide = counts.pivot_table(
        index="period", columns="segment", values="events", aggfunc="sum", fill_value=0,
    ).astype("int64")
    if fill_missing:
        full = pd.date_range(wide.index.min(), wide.index.max(), freq="MS")
        wide = wide.reindex(full, fill_value=0)
        wide.index.name = "period"
    wide.columns.name = None
    return wide


def fetch_segment_counts(
    engine: Engine | None,
    config: AppConfig,
    dimension: str,
    from_date: date | None = None,
    to_date: date | None = None,
    top_n: int | None = None,
) -> pd.DataFrame:
    """Per-segment totals: devices (all), purchased/installed (in range), not_installed (NULL install).

    dimension: territory | customer | product_type | customer_size. Ordered by devices
    descending; top_n keeps the largest segments (ties are ordered arbitrarily by SQL).

    col_sales_date is optional: when it is unmapped or absent from the table, purchased
    is 0 and result.attrs['purchased_available'] is False (devices, installed and
    not_installed stay exact). Only the dimension and install columns are required.
    """
    _ensure_flat_mode(config)
    if config.demo_mode:
        return reference_segment_counts(
            _read_demo_csv(config), config, dimension, from_date, to_date, top_n,
        )
    available = fetch_flat_table_columns(engine, config)
    _resolve_columns(available, config, _attributes_for(dimension, "col_installed_at"))
    has_purchased = _has_column(available, config, "col_sales_date")
    sql, params = build_segment_counts_query(
        config, dimension, from_date, to_date, top_n, include_purchased=has_purchased,
    )
    return _finish_segment_counts(
        _read_aggregate(engine, sql, params), config, dimension, top_n, has_purchased,
    )


def fetch_customer_size_distribution(engine: Engine | None, config: AppConfig) -> pd.DataFrame:
    """Companies and devices per size bucket (device count per company vs IQ_CUSTOMER_SIZE_EDGES).

    Columns: segment, companies, devices. Devices without a company are 'Unknown'.
    """
    _ensure_flat_mode(config)
    if config.demo_mode:
        return reference_customer_size_distribution(_read_demo_csv(config), config)
    _validate_db_columns(engine, config, ["col_customer"])
    sql, params = build_customer_size_distribution_query(config)
    return _finish_size_distribution(_read_aggregate(engine, sql, params), config)


def fetch_shipping_lag_distribution(
    engine: Engine | None,
    config: AppConfig,
    from_date: date | None = None,
    to_date: date | None = None,
) -> pd.DataFrame:
    """Histogram of whole-day lag from shipped (col_shipped_at) to installed, filtered by install date.

    Columns: lag_days, devices. Rows missing either date are excluded. NEGATIVE lags
    (installed before the ship date, a data-quality signal) are deliberately kept in
    both SQL and demo mode; callers that want only valid lags filter lag_days >= 0.
    Exact percentile inputs: see lag_percentiles /
    bin_lag_distribution. col_shipped_at defaults to PurchaseDate, a proxy
    (AppConfig.shipped_at_is_proxy).
    """
    _ensure_flat_mode(config)
    if config.demo_mode:
        return reference_shipping_lag_distribution(_read_demo_csv(config), config, from_date, to_date)
    _validate_db_columns(engine, config, ["col_shipped_at", "col_installed_at"])
    sql, params = build_shipping_lag_query(config, from_date, to_date)
    return _finish_lag_distribution(_read_aggregate(engine, sql, params))


def fetch_inventory_summary(
    engine: Engine | None,
    config: AppConfig,
    as_of: date | None = None,
    trailing_days: Iterable[int] = (30, 90),
) -> pd.DataFrame:
    """Per product type: total_devices, on_hand (devices with NO install date) and installs_{N}d.

    installs_{N}d counts installs in the N calendar days ending on (and including) as_of
    (default today); use it as the demand rate for stockout/overstock thresholds.
    """
    _ensure_flat_mode(config)
    as_of = as_of or date.today()
    windows = _validated_windows(trailing_days)
    if config.demo_mode:
        return reference_inventory_summary(_read_demo_csv(config), config, as_of, windows)
    _validate_db_columns(engine, config, ["col_product_type", "col_installed_at"])
    sql, params = build_inventory_summary_query(config, as_of, windows)
    return _finish_inventory(_read_aggregate(engine, sql, params), windows)


# -- helpers over lag histograms --------------------------------------------


def _as_date(value: date | None) -> date | None:
    """Accept date, datetime or pd.Timestamp cut-offs; return a plain date."""
    return value.date() if isinstance(value, datetime) else value


def _as_of_exclusive(as_of: date) -> datetime:
    return datetime.combine(as_of + timedelta(days=1), time.min)


def build_purchase_install_cohorts_query(
    config: AppConfig,
    from_date: date | None = None,
    to_date: date | None = None,
    group_by: str | None = None,
    as_of: date | None = None,
) -> tuple[str, dict[str, object]]:
    if group_by is not None:
        _check_choice(group_by, DIMENSION_COLUMNS, "group_by")
    _validate_range(from_date, to_date)
    ctes, relation, params = _base_relation(
        config, {"purchased_at": "col_sales_date", "installed_at": "col_installed_at"}, group_by,
    )
    params.update(_range_params(from_date, to_date))
    segment = ", segment" if group_by else ""
    source = relation
    if as_of is not None:
        # Installs after as_of count as "not installed yet" (censoring).
        params["as_of_exclusive"] = _as_of_exclusive(as_of)
        ctes.append(
            "censored AS (SELECT purchased_at, "
            f"CASE WHEN installed_at < :as_of_exclusive THEN installed_at END AS installed_at{segment} "
            f"FROM {relation})"
        )
        source = "censored"
    where = (
        "WHERE purchased_at >= :start_dt AND purchased_at < :end_exclusive" if from_date is not None else ""
    )
    ctes.append(
        "bucketed AS (SELECT "
        "DATEFROMPARTS(YEAR(purchased_at), MONTH(purchased_at), 1) AS purchase_month, "
        "DATEFROMPARTS(YEAR(installed_at), MONTH(installed_at), 1) AS install_month, "
        "CASE WHEN CAST(installed_at AS date) < CAST(purchased_at AS date) THEN 1 ELSE 0 END AS is_negative"
        f"{segment} FROM {source} {where})"
    )
    keys = f"purchase_month, install_month{segment}"
    sql = (
        f"{_with(ctes)}\n"
        f"SELECT {keys}, COUNT(*) AS devices, SUM(is_negative) AS negative_lag_devices "
        f"FROM bucketed GROUP BY {keys} ORDER BY {keys}"
    )
    return sql, params


LAG_BASES = {"shipped": "col_shipped_at", "purchased": "col_sales_date"}


def build_lag_by_cohort_query(
    config: AppConfig,
    from_date: date | None = None,
    to_date: date | None = None,
    basis: str = "shipped",
    as_of: date | None = None,
) -> tuple[str, dict[str, object]]:
    _check_choice(basis, LAG_BASES, "basis")
    _validate_range(from_date, to_date)
    ctes, relation, params = _base_relation(
        config, {"start_at": LAG_BASES[basis], "installed_at": "col_installed_at"}, None,
    )
    params.update(_range_params(from_date, to_date))
    where = "start_at IS NOT NULL AND installed_at IS NOT NULL"
    if from_date is not None:
        where += " AND start_at >= :start_dt AND start_at < :end_exclusive"
    if as_of is not None:
        params["as_of_exclusive"] = _as_of_exclusive(as_of)
        where += " AND installed_at < :as_of_exclusive"
    ctes.append(
        "lagged AS (SELECT DATEFROMPARTS(YEAR(start_at), MONTH(start_at), 1) AS cohort_month, "
        f"DATEDIFF(day, start_at, installed_at) AS lag_days FROM {relation} WHERE {where})"
    )
    sql = (
        f"{_with(ctes)}\n"
        "SELECT cohort_month, lag_days, COUNT(*) AS devices FROM lagged "
        "GROUP BY cohort_month, lag_days ORDER BY cohort_month, lag_days"
    )
    return sql, params


def _month_start(dates: pd.Series) -> pd.Series:
    return dates.dt.to_period("M").dt.to_timestamp()


def _finish_cohorts(frame: pd.DataFrame, config: AppConfig, group_by: str | None) -> pd.DataFrame:
    keys = ["purchase_month", "install_month"] + (["segment"] if group_by else [])
    columns = keys + ["devices", "negative_lag_devices"]
    if frame.empty:
        return pd.DataFrame({
            "purchase_month": pd.Series(dtype="datetime64[ns]"),
            "install_month": pd.Series(dtype="datetime64[ns]"),
            **({"segment": pd.Series(dtype="object")} if group_by else {}),
            "devices": pd.Series(dtype="int64"),
            "negative_lag_devices": pd.Series(dtype="int64"),
        })
    result = frame[columns].copy()
    for name in ("purchase_month", "install_month"):
        result[name] = pd.to_datetime(result[name])
    for name in ("devices", "negative_lag_devices"):
        result[name] = pd.to_numeric(result[name]).fillna(0).astype("int64")
    sort_by = ["purchase_month", "install_month"]
    if group_by:
        result["segment"] = result["segment"].astype(str)
        result["_order"] = _order_segments(result, config, group_by)
        sort_by += ["_order", "segment"]
    result = result.sort_values(sort_by, na_position="last")
    return result[columns].reset_index(drop=True)


def reference_purchase_install_cohorts(
    frame: pd.DataFrame,
    config: AppConfig,
    from_date: date | None = None,
    to_date: date | None = None,
    group_by: str | None = None,
    as_of: date | None = None,
) -> pd.DataFrame:
    if group_by is not None:
        _check_choice(group_by, DIMENSION_COLUMNS, "group_by")
    _validate_range(from_date, to_date)
    cols = _resolve_columns(
        frame.columns, config, _attributes_for(group_by, "col_sales_date", "col_installed_at"),
    )
    purchased = _reference_dates(frame, cols["col_sales_date"])
    installed = _reference_dates(frame, cols["col_installed_at"])
    if as_of is not None:
        installed = installed.where(installed < pd.Timestamp(as_of + timedelta(days=1)))
    work = pd.DataFrame({
        "purchase_month": _month_start(purchased),
        "install_month": _month_start(installed),
        "is_negative": (installed.dt.normalize() < purchased.dt.normalize()).astype(int),
    })
    keys = ["purchase_month", "install_month"]
    if group_by:
        work["segment"] = _reference_segments(frame, config, cols[DIMENSION_COLUMNS[group_by]], group_by)
        keys.append("segment")
    if from_date is not None:
        work = work.loc[_in_range(purchased, from_date, to_date)]
    grouped = work.groupby(keys, dropna=False, as_index=False).agg(
        devices=("is_negative", "size"), negative_lag_devices=("is_negative", "sum"),
    )
    return _finish_cohorts(grouped, config, group_by)


def _finish_lag_by_cohort(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame({
            "cohort_month": pd.Series(dtype="datetime64[ns]"),
            "lag_days": pd.Series(dtype="int64"),
            "devices": pd.Series(dtype="int64"),
        })
    result = frame[["cohort_month", "lag_days", "devices"]].copy()
    result["cohort_month"] = pd.to_datetime(result["cohort_month"])
    for name in ("lag_days", "devices"):
        result[name] = pd.to_numeric(result[name]).astype("int64")
    return result.sort_values(["cohort_month", "lag_days"]).reset_index(drop=True)


def reference_lag_by_cohort(
    frame: pd.DataFrame,
    config: AppConfig,
    from_date: date | None = None,
    to_date: date | None = None,
    basis: str = "shipped",
    as_of: date | None = None,
) -> pd.DataFrame:
    _check_choice(basis, LAG_BASES, "basis")
    _validate_range(from_date, to_date)
    cols = _resolve_columns(frame.columns, config, [LAG_BASES[basis], "col_installed_at"])
    start = _reference_dates(frame, cols[LAG_BASES[basis]])
    installed = _reference_dates(frame, cols["col_installed_at"])
    mask = start.notna() & installed.notna()
    if from_date is not None:
        mask &= _in_range(start, from_date, to_date)
    if as_of is not None:
        mask &= installed < pd.Timestamp(as_of + timedelta(days=1))
    work = pd.DataFrame({
        "cohort_month": _month_start(start[mask]),
        "lag_days": (installed[mask].dt.normalize() - start[mask].dt.normalize()).dt.days,
    })
    grouped = work.groupby(["cohort_month", "lag_days"], as_index=False).size().rename(columns={"size": "devices"})
    return _finish_lag_by_cohort(grouped)


def fetch_purchase_install_cohorts(
    engine: Engine | None,
    config: AppConfig,
    from_date: date | None = None,
    to_date: date | None = None,
    group_by: str | None = None,
    as_of: date | None = None,
) -> pd.DataFrame:
    """Devices per (purchase month, install month) without row-level reads.

    Columns: purchase_month, install_month (NaT = not installed), [segment],
    devices, negative_lag_devices (install date earlier than purchase date; those
    devices are also counted in devices). Bounds filter the purchase date
    (inclusive); with no bounds every device is included, even when the purchase
    date is NULL (purchase_month NaT). as_of treats installs dated after it as not
    installed yet, which makes cohort rates exact for a cut-off inside a month.
    group_by: territory | customer | product_type | customer_size | None.
    """
    _ensure_flat_mode(config)
    as_of = _as_date(as_of)
    if config.demo_mode:
        return reference_purchase_install_cohorts(
            _read_demo_csv(config), config, from_date, to_date, group_by, as_of,
        )
    _validate_db_columns(
        engine, config, _attributes_for(group_by, "col_sales_date", "col_installed_at"),
    )
    sql, params = build_purchase_install_cohorts_query(config, from_date, to_date, group_by, as_of)
    return _finish_cohorts(_read_aggregate(engine, sql, params), config, group_by)


def fetch_lag_by_cohort(
    engine: Engine | None,
    config: AppConfig,
    from_date: date | None = None,
    to_date: date | None = None,
    basis: str = "shipped",
    as_of: date | None = None,
) -> pd.DataFrame:
    """Exact lag histogram per cohort month: columns cohort_month, lag_days, devices.

    Lag = whole days from the start date to the install date; the cohort is the
    month of the start date. basis 'shipped' uses col_shipped_at (default
    PurchaseDate, a proxy) and 'purchased' uses col_sales_date. Bounds filter the
    start date. Devices missing either date are excluded; negative lags are kept as
    rows with lag_days < 0 so they can be counted separately (see
    summarize_lag_by_cohort). as_of drops installs dated after it.
    """
    _ensure_flat_mode(config)
    as_of = _as_date(as_of)
    if config.demo_mode:
        return reference_lag_by_cohort(
            _read_demo_csv(config), config, from_date, to_date, basis, as_of,
        )
    _check_choice(basis, LAG_BASES, "basis")
    _validate_db_columns(engine, config, [LAG_BASES[basis], "col_installed_at"])
    sql, params = build_lag_by_cohort_query(config, from_date, to_date, basis, as_of)
    return _finish_lag_by_cohort(_read_aggregate(engine, sql, params))


def summarize_lag_by_cohort(lag_by_cohort: pd.DataFrame) -> pd.DataFrame:
    """Per-cohort exact stats from fetch_lag_by_cohort (negative lags counted separately).

    Columns: cohort_month, devices (valid lags >= 0), negative_lag_devices,
    mean_days, median_days, p90_days (NaN when a cohort has no valid lag). Percentiles
    use linear interpolation, matching pandas' default quantile.
    """
    columns = ["cohort_month", "devices", "negative_lag_devices", "mean_days", "median_days", "p90_days"]
    if lag_by_cohort is None or lag_by_cohort.empty:
        return pd.DataFrame(columns=columns)
    rows = []
    for month, group in lag_by_cohort.groupby("cohort_month", sort=True):
        valid = group[group["lag_days"] >= 0]
        count = int(valid["devices"].sum())
        stats = (
            lag_percentiles(valid[["lag_days", "devices"]], (0.5, 0.9)) if count else {0.5: math.nan, 0.9: math.nan}
        )
        rows.append({
            "cohort_month": month,
            "devices": count,
            "negative_lag_devices": int(group.loc[group["lag_days"] < 0, "devices"].sum()),
            "mean_days": float((valid["lag_days"] * valid["devices"]).sum() / count) if count else math.nan,
            "median_days": stats[0.5],
            "p90_days": stats[0.9],
        })
    return pd.DataFrame(rows, columns=columns)


def lag_percentiles(
    distribution: pd.DataFrame, quantiles: Iterable[float] = (0.25, 0.5, 0.75, 0.9),
) -> dict[float, float]:
    """Exact linear-interpolated percentiles from a (lag_days, devices) histogram."""
    data = distribution.sort_values("lag_days")
    lags = data["lag_days"].to_numpy(dtype=float)
    cumulative = data["devices"].to_numpy(dtype=np.int64).cumsum()
    total = int(cumulative[-1]) if len(cumulative) else 0
    result: dict[float, float] = {}
    for quantile in quantiles:
        if not 0 <= quantile <= 1:
            raise ValueError("Quantiles must be between 0 and 1.")
        if total == 0:
            result[quantile] = math.nan
            continue
        position = quantile * (total - 1)
        low, high = math.floor(position), math.ceil(position)
        low_value = lags[np.searchsorted(cumulative, low, side="right")]
        high_value = lags[np.searchsorted(cumulative, high, side="right")]
        result[quantile] = float(low_value + (position - low) * (high_value - low_value))
    return result


def bin_lag_distribution(distribution: pd.DataFrame, bin_days: int = 7) -> pd.DataFrame:
    """Re-bin a lag histogram: columns bin_start, bin_end (exclusive), devices."""
    if bin_days < 1:
        raise ValueError("bin_days must be greater than zero.")
    if distribution.empty:
        return pd.DataFrame({"bin_start": [], "bin_end": [], "devices": []}, dtype="int64")
    start = (distribution["lag_days"] // bin_days) * bin_days
    binned = distribution.assign(bin_start=start).groupby("bin_start", as_index=False)["devices"].sum()
    binned["bin_end"] = binned["bin_start"] + bin_days
    return binned[["bin_start", "bin_end", "devices"]].astype("int64")


# -- chat / Ask-the-data aggregates (SQL-side; no raw rows are loaded) -----------

CHAT_ATTRIBUTES = (
    "col_device", "col_customer", "col_product_type", "col_sales_date",
    "col_installed_at", "col_latitude", "col_longitude",
)
CHAT_MODES = ("rows", "events", "pending", "delay")
CHAT_GROUPINGS = ("none", "period", "value")
CHAT_ORDERS = ("group", "count")
CHAT_ALL_LABEL = "All matching rows"
CHAT_MISSING_LABEL = "(missing)"
_CHAT_DELAY_COLUMNS = ["mean_days", "median_days", "min_days", "max_days"]
_CHAT_MS_PER_DAY = "86400000.0"
_CHAT_ALIAS = re.compile(r"[a-z_]+")


def _chat_column(config: AppConfig, attribute: str) -> str:
    _check_choice(attribute, CHAT_ATTRIBUTES, "attribute")
    return _quote_name(getattr(config, attribute))


def _chat_scope(
    config: AppConfig, scope_attr: str | None, from_date: date | None, to_date: date | None,
) -> tuple[list[str], dict[str, object]]:
    """WHERE fragments + params restricting rows to an inclusive date range of one column."""
    _validate_range(from_date, to_date)
    if (scope_attr is None) != (from_date is None):
        raise ValueError("A scope column and both date bounds must be provided together.")
    if scope_attr is None:
        return [], {}
    column = _chat_column(config, scope_attr)
    return [f"{column} >= :start_dt", f"{column} < :end_exclusive"], _range_params(from_date, to_date)


def _where(conditions: list[str]) -> str:
    return "WHERE " + " AND ".join(conditions) if conditions else ""


def build_chat_row_profile_query(
    config: AppConfig,
    scope_attr: str | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
    distinct_attrs: dict[str, str] | None = None,
    pending: bool = False,
    delay: bool = False,
    coordinates: bool = False,
) -> tuple[str, dict[str, object]]:
    """One aggregate row over the (optionally date-scoped) flat table: counts only."""
    conditions, params = _chat_scope(config, scope_attr, from_date, to_date)
    selects = ["COUNT(*) AS row_count"]
    for alias, attribute in (distinct_attrs or {}).items():
        if not _CHAT_ALIAS.fullmatch(alias):
            raise ValueError(f"Unsafe alias: '{alias}'")
        column = _chat_column(config, attribute)
        selects.append(
            f"COUNT(DISTINCT CAST({column} AS nvarchar(4000)) COLLATE Latin1_General_BIN2) "
            f"AS [distinct_{alias}]"
        )
    if pending or delay:
        purchased = _chat_column(config, "col_sales_date")
        installed = _chat_column(config, "col_installed_at")
        if pending:
            selects.append(
                f"SUM(CASE WHEN {purchased} IS NOT NULL AND {installed} IS NULL THEN 1 ELSE 0 END) AS pending"
            )
        if delay:
            both = f"{purchased} IS NOT NULL AND {installed} IS NOT NULL"
            selects.append(f"SUM(CASE WHEN {both} THEN 1 ELSE 0 END) AS paired")
            selects.append(
                f"SUM(CASE WHEN {both} AND {installed} < {purchased} THEN 1 ELSE 0 END) AS negative_delay"
            )
    if coordinates:
        lat, lon = _chat_column(config, "col_latitude"), _chat_column(config, "col_longitude")
        selects.append(
            f"SUM(CASE WHEN TRY_CAST({lat} AS float) BETWEEN -90 AND 90 "
            f"AND TRY_CAST({lon} AS float) BETWEEN -180 AND 180 THEN 1 ELSE 0 END) AS valid_coordinates"
        )
    table = _quote_table_name(config.source_table)
    return f"SELECT {', '.join(selects)} FROM {table} {_where(conditions)}".strip(), params


def _ms_to_days(expression: str) -> str:
    return f"{expression} / {_CHAT_MS_PER_DAY}"


def build_chat_group_query(
    config: AppConfig,
    mode: str,
    grouping: str = "none",
    event_attr: str | None = None,
    group_attr: str | None = None,
    grain: str = "month",
    scope_attr: str | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
    order: str = "group",
    limit: int = 1000,
) -> tuple[str, dict[str, object]]:
    """GROUP BY counts (and exact delay statistics) for one supported chat/guided metric.

    mode: rows (every row), events (event_attr not NULL), pending (purchased, not installed),
    delay (installed >= purchased; days from PERCENTILE_CONT/AVG/MIN/MAX over milliseconds).
    grouping: none | period (event_attr bucketed by grain) | value (group_attr text).
    """
    _check_choice(mode, CHAT_MODES, "mode")
    _check_choice(grouping, CHAT_GROUPINGS, "grouping")
    _check_choice(grain, GRAINS, "grain")
    _check_choice(order, CHAT_ORDERS, "order")
    if limit < 1:
        raise ValueError("limit must be greater than zero.")
    conditions, params = _chat_scope(config, scope_attr, from_date, to_date)
    event = _chat_column(config, event_attr) if event_attr else None
    if mode == "events" or grouping == "period":
        if event is None:
            raise ValueError("An event date column is required.")
    if mode == "events":
        conditions.append(f"{event} IS NOT NULL")
    if mode in {"pending", "delay"}:
        purchased = _chat_column(config, "col_sales_date")
        installed = _chat_column(config, "col_installed_at")
        conditions.append(f"{purchased} IS NOT NULL")
        conditions.append(f"{installed} IS NULL" if mode == "pending" else f"{installed} IS NOT NULL")
        if mode == "delay":
            conditions.append(f"{installed} >= {purchased}")
    group_expr = None
    if grouping == "period":
        group_expr = f"CONVERT(char(10), {_period_expr(grain, event)}, 23)"
    elif grouping == "value":
        if not group_attr:
            raise ValueError("A grouping column is required.")
        group_expr = (
            f"COALESCE(CAST({_chat_column(config, group_attr)} AS nvarchar(4000)), "
            f"N'{CHAT_MISSING_LABEL}') COLLATE Latin1_General_BIN2"
        )
    table = _quote_table_name(config.source_table)
    where = _where(conditions)
    params["limit"] = limit
    ordering = "grp" if order == "group" else "cnt DESC, grp"

    if mode != "delay":
        if group_expr is None:
            return (
                f"SELECT N'{CHAT_ALL_LABEL}' AS grp, COUNT(*) AS cnt FROM {table} {where} "
                "HAVING COUNT(*) > 0"
            ), params
        sql = (
            f"WITH scoped AS (SELECT {group_expr} AS grp FROM {table} {where})\n"
            f"SELECT TOP (:limit) grp, COUNT(*) AS cnt FROM scoped GROUP BY grp ORDER BY {ordering}"
        )
        return sql, params

    delay_ms = f"CAST(DATEDIFF_BIG(millisecond, {purchased}, {installed}) AS float)"
    days = lambda expr: f"{expr} / {_CHAT_MS_PER_DAY}"  # noqa: E731
    stats = (
        "COUNT(*) AS cnt, AVG(delay_ms) AS mean_ms, MIN(delay_ms) AS min_ms, MAX(delay_ms) AS max_ms"
    )
    outputs = (
        f"s.cnt AS cnt, {_ms_to_days('s.mean_ms')} AS mean_days, {_ms_to_days('m.median_ms')} AS median_days, "
        f"{_ms_to_days('s.min_ms')} AS min_days, {_ms_to_days('s.max_ms')} AS max_days"
    )
    if group_expr is None:
        sql = (
            f"WITH scoped AS (SELECT {delay_ms} AS delay_ms FROM {table} {where}),\n"
            "medians AS (SELECT DISTINCT PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY delay_ms) "
            "OVER () AS median_ms FROM scoped),\n"
            f"stats AS (SELECT {stats} FROM scoped HAVING COUNT(*) > 0)\n"
            f"SELECT N'{CHAT_ALL_LABEL}' AS grp, {outputs} FROM stats s CROSS JOIN medians m"
        )
        return sql, params
    sql = (
        f"WITH scoped AS (SELECT {group_expr} AS grp, {delay_ms} AS delay_ms FROM {table} {where}),\n"
        "medians AS (SELECT DISTINCT grp, PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY delay_ms) "
        "OVER (PARTITION BY grp) AS median_ms FROM scoped),\n"
        f"stats AS (SELECT grp, {stats} FROM scoped GROUP BY grp)\n"
        f"SELECT TOP (:limit) s.grp AS grp, {outputs} FROM stats s JOIN medians m ON m.grp = s.grp "
        f"ORDER BY {'s.grp' if order == 'group' else 's.cnt DESC, s.grp'}"
    )
    return sql, params


def _finish_chat_groups(frame: pd.DataFrame, mode: str, order: str, limit: int) -> pd.DataFrame:
    columns = ["group", "count"] + (_CHAT_DELAY_COLUMNS if mode == "delay" else [])
    if frame.empty:
        return pd.DataFrame({
            name: pd.Series(dtype="object" if name == "group" else "int64" if name == "count" else "float64")
            for name in columns
        })
    result = frame.rename(columns={"grp": "group", "cnt": "count"})[columns].copy()
    result["group"] = result["group"].astype(str)
    result["count"] = pd.to_numeric(result["count"]).astype("int64")
    for name in _CHAT_DELAY_COLUMNS if mode == "delay" else []:
        result[name] = pd.to_numeric(result[name]).astype("float64")
    by = ["group"] if order == "group" else ["count", "group"]
    ascending = [True] if order == "group" else [False, True]
    return result.sort_values(by, ascending=ascending, kind="stable").head(limit).reset_index(drop=True)


def _chat_group_attributes(
    mode: str, grouping: str, event_attr: str | None, group_attr: str | None, scope_attr: str | None,
) -> list[str]:
    attributes = [attr for attr in (scope_attr, event_attr, group_attr if grouping == "value" else None) if attr]
    if mode in {"pending", "delay"}:
        attributes += ["col_sales_date", "col_installed_at"]
    return list(dict.fromkeys(attributes))


def reference_chat_row_profile(
    frame: pd.DataFrame,
    config: AppConfig,
    scope_attr: str | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
    distinct_attrs: dict[str, str] | None = None,
    pending: bool = False,
    delay: bool = False,
    coordinates: bool = False,
) -> dict[str, int]:
    """Pandas implementation of fetch_chat_row_profile over a flat-table frame."""
    _chat_scope(config, scope_attr, from_date, to_date)
    attributes = [scope_attr, *(distinct_attrs or {}).values()]
    if pending or delay:
        attributes += ["col_sales_date", "col_installed_at"]
    if coordinates:
        attributes += ["col_latitude", "col_longitude"]
    for attribute in attributes:
        if attribute is not None:
            _check_choice(attribute, CHAT_ATTRIBUTES, "attribute")
    cols = _resolve_columns(frame.columns, config, [a for a in attributes if a])
    if scope_attr is not None:
        frame = frame.loc[_in_range(_reference_dates(frame, cols[scope_attr]), from_date, to_date)]
    profile = {"row_count": len(frame)}
    for alias, attribute in (distinct_attrs or {}).items():
        profile[f"distinct_{alias}"] = int(frame[cols[attribute]].nunique())
    if pending or delay:
        purchased = _reference_dates(frame, cols["col_sales_date"])
        installed = _reference_dates(frame, cols["col_installed_at"])
        if pending:
            profile["pending"] = int((purchased.notna() & installed.isna()).sum())
        if delay:
            days = (installed - purchased).dt.total_seconds() / 86400
            profile["paired"] = int(days.notna().sum())
            profile["negative_delay"] = int((days < 0).sum())
    if coordinates:
        lat = pd.to_numeric(frame[cols["col_latitude"]], errors="coerce")
        lon = pd.to_numeric(frame[cols["col_longitude"]], errors="coerce")
        profile["valid_coordinates"] = int((lat.between(-90, 90) & lon.between(-180, 180)).sum())
    return profile


def reference_chat_groups(
    frame: pd.DataFrame,
    config: AppConfig,
    mode: str,
    grouping: str = "none",
    event_attr: str | None = None,
    group_attr: str | None = None,
    grain: str = "month",
    scope_attr: str | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
    order: str = "group",
    limit: int = 1000,
) -> pd.DataFrame:
    """Pandas implementation of fetch_chat_groups' semantics (the former row-based logic)."""
    _check_choice(mode, CHAT_MODES, "mode")
    _check_choice(grouping, CHAT_GROUPINGS, "grouping")
    _check_choice(grain, GRAINS, "grain")
    _check_choice(order, CHAT_ORDERS, "order")
    _chat_scope(config, scope_attr, from_date, to_date)
    attributes = _chat_group_attributes(mode, grouping, event_attr, group_attr, scope_attr)
    for attribute in attributes:
        _check_choice(attribute, CHAT_ATTRIBUTES, "attribute")
    cols = _resolve_columns(frame.columns, config, attributes)
    if scope_attr is not None:
        frame = frame.loc[_in_range(_reference_dates(frame, cols[scope_attr]), from_date, to_date)]
    event = _reference_dates(frame, cols[event_attr]) if event_attr else None
    if mode == "events":
        valid = event.notna()
    elif mode == "rows":
        valid = pd.Series(True, index=frame.index)
    else:
        purchased = _reference_dates(frame, cols["col_sales_date"])
        installed = _reference_dates(frame, cols["col_installed_at"])
        days = (installed - purchased).dt.total_seconds() / 86400
        valid = (purchased.notna() & installed.isna()) if mode == "pending" else days.ge(0)
    if grouping == "period":
        labels = _reference_period(event, grain).dt.strftime("%Y-%m-%d")
    elif grouping == "value":
        labels = frame[cols[group_attr]].astype("string").fillna(CHAT_MISSING_LABEL)
    else:
        labels = pd.Series(CHAT_ALL_LABEL, index=frame.index)
    work = pd.DataFrame({"grp": labels.astype(object)})
    if mode == "delay":
        work["delay_days"] = days
    work = work.loc[valid]
    grouped = work.groupby("grp", sort=True)
    counts = grouped.size().reset_index(name="cnt")
    if mode == "delay" and not counts.empty:
        stats = grouped["delay_days"].agg(
            mean_days="mean", median_days="median", min_days="min", max_days="max",
        ).reset_index()
        counts = counts.merge(stats, on="grp")
    return _finish_chat_groups(counts, mode, order, limit)


def fetch_chat_row_profile(
    engine: Engine | None,
    config: AppConfig,
    scope_attr: str | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
    distinct_attrs: dict[str, str] | None = None,
    pending: bool = False,
    delay: bool = False,
    coordinates: bool = False,
) -> dict[str, int]:
    """Counts over the scoped rows in one SQL aggregate: row_count, distinct_<alias>, pending,
    paired/negative_delay (delay), valid_coordinates. Never returns row-level values."""
    _ensure_flat_mode(config)
    if config.demo_mode:
        return reference_chat_row_profile(
            _read_demo_csv(config), config, scope_attr, from_date, to_date,
            distinct_attrs, pending, delay, coordinates,
        )
    attributes = [scope_attr, *(distinct_attrs or {}).values()]
    if pending or delay:
        attributes += ["col_sales_date", "col_installed_at"]
    if coordinates:
        attributes += ["col_latitude", "col_longitude"]
    _validate_db_columns(engine, config, [a for a in attributes if a])
    sql, params = build_chat_row_profile_query(
        config, scope_attr, from_date, to_date, distinct_attrs, pending, delay, coordinates,
    )
    row = _read_aggregate(engine, sql, params).iloc[0]
    return {str(name): int(0 if pd.isna(value) else value) for name, value in row.items()}


def fetch_chat_groups(
    engine: Engine | None,
    config: AppConfig,
    mode: str,
    grouping: str = "none",
    event_attr: str | None = None,
    group_attr: str | None = None,
    grain: str = "month",
    scope_attr: str | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
    order: str = "group",
    limit: int = 1000,
) -> pd.DataFrame:
    """Grouped counts for the chat/guided metrics, computed by the database.

    Columns: group (text; periods are ISO period-start dates), count, and for mode='delay'
    mean_days/median_days/min_days/max_days (non-negative purchase-to-install days, unrounded).
    order='group' sorts by label; 'count' by count descending then label. At most `limit`
    groups are returned, so callers pass their cap + 1 to detect truncation.
    """
    _ensure_flat_mode(config)
    if config.demo_mode:
        return reference_chat_groups(
            _read_demo_csv(config), config, mode, grouping, event_attr, group_attr, grain,
            scope_attr, from_date, to_date, order, limit,
        )
    _validate_db_columns(
        engine, config, _chat_group_attributes(mode, grouping, event_attr, group_attr, scope_attr),
    )
    sql, params = build_chat_group_query(
        config, mode, grouping, event_attr, group_attr, grain, scope_attr, from_date, to_date, order, limit,
    )
    return _finish_chat_groups(_read_aggregate(engine, sql, params), mode, order, limit)


# -- optional market series --------------------------------------------------


def load_market_series(config: AppConfig) -> pd.DataFrame | None:
    """Load the optional IQ_MARKET_CSV (columns month,value); None when not configured.

    Returns month (month-start Timestamp) and value (float), sorted by month.
    Raises ValueError on a malformed file and FileNotFoundError if the path is missing.
    """
    path: Path | None = resolve_market_csv_path(config)
    if path is None:
        return None
    if not path.is_file():
        raise FileNotFoundError(f"Market CSV '{path}' does not exist. Check IQ_MARKET_CSV.")
    raw = pd.read_csv(path)
    raw.columns = [str(name).strip().casefold() for name in raw.columns]
    if not {"month", "value"}.issubset(raw.columns):
        raise ValueError("Market CSV must have the columns: month, value.")
    raw = raw[["month", "value"]].dropna(how="all")
    if raw.empty:
        raise ValueError("Market CSV has no data rows.")
    months = pd.to_datetime(raw["month"], errors="coerce")
    values = pd.to_numeric(raw["value"], errors="coerce")
    if months.isna().any() or values.isna().any():
        raise ValueError("Market CSV needs a valid date in 'month' and a number in 'value' on every row.")
    frame = pd.DataFrame({"month": months.dt.to_period("M").dt.to_timestamp(), "value": values.astype(float)})
    if frame["month"].duplicated().any():
        raise ValueError("Market CSV has duplicate months.")
    return frame.sort_values("month").reset_index(drop=True)
