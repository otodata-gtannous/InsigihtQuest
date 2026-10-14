from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _as_bool(value: str | None, default: bool = True) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class AppConfig:
    demo_mode: bool
    demo_csv_path: Path
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
    demo_mode = _as_bool(os.getenv("IQ_DEMO_MODE"), default=False)

    def mapped_column(name: str, default: str = "", demo_default: str = "") -> str:
        configured = os.getenv(name, "").strip()
        if configured:
            return configured
        if demo_mode and demo_default:
            return demo_default
        return default

    demo_csv_path = Path(os.getenv("IQ_DEMO_CSV", "demo_data.csv"))
    if not demo_csv_path.is_absolute():
        demo_csv_path = Path(__file__).resolve().parent.parent / demo_csv_path

    return AppConfig(
        demo_mode=demo_mode,
        demo_csv_path=demo_csv_path,
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
        col_device=mapped_column("IQ_COL_DEVICE", "Id", "Id"),
        col_location=mapped_column("IQ_COL_LOCATION", demo_default="DeviceLocation"),
        col_installed_at=mapped_column("IQ_COL_INSTALLED_AT", "TankInstallDate", "TankInstallDate"),
        col_customer=mapped_column("IQ_COL_CUSTOMER", "CompanyId", "CompanyId"),
        col_latitude=mapped_column("IQ_COL_LATITUDE", "DeviceLat", "DeviceLat"),
        col_longitude=mapped_column("IQ_COL_LONGITUDE", "DeviceLong", "DeviceLong"),
        col_territory=mapped_column("IQ_COL_TERRITORY", demo_default="Territory"),
        col_sales_date=mapped_column("IQ_COL_SALES_DATE", "PurchaseDate", "PurchaseDate"),
        col_sales_amount=mapped_column("IQ_COL_SALES_AMOUNT", demo_default="PurchaseAmount"),
        col_shipped_at=mapped_column("IQ_COL_SHIPPED_AT", demo_default="ShipmentDate"),
        col_shipping_key=mapped_column("IQ_COL_SHIPPING_KEY", "Id", "Id"),
        col_customer_size=mapped_column("IQ_COL_CUSTOMER_SIZE", demo_default="CustomerSize"),
        col_product_type=mapped_column("IQ_COL_PRODUCT_TYPE", "DeviceType", "DeviceType"),
        col_rate_denominator=mapped_column("IQ_COL_RATE_DENOMINATOR", demo_default="EligibleUnits"),
        col_market_indicator=mapped_column("IQ_COL_MARKET_INDICATOR", demo_default="MarketIndex"),
        col_market_indicator_at=mapped_column("IQ_COL_MARKET_INDICATOR_AT", demo_default="MarketDate"),
        col_inventory_on_hand=mapped_column("IQ_COL_INVENTORY_ON_HAND", demo_default="InventoryOnHand"),
        col_inventory_demand=mapped_column("IQ_COL_INVENTORY_DEMAND", demo_default="InventoryDailyDemand"),
        col_inventory_as_of=mapped_column("IQ_COL_INVENTORY_AS_OF", demo_default="InventoryAsOf"),
        col_inventory_item=mapped_column("IQ_COL_INVENTORY_ITEM", demo_default="InventoryItem"),
        col_replenishment_days=mapped_column("IQ_COL_REPLENISHMENT_DAYS", demo_default="ReplenishmentDays"),
        col_reorder_point=mapped_column("IQ_COL_REORDER_POINT", demo_default="ReorderPoint"),
        col_max_stock=mapped_column("IQ_COL_MAX_STOCK", demo_default="MaxStock"),
    )
