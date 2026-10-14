from __future__ import annotations

from datetime import date, timedelta

import streamlit as st
from sqlalchemy.engine import Engine

from insightquest.config import AppConfig, get_config
from insightquest.data import fetch_flat_table_columns
from insightquest.db import create_db_engine


@st.cache_resource
def get_engine() -> Engine:
    return create_db_engine(get_config())


@st.cache_data(ttl=300, show_spinner=False)
def get_source_columns() -> tuple[str, ...]:
    return tuple(sorted(fetch_flat_table_columns(get_engine(), get_config())))


def get_source_context() -> tuple[AppConfig, Engine, tuple[str, ...]]:
    config = get_config()
    return config, get_engine(), get_source_columns()


def missing_source_fields(
    config: AppConfig,
    source_columns: tuple[str, ...] | set[str],
    requirements: dict[str, str],
) -> list[str]:
    available = {column.casefold() for column in source_columns}
    missing: list[str] = []
    for label, attribute in requirements.items():
        column = getattr(config, attribute, "")
        if not column:
            missing.append(f"{label}: no column mapping configured ({attribute})")
        elif column.casefold() not in available:
            missing.append(f"{label}: mapped column '{column}' is not in the flat table")
    return missing


def show_source_readiness(
    config: AppConfig,
    source_columns: tuple[str, ...] | set[str],
    requirements: dict[str, str],
) -> bool:
    missing = missing_source_fields(config, source_columns, requirements)
    if missing:
        st.info("This analysis is not available until the flat table has these fields:")
        st.code("\n".join(missing))
        return False
    return True


def warn_if_row_limit_reached(row_count: int, limit: int) -> None:
    if row_count >= limit:
        st.warning(
            "The query returned the configured row limit. Results may be truncated; "
            "increase the limit before interpreting totals or trends."
        )


def render_analysis_page(title: str, description: str) -> None:
    st.title(title)
    st.caption(description)
    st.caption(f"Source: `{get_config().source_table}` (configured flat table)")


def render_date_filters(default_days: int = 365) -> tuple[date, date]:
    end_date = date.today()
    start_date = end_date - timedelta(days=default_days)
    from_date = st.sidebar.date_input(
        "From date", start_date, key="analysis_from_date"
    )
    to_date = st.sidebar.date_input("To date", end_date, key="analysis_to_date")
    return from_date, to_date
