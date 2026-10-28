from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

DEFAULT_CUSTOMER_SIZE_EDGES: tuple[int, ...] = (10, 100)
# Secrets must never reach cache keys, logs, or fingerprints.
SECRET_FIELDS = frozenset({"db_password", "openai_api_key"})


def _as_bool(value: str | None, default: bool = True) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _positive_int(name: str, default: int, maximum: int | None = None) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a positive integer.") from None
    if value < 1 or (maximum is not None and value > maximum):
        limit = f" no greater than {maximum}" if maximum is not None else ""
        raise ValueError(f"{name} must be a positive integer{limit}.")
    return value


def parse_customer_size_edges(raw: str | None) -> tuple[int, ...]:
    """Parse '10,100' into (10, 100); edges must be positive and strictly ascending."""
    text = (raw or "").strip()
    if not text:
        return DEFAULT_CUSTOMER_SIZE_EDGES
    try:
        edges = tuple(int(part.strip()) for part in text.split(","))
    except ValueError:
        raise ValueError(
            "IQ_CUSTOMER_SIZE_EDGES must be comma-separated integers, e.g. '10,100'."
        ) from None
    if any(edge < 1 for edge in edges):
        raise ValueError("IQ_CUSTOMER_SIZE_EDGES values must be positive integers.")
    if any(later <= earlier for earlier, later in zip(edges, edges[1:])):
        raise ValueError("IQ_CUSTOMER_SIZE_EDGES values must be strictly ascending.")
    return edges


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
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    ai_allow_external_data: bool = False
    ai_db_read_only_confirmed: bool = False
    sample_installations: int = 0
    purchase_date_is_proxy: bool = False
    shipped_at_is_proxy: bool = True
    customer_size_edges: tuple[int, ...] = DEFAULT_CUSTOMER_SIZE_EDGES
    market_csv_path: str = ""
    cache_ttl_seconds: int = 300
    db_query_timeout_seconds: int = 60
    db_pool_size: int = 5
    db_pool_timeout_seconds: int = 30


def get_config() -> AppConfig:
    demo_mode = _as_bool(os.getenv("IQ_DEMO_MODE"), default=False)
    sample_installations = int(os.getenv("IQ_SAMPLE_INSTALLATIONS", "0"))
    if sample_installations < 0:
        raise ValueError("IQ_SAMPLE_INSTALLATIONS must be zero or a positive integer.")

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
        sample_installations=sample_installations,
        purchase_date_is_proxy=_as_bool(os.getenv("IQ_PURCHASE_DATE_IS_PROXY"), default=False),
        shipped_at_is_proxy=_as_bool(os.getenv("IQ_SHIPPED_AT_IS_PROXY"), default=True),
        customer_size_edges=parse_customer_size_edges(os.getenv("IQ_CUSTOMER_SIZE_EDGES")),
        market_csv_path=os.getenv("IQ_MARKET_CSV", "").strip(),
        cache_ttl_seconds=_positive_int("IQ_CACHE_TTL_SECONDS", 300),
        db_query_timeout_seconds=_positive_int("IQ_DB_QUERY_TIMEOUT_SECONDS", 60),
        db_pool_size=_positive_int("IQ_DB_POOL_SIZE", 5, maximum=100),
        db_pool_timeout_seconds=_positive_int("IQ_DB_POOL_TIMEOUT_SECONDS", 30),
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
        col_location=mapped_column("IQ_COL_LOCATION"),
        col_installed_at=mapped_column("IQ_COL_INSTALLED_AT", "TankInstallDate", "TankInstallDate"),
        col_customer=mapped_column("IQ_COL_CUSTOMER", "CompanyId", "CompanyId"),
        col_latitude=mapped_column("IQ_COL_LATITUDE", "Latitude", "Latitude"),
        col_longitude=mapped_column("IQ_COL_LONGITUDE", "Longitude", "Longitude"),
        col_territory=mapped_column("IQ_COL_TERRITORY", "Territory", "Territory"),
        col_sales_date=mapped_column("IQ_COL_SALES_DATE", "PurchaseDate", "PurchaseDate"),
        col_sales_amount=mapped_column("IQ_COL_SALES_AMOUNT"),
        col_shipped_at=mapped_column("IQ_COL_SHIPPED_AT", "PurchaseDate", "PurchaseDate"),
        col_shipping_key=mapped_column("IQ_COL_SHIPPING_KEY", "Id", "Id"),
        col_customer_size=mapped_column("IQ_COL_CUSTOMER_SIZE"),
        col_product_type=mapped_column("IQ_COL_PRODUCT_TYPE", "DeviceType", "DeviceType"),
        col_rate_denominator=mapped_column("IQ_COL_RATE_DENOMINATOR"),
        col_market_indicator=mapped_column("IQ_COL_MARKET_INDICATOR"),
        col_market_indicator_at=mapped_column("IQ_COL_MARKET_INDICATOR_AT"),
        col_inventory_on_hand=mapped_column("IQ_COL_INVENTORY_ON_HAND"),
        col_inventory_demand=mapped_column("IQ_COL_INVENTORY_DEMAND"),
        col_inventory_as_of=mapped_column("IQ_COL_INVENTORY_AS_OF"),
        col_inventory_item=mapped_column("IQ_COL_INVENTORY_ITEM"),
        col_replenishment_days=mapped_column("IQ_COL_REPLENISHMENT_DAYS"),
        col_reorder_point=mapped_column("IQ_COL_REORDER_POINT"),
        col_max_stock=mapped_column("IQ_COL_MAX_STOCK"),
        openai_api_key=(
            os.getenv("IQ_OPENAI_API_KEY", "").strip()
            or os.getenv("OPENAI_API_KEY", "").strip()
        ),
        openai_model=os.getenv("IQ_OPENAI_MODEL", "").strip() or "gpt-4o-mini",
        ai_allow_external_data=_as_bool(os.getenv("IQ_AI_ALLOW_EXTERNAL_DATA"), default=False),
        ai_db_read_only_confirmed=_as_bool(os.getenv("IQ_AI_DB_READ_ONLY_CONFIRMED"), default=False),
    )


def resolve_market_csv_path(config: AppConfig) -> Path | None:
    """Return the optional market-series CSV path (relative paths resolve from the repo root)."""
    raw = (config.market_csv_path or "").strip()
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_absolute() else Path(__file__).resolve().parent.parent / path


def config_fingerprint(config: AppConfig) -> str:
    """Stable, secret-free digest of everything that changes query results."""
    payload = {
        name: str(value)
        for name, value in sorted(vars(config).items())
        if name not in SECRET_FIELDS
    }
    for name, path in (
        ("demo_csv_mtime_ns", config.demo_csv_path if config.demo_mode else None),
        ("market_csv_mtime_ns", resolve_market_csv_path(config)),
    ):
        try:
            payload[name] = str(path.stat().st_mtime_ns) if path else ""
        except OSError:
            payload[name] = ""
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
