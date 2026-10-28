"""Streamlit caching for the aggregate fetchers in insightquest.data.

data.py stays free of Streamlit; pages call the cached_* wrappers here. Cache keys
use a secret-free config fingerprint plus hashable call arguments; the config itself
(which holds secrets) is passed in an underscore-prefixed, unhashed parameter.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Callable, Iterable

import pandas as pd
import streamlit as st
from sqlalchemy.engine import Engine

from insightquest import data
from insightquest.config import AppConfig, config_fingerprint, get_config
from insightquest.db import create_db_engine

try:
    CACHE_TTL_SECONDS = get_config().cache_ttl_seconds
except ValueError:
    CACHE_TTL_SECONDS = 300

_FETCHERS: dict[str, Callable[..., pd.DataFrame]] = {
    "event_counts": lambda *a, **k: data.fetch_event_counts(*a, **k),
    "segment_counts": lambda *a, **k: data.fetch_segment_counts(*a, **k),
    "customer_size_distribution": lambda *a, **k: data.fetch_customer_size_distribution(*a, **k),
    "shipping_lag_distribution": lambda *a, **k: data.fetch_shipping_lag_distribution(*a, **k),
    "inventory_summary": lambda *a, **k: data.fetch_inventory_summary(*a, **k),
    "purchase_install_cohorts": lambda *a, **k: data.fetch_purchase_install_cohorts(*a, **k),
    "lag_by_cohort": lambda *a, **k: data.fetch_lag_by_cohort(*a, **k),
}


@st.cache_resource(show_spinner=False)
def _cached_engine(fingerprint: str, _config: AppConfig) -> Engine:
    return create_db_engine(_config)


def get_cached_engine(config: AppConfig) -> Engine | None:
    """Shared pooled engine (None in demo mode), created once per config fingerprint."""
    if config.demo_mode:
        return None
    return _cached_engine(config_fingerprint(config), config)


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def _cached_call(
    fingerprint: str,
    _config: AppConfig,
    name: str,
    args: tuple[Any, ...],
    kwargs: tuple[tuple[str, Any], ...],
) -> pd.DataFrame:
    return _FETCHERS[name](get_cached_engine(_config), _config, *args, **dict(kwargs))


def _call(config: AppConfig, name: str, *args: Any, **kwargs: Any) -> pd.DataFrame:
    hashable_kwargs = tuple(sorted(
        (key, tuple(value) if isinstance(value, (list, set, frozenset)) else value)
        for key, value in kwargs.items()
    ))
    return _cached_call(config_fingerprint(config), config, name, args, hashable_kwargs)


def clear_data_cache() -> None:
    """Drop cached query results and schema lookups (engines are kept)."""
    _cached_call.clear()
    data.clear_column_cache()


def clear_engine_cache() -> None:
    """Dispose and forget pooled engines, e.g. after changing credentials."""
    _cached_engine.clear()


def cached_event_counts(
    config: AppConfig,
    event: str,
    grain: str = "month",
    from_date: date | None = None,
    to_date: date | None = None,
    group_by: str | None = None,
) -> pd.DataFrame:
    return _call(config, "event_counts", event, grain, from_date, to_date, group_by)


def cached_monthly_purchase_counts(
    config: AppConfig,
    group_by: str = "territory",
    from_date: date | None = None,
    to_date: date | None = None,
) -> pd.DataFrame:
    return _call(config, "event_counts", "purchases", "month", from_date, to_date, group_by)


def cached_segment_counts(
    config: AppConfig,
    dimension: str,
    from_date: date | None = None,
    to_date: date | None = None,
    top_n: int | None = None,
) -> pd.DataFrame:
    return _call(config, "segment_counts", dimension, from_date, to_date, top_n)


def cached_customer_size_distribution(config: AppConfig) -> pd.DataFrame:
    return _call(config, "customer_size_distribution")


def cached_shipping_lag_distribution(
    config: AppConfig, from_date: date | None = None, to_date: date | None = None,
) -> pd.DataFrame:
    return _call(config, "shipping_lag_distribution", from_date, to_date)


def cached_inventory_summary(
    config: AppConfig, as_of: date | None = None, trailing_days: Iterable[int] = (30, 90),
) -> pd.DataFrame:
    # Resolve "today" here so a cached entry never serves yesterday's window.
    return _call(config, "inventory_summary", as_of or date.today(), tuple(trailing_days))


def cached_purchase_install_cohorts(
    config: AppConfig,
    from_date: date | None = None,
    to_date: date | None = None,
    group_by: str | None = None,
    as_of: date | None = None,
) -> pd.DataFrame:
    return _call(config, "purchase_install_cohorts", from_date, to_date, group_by, as_of)


def cached_lag_by_cohort(
    config: AppConfig,
    from_date: date | None = None,
    to_date: date | None = None,
    basis: str = "shipped",
    as_of: date | None = None,
) -> pd.DataFrame:
    return _call(config, "lag_by_cohort", from_date, to_date, basis, as_of)
