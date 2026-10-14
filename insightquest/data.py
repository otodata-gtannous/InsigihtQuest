from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Engine

from insightquest.config import AppConfig

_COORDINATE_REGEX = re.compile(
    r"^\s*(-?\d+(?:\.\d+)?)\s*[,;]\s*(-?\d+(?:\.\d+)?)\s*$"
)
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_ ]+$")


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
    col_location = _quote_name(config.col_location)
    col_installed_at = _quote_name(config.col_installed_at)
    col_latitude = _quote_name(config.col_latitude)
    col_longitude = _quote_name(config.col_longitude)

    return f"""
        SELECT TOP (:limit)
            {col_device} AS device_name,
            {col_customer} AS customer_name,
            {col_location} AS device_location,
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
    engine: Engine,
    config: AppConfig,
    from_date: date,
    to_date: date,
    limit: int,
) -> pd.DataFrame:
    _ensure_flat_mode(config)
    start_dt, end_dt = _to_datetime_bounds(from_date, to_date)

    if config.custom_map_sql:
        stmt = text(config.custom_map_sql)
    else:
        stmt = text(_build_flat_map_query(config))

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
    engine: Engine,
    config: AppConfig,
    from_date: date,
    to_date: date,
    limit: int,
) -> pd.DataFrame:
    _ensure_flat_mode(config)
    start_dt, end_dt = _to_datetime_bounds(from_date, to_date)

    if config.custom_trend_sql:
        stmt = text(config.custom_trend_sql)
    else:
        stmt = text(_build_flat_trend_query(config))

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
    engine: Engine,
    config: AppConfig,
    from_date: date,
    to_date: date,
) -> pd.DataFrame:
    _ensure_flat_mode(config)
    start_dt = datetime.combine(from_date, time.min)
    end_exclusive = datetime.combine(to_date + timedelta(days=1), time.min)
    table_name = _quote_table_name(config.source_table)
    col_sales_date = _quote_name(config.col_sales_date)
    col_sales_amount = _quote_name(config.col_sales_amount)
    stmt = text(
        f"""
        SELECT
            CAST({col_sales_date} AS date) AS purchase_date,
            SUM(COALESCE(CAST({col_sales_amount} AS bigint), 0)) AS sales
        FROM {table_name}
        WHERE
            {col_sales_date} >= :start_dt
            AND {col_sales_date} < :end_exclusive
        GROUP BY CAST({col_sales_date} AS date)
        ORDER BY purchase_date
        """
    )

    with engine.connect() as connection:
        return pd.read_sql(
            stmt,
            con=connection,
            params={"start_dt": start_dt, "end_exclusive": end_exclusive},
        )


def fetch_install_territory_trend_data(
    engine: Engine,
    config: AppConfig,
    from_date: date,
    to_date: date,
    limit: int,
) -> pd.DataFrame:
    _ensure_flat_mode(config)
    start_dt, end_dt = _to_datetime_bounds(from_date, to_date)
    stmt = text(_build_flat_territory_query(config))

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
    engine: Engine,
    config: AppConfig,
    from_date: date,
    to_date: date,
) -> pd.DataFrame:
    _ensure_flat_mode(config)
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
    with engine.connect() as connection:
        return pd.read_sql(stmt, con=connection, params={"start_dt": start_dt, "end_dt": end_dt})
