from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _as_bool(value: str | None, default: bool = True) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class AppConfig:
    db_server: str
    db_port: int
    db_name: str
    db_auth_mode: str
    db_user: str
    db_password: str
    db_driver: str
    db_trust_cert: bool
    source_mode: str
    source_table: str
    col_device: str
    col_location: str
    col_installed_at: str
    col_customer: str
    col_latitude: str
    col_longitude: str
    col_territory: str
    col_sales_date: str
    col_sales_amount: str
    custom_map_sql: str | None
    custom_trend_sql: str | None


def get_config() -> AppConfig:
    return AppConfig(
        db_server=os.getenv("IQ_DB_SERVER", "localhost"),
        db_port=int(os.getenv("IQ_DB_PORT", "1433")),
        db_name=os.getenv("IQ_DB_NAME", "otodatawebportal_dev"),
        db_auth_mode=os.getenv("IQ_DB_AUTH_MODE", "auto"),
        db_user=os.getenv("IQ_DB_USER", "sa"),
        db_password=os.getenv("IQ_DB_PASSWORD", ""),
        db_driver=os.getenv("IQ_DB_DRIVER", "ODBC Driver 18 for SQL Server"),
        db_trust_cert=_as_bool(os.getenv("IQ_DB_TRUST_CERT"), default=True),
        source_mode=os.getenv("IQ_SOURCE_MODE", "flat-table"),
        source_table=os.getenv("IQ_SOURCE_TABLE", "dbo.Insight Project"),
        col_device=os.getenv("IQ_COL_DEVICE", "DeviceName"),
        col_location=os.getenv("IQ_COL_LOCATION", "DeviceLocation"),
        col_installed_at=os.getenv("IQ_COL_INSTALLED_AT", "InstalledAt"),
        col_customer=os.getenv("IQ_COL_CUSTOMER", "CustomerName"),
        col_latitude=os.getenv("IQ_COL_LATITUDE", "DeviceLat"),
        col_longitude=os.getenv("IQ_COL_LONGITUDE", "DeviceLong"),
        col_territory=os.getenv("IQ_COL_TERRITORY", "TerritoryName"),
        col_sales_date=os.getenv("IQ_COL_SALES_DATE", "SalesDate"),
        col_sales_amount=os.getenv("IQ_COL_SALES_AMOUNT", "SalesAmount"),
        custom_map_sql=os.getenv("IQ_MAP_SQL") or None,
        custom_trend_sql=os.getenv("IQ_TREND_SQL") or None,
    )
