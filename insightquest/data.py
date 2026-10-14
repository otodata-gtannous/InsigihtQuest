from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

import pandas as pd
from sqlalchemy import inspect, text
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


def _require_engine(engine: Engine | None) -> Engine:
    if engine is None:
        raise ValueError("A database engine is required when demo mode is disabled.")
    return engine


def _read_demo_csv(config: AppConfig) -> pd.DataFrame:
    if not config.demo_csv_path.is_file():
        raise FileNotFoundError(
            f"Demo CSV '{config.demo_csv_path}' does not exist. "
            "Set IQ_DEMO_CSV to a valid CSV path or disable IQ_DEMO_MODE."
        )
    return pd.read_csv(config.demo_csv_path)


def fetch_flat_table_columns(engine: Engine | None, config: AppConfig) -> set[str]:
    """Return column names from the configured flat table without reading rows."""
    _ensure_flat_mode(config)
    if config.demo_mode:
        return set(_read_demo_csv(config).columns)
    if engine is None:
        raise ValueError("A database engine is required when demo mode is disabled.")
    parts = [part.strip() for part in config.source_table.split(".") if part.strip()]
    if len(parts) == 1:
        schema, table = "dbo", parts[0]
    elif len(parts) == 2:
        schema, table = parts
    else:
        raise ValueError(f"Invalid table name format: '{config.source_table}'")

    inspector = inspect(engine)
    if not inspector.has_table(table, schema=schema):
        raise ValueError(f"Configured flat table '{schema}.{table}' does not exist or is not visible.")
    return {column["name"] for column in inspector.get_columns(table, schema=schema)}


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
                end = pd.Timestamp(to_date) + pd.Timedelta(days=1)
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
