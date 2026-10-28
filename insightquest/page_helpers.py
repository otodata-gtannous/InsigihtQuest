"""Shared scaffolding for the Streamlit views: filters, source readiness, states and exports."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import pandas as pd
import streamlit as st
from PIL import Image
from sqlalchemy.engine import Engine

from insightquest import config as config_module
from insightquest import data as data_module
from insightquest.analytics import PROXY_LABEL, MarketPredictability
from insightquest.aggregates import assess_markets_from_counts
from insightquest.cache import (
    CACHE_TTL_SECONDS,
    cached_event_counts,
    cached_monthly_purchase_counts,
    clear_data_cache,
    get_cached_engine,
)
from insightquest.config import AppConfig, config_fingerprint
from insightquest.db import check_db_read_only
from insightquest.exports import render_export_buttons
from insightquest.i18n import format_int, t

logger = logging.getLogger(__name__)

FROM_DATE_KEY = "analysis_from_date"
TO_DATE_KEY = "analysis_to_date"
DEFAULT_RANGE_DAYS = 1095  # three years: enough history for Q4 cohorts and Q5 growth comparisons
LOGO_PATH = Path(__file__).resolve().parents[1] / "assets" / "insightquest-logo.png"
DARK_LOGO_PATH = LOGO_PATH.with_name("insightquest-logo-dark.png")
CURRENT_PAGE_KEY = "iq_current_page"

PageBody = Callable[["PageContext"], "dict[str, pd.DataFrame] | None"]


def current_config() -> AppConfig:
    """Always resolve through the module so tests can patch insightquest.config.get_config."""
    return config_module.get_config()


# -- engines, schema and cached row access ------------------------------------


def get_engine(config: AppConfig | None = None) -> Engine | None:
    return get_cached_engine(config or current_config())


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def _source_columns(fingerprint: str, _config: AppConfig) -> tuple[str, ...]:
    return tuple(sorted(data_module.fetch_flat_table_columns(get_cached_engine(_config), _config)))


def get_source_columns(config: AppConfig | None = None) -> tuple[str, ...]:
    config = config or current_config()
    return _source_columns(config_fingerprint(config), config)


def get_source_context() -> tuple[AppConfig, Engine | None, tuple[str, ...]]:
    config = current_config()
    return config, get_cached_engine(config), get_source_columns(config)


_ROW_FETCHERS: dict[str, Callable[..., pd.DataFrame]] = {
    "map": lambda *a, **k: data_module.fetch_install_map_data(*a, **k),
    "diagnostics": lambda *a, **k: data_module.fetch_data_source_diagnostics(*a, **k),
}


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def _cached_rows(fingerprint: str, _config: AppConfig, name: str, args: tuple[Any, ...]) -> pd.DataFrame:
    return _ROW_FETCHERS[name](get_cached_engine(_config), _config, *args)


def cached_map_rows(config: AppConfig, from_date: date, to_date: date, limit: int) -> pd.DataFrame:
    """Row-level read; coordinates have no SQL aggregate, so the map keeps a row limit."""
    return _cached_rows(config_fingerprint(config), config, "map", (from_date, to_date, limit))


def cached_diagnostics(config: AppConfig, from_date: date, to_date: date) -> pd.DataFrame:
    return _cached_rows(config_fingerprint(config), config, "diagnostics", (from_date, to_date))


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def _cached_markets(
    fingerprint: str, _config: AppConfig, dimension: str, from_date: date, to_date: date,
    end_month: pd.Timestamp, is_proxy_date: bool,
) -> MarketPredictability:
    counts = cached_monthly_purchase_counts(_config, dimension, from_date, to_date)
    return assess_markets_from_counts(counts, as_of=end_month, is_proxy=is_proxy_date)


def cached_market_assessment(
    config: AppConfig, dimension: str, from_date: date, to_date: date, end_month: pd.Timestamp,
) -> MarketPredictability:
    """Per-market predictability from SQL monthly counts; cached because the backtests are slow."""
    return _cached_markets(
        config_fingerprint(config), config, dimension, from_date, to_date, end_month, is_proxy(config)
    )


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def _read_only_status(fingerprint: str, _config: AppConfig) -> bool | None:
    return check_db_read_only(get_cached_engine(_config), _config.source_table)


_FRESHNESS: dict[str, datetime] = {}


def refresh_data() -> None:
    """Drop every cached query result so the next render re-reads the source."""
    clear_data_cache()
    for cached in (_source_columns, _cached_rows, _cached_markets, _read_only_status):
        cached.clear()
    _FRESHNESS.clear()


def data_freshness(config: AppConfig) -> datetime:
    """When the cached data was (approximately) loaded; resets when the cache TTL lapses."""
    key = config_fingerprint(config)
    now = datetime.now()
    loaded = _FRESHNESS.get(key)
    if loaded is None or (now - loaded).total_seconds() > config.cache_ttl_seconds:
        loaded = _FRESHNESS[key] = now
    return loaded


# -- source readiness ----------------------------------------------------------


@dataclass(frozen=True)
class QuestionSpec:
    """Mapped fields a business question needs (AppConfig attribute names)."""

    number: int
    page: str
    required: tuple[str, ...]
    core: tuple[str, ...] = ()
    any_of: tuple[str, ...] = ()
    uses_purchase_timing: bool = False


QUESTIONS: dict[int, QuestionSpec] = {
    1: QuestionSpec(1, "views/q1_peaks_lows.py", ("col_sales_date", "col_installed_at"),
                    any_of=("col_sales_date", "col_installed_at"), uses_purchase_timing=True),
    2: QuestionSpec(2, "views/q2_gaps.py",
                    ("col_installed_at", "col_territory", "col_customer", "col_product_type"),
                    core=("col_installed_at",), any_of=("col_territory", "col_customer", "col_product_type")),
    3: QuestionSpec(3, "views/q3_shipping_delay.py", ("col_shipped_at", "col_installed_at"),
                    core=("col_shipped_at", "col_installed_at"), uses_purchase_timing=True),
    4: QuestionSpec(4, "views/q4_installation_rate.py", ("col_sales_date", "col_installed_at"),
                    core=("col_sales_date", "col_installed_at"), uses_purchase_timing=True),
    5: QuestionSpec(5, "views/q5_predictability.py", ("col_sales_date", "col_territory", "col_product_type"),
                    core=("col_sales_date",), any_of=("col_territory", "col_product_type"),
                    uses_purchase_timing=True),
    6: QuestionSpec(6, "views/q6_stock_thresholds.py", ("col_product_type", "col_installed_at"),
                    core=("col_product_type", "col_installed_at")),
}


def field_label(attribute: str) -> str:
    key = f"field.{attribute.removeprefix('col_')}"
    label = t(key)
    return label if label != key else attribute


def env_name(attribute: str) -> str:
    return f"IQ_{attribute.upper()}"


def mapped_status(
    config: AppConfig, source_columns: tuple[str, ...] | set[str], attributes: tuple[str, ...]
) -> dict[str, str]:
    """attribute -> 'ok' | 'unmapped' (no IQ_COL_* value) | 'absent' (mapped column not in the table)."""
    available = {column.casefold() for column in source_columns}
    result: dict[str, str] = {}
    for attribute in attributes:
        column = getattr(config, attribute, "")
        if not column:
            result[attribute] = "unmapped"
        elif column.casefold() not in available:
            result[attribute] = "absent"
        else:
            result[attribute] = "ok"
    return result


def describe_missing(config: AppConfig, statuses: dict[str, str]) -> str:
    parts = []
    for attribute, status in statuses.items():
        if status == "unmapped":
            parts.append(f"{field_label(attribute)} ({env_name(attribute)})")
        elif status == "absent":
            parts.append(f"{field_label(attribute)} ({getattr(config, attribute)!r})")
    return ", ".join(parts)


def question_readiness(
    spec: QuestionSpec, config: AppConfig, source_columns: tuple[str, ...] | set[str]
) -> str:
    """'ready' | 'partial' | 'unavailable' from the configured column mappings."""
    statuses = mapped_status(config, source_columns, spec.required)
    ok = {attribute for attribute, status in statuses.items() if status == "ok"}
    if set(spec.required) <= ok:
        return "ready"
    if spec.core:
        usable = set(spec.core) <= ok and (not spec.any_of or bool(set(spec.any_of) & ok))
    else:
        usable = bool(ok)
    return "partial" if usable else "unavailable"


@dataclass(frozen=True)
class SourceState:
    config: AppConfig
    columns: tuple[str, ...]
    error: bool = False


def load_source(config: AppConfig) -> SourceState:
    try:
        return SourceState(config, get_source_columns(config))
    except Exception:
        logger.exception("Unable to inspect the configured flat-table source.")
        return SourceState(config, (), error=True)


# -- dates ---------------------------------------------------------------------


def _demo_bounds(config: AppConfig) -> tuple[date, date] | None:
    try:
        periods = [
            cached_event_counts(config, event, "day")["period"] for event in ("purchases", "installs")
        ]
        dates = pd.concat([p for p in periods if not p.empty])
    except Exception:
        logger.warning("Could not read demo date bounds.", exc_info=True)
        return None
    if dates.empty:
        return None
    return pd.Timestamp(dates.min()).date(), pd.Timestamp(dates.max()).date()


def latest_data_date(config: AppConfig) -> date:
    """Today in live mode; the latest purchase/install date in demo mode."""
    if config.demo_mode:
        bounds = _demo_bounds(config)
        if bounds:
            return bounds[1]
    return date.today()


def default_date_range(config: AppConfig) -> tuple[date, date]:
    end = latest_data_date(config)
    start = end - timedelta(days=DEFAULT_RANGE_DAYS - 1)
    if config.demo_mode:
        bounds = _demo_bounds(config)
        if bounds:
            start = max(start, bounds[0])
    return start, end


def keep_shared_state() -> None:
    """Re-assign shared widget keys so Streamlit does not drop them on pages that skip the widgets."""
    for key in (FROM_DATE_KEY, TO_DATE_KEY):
        if key in st.session_state:
            st.session_state[key] = st.session_state[key]


def render_date_filters(config: AppConfig | None = None) -> tuple[date, date]:
    """Sidebar date range shared by every page through session_state."""
    config = config or current_config()
    keep_shared_state()
    default_from, default_to = default_date_range(config)
    st.session_state.setdefault(FROM_DATE_KEY, default_from)
    st.session_state.setdefault(TO_DATE_KEY, default_to)
    from_date = st.sidebar.date_input(
        t("sidebar.from_date"), key=FROM_DATE_KEY, help=t("sidebar.from_date_help")
    )
    to_date = st.sidebar.date_input(
        t("sidebar.to_date"), key=TO_DATE_KEY, help=t("sidebar.to_date_help")
    )
    return from_date, to_date


# -- header, notices, states ---------------------------------------------------


def app_page_icon() -> Image.Image:
    return Image.open(LOGO_PATH)


def render_app_logo() -> None:
    st.logo(str(DARK_LOGO_PATH if DARK_LOGO_PATH.is_file() else LOGO_PATH), size="large")


def update_current_page(**info: Any) -> None:
    """Record what the visitor is looking at (question, date range) for the chat widget."""
    page = dict(st.session_state.get(CURRENT_PAGE_KEY) or {})
    page.update({key: value.isoformat() if isinstance(value, date) else value for key, value in info.items()})
    st.session_state[CURRENT_PAGE_KEY] = page


def render_page_header(question: int | None, title: str, description: str) -> None:
    """Short plain-language name, the official question as subheader, then 'what this tells you'."""
    st.title(title)
    if question is not None:
        st.subheader(t(f"question.{question}.title"))
        st.caption(f":blue-background[{t('home.question_badge', number=question)}] {description}")
    elif description:
        st.caption(description)


def warn_if_row_limit_reached(row_count: int, limit: int) -> None:
    if row_count >= limit:
        st.warning(t("state.row_limit_reached", limit=format_int(limit)))


def render_dataset_scope(config: AppConfig) -> None:
    if config.demo_mode:
        return
    if config.sample_installations:
        st.warning(
            f"INSTALLATION SAMPLE: the latest {config.sample_installations:,} installed devices, "
            "ranked by tank installation date with initial device installation date as fallback. "
            "Older and uninstalled devices are excluded. Results are not all-time population totals; "
            "purchase trends, pending counts, and forecasts are selection-biased."
        )
    if config.purchase_date_is_proxy:
        st.info(
            "PurchaseDate is the earliest nondeleted warranty start date, not a confirmed "
            "order or shipment date. Purchase-to-install delay uses this proxy."
        )


def is_proxy(config: AppConfig) -> bool:
    return bool(config.shipped_at_is_proxy or config.purchase_date_is_proxy)


def render_status_banners(config: AppConfig) -> None:
    render_dataset_scope(config)
    if config.demo_mode:
        st.warning(t("state.demo_banner", file=config.demo_csv_path.name))
        hint = demo_mapping_hint(config)
        if hint:
            st.info(hint)
    else:
        st.caption(t("state.source_caption", table=config.source_table))


def demo_mapping_hint(config: AppConfig) -> str:
    """Tell the user which IQ_COL_* variables point outside the demo CSV (e.g. left over from a real .env)."""
    if not config.demo_mode:
        return ""
    try:
        columns = get_source_columns(config)
    except Exception:
        return ""
    available = {column.casefold() for column in columns}
    stray = [
        f"{env_name(attribute)}={getattr(config, attribute)!r}"
        for attribute in vars(config)
        if attribute.startswith("col_") and getattr(config, attribute)
        and getattr(config, attribute).casefold() not in available
    ]
    return t("state.demo_mapping_hint", variables=", ".join(stray)) if stray else ""


def load_config() -> AppConfig | None:
    """The app configuration, or None after showing a friendly error naming the invalid setting."""
    try:
        return current_config()
    except ValueError as error:
        logger.exception("Invalid configuration.")
        message = str(error)
        if "IQ_" not in message:
            message = f"{message} ({t('state.config_check_hint')})"
        st.error(t("state.config_invalid", detail=message))
        return None


def render_analysis_page(title: str, description: str) -> None:
    """Header used by pages that are not data-analysis questions (Ask the data)."""
    config = load_config()
    if config is None:
        st.stop()
        return
    st.title(title)
    st.caption(description)
    render_status_banners(config)


def warn_if_writable(config: AppConfig | None = None) -> None:
    """Warn when the database login can write to the source table (skipped in demo mode).

    A bad configuration is ignored here; every page reports it with a clear message.
    """
    try:
        config = config or current_config()
    except ValueError:
        logger.warning("Read-only check skipped: invalid configuration.", exc_info=True)
        return
    if config.demo_mode:
        return
    try:
        status = _read_only_status(config_fingerprint(config), config)
    except Exception:
        logger.warning("Read-only check could not run.", exc_info=True)
        return
    if status is False:
        st.warning(t("state.read_only_warning"))


def with_dates(frame: pd.DataFrame) -> pd.DataFrame:
    """Copy where midnight timestamps become plain dates (cleaner tables and exports)."""
    result = frame.copy()
    for column in result.columns:
        if pd.api.types.is_datetime64_any_dtype(result[column]):
            result[column] = result[column].dt.date
    return result


def data_table(frame: pd.DataFrame, label_key: str = "common.data_table", **kwargs: Any) -> None:
    """Table fallback for every chart, inside a collapsed expander."""
    with st.expander(t(label_key)):
        st.dataframe(localize_columns(frame), use_container_width=True, hide_index=True, **kwargs)


def export_frame(frame: pd.DataFrame) -> pd.DataFrame:
    return localize_columns(with_dates(frame))


def localize_columns(frame: pd.DataFrame) -> pd.DataFrame:
    """Rename columns through the col.<name> translation keys (unknown names stay as they are)."""
    names = {}
    for column in frame.columns:
        label = t(f"col.{column}")
        if label != f"col.{column}":
            names[column] = label
    return frame.rename(columns=names)


# -- page runner ---------------------------------------------------------------


@dataclass
class PageContext:
    """What a view body receives: config, validated range and helpers for consistent states."""

    config: AppConfig
    columns: tuple[str, ...]
    from_date: date
    to_date: date
    export_page: str
    filters: dict[str, Any] = field(default_factory=dict)

    @property
    def proxy(self) -> bool:
        return is_proxy(self.config)

    def has(self, attribute: str) -> bool:
        return mapped_status(self.config, self.columns, (attribute,))[attribute] == "ok"

    def empty(self) -> None:
        st.info(t("state.no_rows"))

    def check_row_limit(self, row_count: int, limit: int) -> None:
        warn_if_row_limit_reached(row_count, limit)

    def row_limit_input(self, default: int = 50_000, maximum: int = 500_000, key: str = "iq_row_limit") -> int:
        limit = int(
            st.sidebar.number_input(
                t("sidebar.max_rows"), min_value=100, max_value=maximum, value=default, step=1_000,
                help=t("sidebar.max_rows_help"), key=key,
            )
        )
        self.filters[t("sidebar.max_rows")] = limit
        return limit


def run_analysis_page(
    question: int | None,
    body: PageBody,
    *,
    title_key: str | None = None,
    description_key: str | None = None,
    export_page: str,
    requires: tuple[str, ...] = (),
    requires_any: tuple[str, ...] = (),
    uses_purchase_timing: bool = False,
    date_filters: bool = True,
) -> None:
    """Render a standard analysis page: header, notices, filters, readiness, error handling, exports.

    ``body`` receives a PageContext and returns {sheet name: DataFrame} for the export
    buttons (or None). Exceptions are logged and shown as a generic message, never as a
    stack trace.
    """
    config = load_config()
    if config is None:
        st.stop()
        return

    if question is not None:
        title = t(f"page.q{question}")
        description = t(f"question.{question}.desc")
    else:
        title = t(title_key or "app.name")
        description = t(description_key) if description_key else ""
    render_page_header(question, title, description)
    update_current_page(question=question)
    render_status_banners(config)
    if uses_purchase_timing and is_proxy(config):
        st.warning(t("state.proxy_date_warning"))

    st.sidebar.header(t("sidebar.filters"))
    if date_filters:
        from_date, to_date = render_date_filters(config)
        if from_date > to_date:
            st.error(t("state.date_order_error"))
            st.stop()
            return
        update_current_page(from_date=from_date, to_date=to_date)
    else:
        from_date = to_date = date.today()

    source = load_source(config)
    if source.error:
        st.info(t("state.source_not_configured"))
        st.stop()
        return
    if requires:
        statuses = mapped_status(config, source.columns, requires)
        missing = {a: s for a, s in statuses.items() if s != "ok"}
        if missing:
            st.info(t("state.columns_missing", columns=describe_missing(config, missing)))
            st.stop()
            return

    if requires_any:
        statuses = mapped_status(config, source.columns, requires_any)
        if "ok" not in statuses.values():
            st.info(t("state.columns_missing", columns=describe_missing(config, statuses)))
            st.stop()
            return

    context = PageContext(config, source.columns, from_date, to_date, export_page)
    try:
        sheets = body(context)
    except Exception:
        logger.exception("Analysis page %s failed.", export_page)
        st.error(t("state.error_generic", detail=t("state.error_detail")))
        return

    if sheets:
        sheets = {name: frame for name, frame in sheets.items() if frame is not None and not frame.empty}
    if sheets:
        render_export_buttons(
            sheets,
            export_page,
            filters=context.filters,
            date_range=(from_date, to_date) if date_filters else None,
            proxy_label=PROXY_LABEL if uses_purchase_timing and context.proxy else None,
        )
    st.caption(t("state.data_freshness", timestamp=data_freshness(config).strftime("%Y-%m-%d %H:%M")))
