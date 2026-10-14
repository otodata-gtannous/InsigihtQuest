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


def _to_datetime_bounds(from_date: date, to_date: date) -> tuple[datetime, datetime]:
    start_dt = datetime.combine(from_date, time.min)
    end_dt = datetime.combine(to_date, time.max)
    return start_dt, end_dt


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


def fetch_install_map_data(
    engine: Engine,
    config: AppConfig,
    from_date: date,
    to_date: date,
    limit: int,
) -> pd.DataFrame:
    start_dt, end_dt = _to_datetime_bounds(from_date, to_date)

    if config.custom_map_sql:
        stmt = text(config.custom_map_sql)
    else:
        stmt = text(
            """
            WITH device_install AS (
                SELECT
                    d.Id AS device_id,
                    CONCAT('Device-', CAST(d.SerialNumber AS nvarchar(50))) AS device_name,
                    COALESCE(NULLIF(LTRIM(RTRIM(c.Name)), ''), 'Unknown Customer') AS customer_name,
                    COALESCE(
                        NULLIF(LTRIM(RTRIM(l.Name)), ''),
                        NULLIF(LTRIM(RTRIM(l.Address)), ''),
                        'Unknown Location'
                    ) AS device_location,
                    COALESCE(d.InitialInstallDate, d.DetectedInstallDate, ts.CreatedDate) AS installed_at,
                    CAST(l.Latitude AS float) AS latitude,
                    CAST(l.Longitude AS float) AS longitude,
                    ROW_NUMBER() OVER (
                        PARTITION BY d.Id
                        ORDER BY
                            COALESCE(d.InitialInstallDate, d.DetectedInstallDate, ts.CreatedDate) DESC,
                            ts.CreatedDate DESC
                    ) AS rn
                FROM dbo.TankSensors ts
                JOIN dbo.Sensors s ON s.Id = ts.SensorId AND s.IsDeleted = 0
                JOIN dbo.Devices d ON d.Id = s.DeviceId AND d.IsDeleted = 0
                JOIN dbo.Tanks t ON t.Id = ts.TankId AND t.IsDeleted = 0
                JOIN dbo.Locations l ON l.Id = t.LocationId AND l.IsDeleted = 0
                LEFT JOIN dbo.Customers c ON c.Id = l.CustomerId AND c.IsDeleted = 0
                WHERE ts.IsDeleted = 0
            )
            SELECT TOP (:limit)
                device_name,
                customer_name,
                device_location,
                installed_at,
                latitude,
                longitude
            FROM device_install
            WHERE
                rn = 1
                AND installed_at BETWEEN :start_dt AND :end_dt
            ORDER BY installed_at DESC
            """
        )

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
    start_dt, end_dt = _to_datetime_bounds(from_date, to_date)

    if config.custom_trend_sql:
        stmt = text(config.custom_trend_sql)
    else:
        stmt = text(
            """
            WITH device_install AS (
                SELECT
                    d.Id AS device_id,
                    COALESCE(NULLIF(LTRIM(RTRIM(c.Name)), ''), 'Unknown Customer') AS customer_name,
                    COALESCE(d.InitialInstallDate, d.DetectedInstallDate, ts.CreatedDate) AS installed_at,
                    ROW_NUMBER() OVER (
                        PARTITION BY d.Id
                        ORDER BY
                            COALESCE(d.InitialInstallDate, d.DetectedInstallDate, ts.CreatedDate) DESC,
                            ts.CreatedDate DESC
                    ) AS rn
                FROM dbo.TankSensors ts
                JOIN dbo.Sensors s ON s.Id = ts.SensorId AND s.IsDeleted = 0
                JOIN dbo.Devices d ON d.Id = s.DeviceId AND d.IsDeleted = 0
                JOIN dbo.Tanks t ON t.Id = ts.TankId AND t.IsDeleted = 0
                JOIN dbo.Locations l ON l.Id = t.LocationId AND l.IsDeleted = 0
                LEFT JOIN dbo.Customers c ON c.Id = l.CustomerId AND c.IsDeleted = 0
                WHERE ts.IsDeleted = 0
            )
            SELECT TOP (:limit)
                installed_at,
                customer_name
            FROM device_install
            WHERE
                rn = 1
                AND installed_at BETWEEN :start_dt AND :end_dt
            ORDER BY installed_at ASC
            """
        )

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
    from_date: date,
    to_date: date,
) -> pd.DataFrame:
    start_dt = datetime.combine(from_date, time.min)
    end_exclusive = datetime.combine(to_date + timedelta(days=1), time.min)
    stmt = text(
        """
        SELECT
            CAST(o.CreatedDate AS date) AS purchase_date,
            COUNT_BIG(*) AS sales
        FROM dbo.Ownerships o
        INNER JOIN dbo.Devices d ON d.Id = o.DeviceId
        WHERE
            o.IsOwner = 1
            AND o.IsDeleted = 0
            AND o.CreatedDate >= :start_dt
            AND o.CreatedDate < :end_exclusive
        GROUP BY CAST(o.CreatedDate AS date)
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
    from_date: date,
    to_date: date,
    limit: int,
) -> pd.DataFrame:
    start_dt, end_dt = _to_datetime_bounds(from_date, to_date)
    stmt = text(
        """
        WITH owner_company AS (
            SELECT DISTINCT o.DeviceId, c.TerritoryId
            FROM dbo.Ownerships o
            JOIN dbo.Companies c ON c.Id = o.CompanyId AND c.IsDeleted = 0
            WHERE o.IsDeleted = 0 AND o.IsOwner = 1
        ),
        device_install AS (
            SELECT
                d.Id AS device_id,
                COALESCE(d.InitialInstallDate, d.DetectedInstallDate, ts.CreatedDate) AS installed_at,
                COALESCE(NULLIF(LTRIM(RTRIM(tr.Name)), ''), 'Unknown Territory') AS territory_name,
                ROW_NUMBER() OVER (
                    PARTITION BY d.Id
                    ORDER BY
                        COALESCE(d.InitialInstallDate, d.DetectedInstallDate, ts.CreatedDate) DESC,
                        ts.CreatedDate DESC
                ) AS rn
            FROM dbo.TankSensors ts
            JOIN dbo.Sensors s ON s.Id = ts.SensorId AND s.IsDeleted = 0
            JOIN dbo.Devices d ON d.Id = s.DeviceId AND d.IsDeleted = 0
            JOIN dbo.Tanks t ON t.Id = ts.TankId AND t.IsDeleted = 0
            JOIN dbo.Locations l ON l.Id = t.LocationId AND l.IsDeleted = 0
            LEFT JOIN owner_company oc ON oc.DeviceId = d.Id
            LEFT JOIN dbo.Territories tr ON tr.Id = oc.TerritoryId AND tr.IsDeleted = 0
            WHERE ts.IsDeleted = 0
        )
        SELECT TOP (:limit)
            installed_at,
            territory_name
        FROM device_install
        WHERE
            rn = 1
            AND installed_at BETWEEN :start_dt AND :end_dt
        ORDER BY installed_at ASC
        """
    )

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
    from_date: date,
    to_date: date,
) -> pd.DataFrame:
    start_dt, end_dt = _to_datetime_bounds(from_date, to_date)
    stmt = text(
        """
        WITH device_install AS (
            SELECT
                d.Id AS device_id,
                COALESCE(d.InitialInstallDate, d.DetectedInstallDate, ts.CreatedDate) AS installed_at,
                CASE
                    WHEN d.InitialInstallDate IS NOT NULL THEN 'InitialInstallDate'
                    WHEN d.DetectedInstallDate IS NOT NULL THEN 'DetectedInstallDate'
                    ELSE 'TankSensors.CreatedDate'
                END AS install_source,
                CAST(l.Latitude AS float) AS latitude,
                CAST(l.Longitude AS float) AS longitude,
                CASE
                    WHEN c.Id IS NULL THEN 0
                    ELSE 1
                END AS has_customer,
                ROW_NUMBER() OVER (
                    PARTITION BY d.Id
                    ORDER BY
                        COALESCE(d.InitialInstallDate, d.DetectedInstallDate, ts.CreatedDate) DESC,
                        ts.CreatedDate DESC
                ) AS rn
            FROM dbo.TankSensors ts
            JOIN dbo.Sensors s ON s.Id = ts.SensorId AND s.IsDeleted = 0
            JOIN dbo.Devices d ON d.Id = s.DeviceId AND d.IsDeleted = 0
            JOIN dbo.Tanks t ON t.Id = ts.TankId AND t.IsDeleted = 0
            JOIN dbo.Locations l ON l.Id = t.LocationId AND l.IsDeleted = 0
            LEFT JOIN dbo.Customers c ON c.Id = l.CustomerId AND c.IsDeleted = 0
            WHERE ts.IsDeleted = 0
        )
        SELECT
            COUNT(*) AS total_devices_in_range,
            SUM(CASE WHEN latitude IS NOT NULL AND longitude IS NOT NULL THEN 1 ELSE 0 END) AS with_coordinates,
            SUM(CASE WHEN has_customer = 1 THEN 1 ELSE 0 END) AS with_customer,
            SUM(CASE WHEN install_source = 'InitialInstallDate' THEN 1 ELSE 0 END) AS from_initial_install_date,
            SUM(CASE WHEN install_source = 'DetectedInstallDate' THEN 1 ELSE 0 END) AS from_detected_install_date,
            SUM(CASE WHEN install_source = 'TankSensors.CreatedDate' THEN 1 ELSE 0 END) AS from_tanksensor_created_date
        FROM device_install
        WHERE
            rn = 1
            AND installed_at BETWEEN :start_dt AND :end_dt
        """
    )
    with engine.connect() as connection:
        df = pd.read_sql(stmt, con=connection, params={"start_dt": start_dt, "end_dt": end_dt})
    return df
