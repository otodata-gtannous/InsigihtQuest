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
    col_shipped_at: str
    col_shipping_key: str
    col_customer_size: str
    col_product_type: str
    col_rate_denominator: str
    col_market_indicator: str
    col_market_indicator_at: str
    col_inventory_on_hand: str
    col_inventory_demand: str
    col_inventory_as_of: str
    col_inventory_item: str
    col_replenishment_days: str
    col_reorder_point: str
    col_max_stock: str


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
        col_device=os.getenv("IQ_COL_DEVICE", "Id"),
        col_location=os.getenv("IQ_COL_LOCATION", ""),
        col_installed_at=os.getenv("IQ_COL_INSTALLED_AT", ""),
        col_customer=os.getenv("IQ_COL_CUSTOMER", ""),
        col_latitude=os.getenv("IQ_COL_LATITUDE", "DeviceLat"),
        col_longitude=os.getenv("IQ_COL_LONGITUDE", "DeviceLong"),
        col_territory=os.getenv("IQ_COL_TERRITORY", ""),
        col_sales_date=os.getenv("IQ_COL_SALES_DATE", ""),
        col_sales_amount=os.getenv("IQ_COL_SALES_AMOUNT", ""),
        col_shipped_at=os.getenv("IQ_COL_SHIPPED_AT", ""),
        col_shipping_key=os.getenv("IQ_COL_SHIPPING_KEY", "Id"),
        col_customer_size=os.getenv("IQ_COL_CUSTOMER_SIZE", ""),
        col_product_type=os.getenv("IQ_COL_PRODUCT_TYPE", ""),
        col_rate_denominator=os.getenv("IQ_COL_RATE_DENOMINATOR", ""),
        col_market_indicator=os.getenv("IQ_COL_MARKET_INDICATOR", ""),
        col_market_indicator_at=os.getenv("IQ_COL_MARKET_INDICATOR_AT", ""),
        col_inventory_on_hand=os.getenv("IQ_COL_INVENTORY_ON_HAND", ""),
        col_inventory_demand=os.getenv("IQ_COL_INVENTORY_DEMAND", ""),
        col_inventory_as_of=os.getenv("IQ_COL_INVENTORY_AS_OF", ""),
        col_inventory_item=os.getenv("IQ_COL_INVENTORY_ITEM", ""),
        col_replenishment_days=os.getenv("IQ_COL_REPLENISHMENT_DAYS", ""),
        col_reorder_point=os.getenv("IQ_COL_REORDER_POINT", ""),
        col_max_stock=os.getenv("IQ_COL_MAX_STOCK", ""),
    )
