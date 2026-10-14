from __future__ import annotations

import pandas as pd
import streamlit as st
from sqlalchemy.exc import SQLAlchemyError

from insightquest.analytics import build_segment_periods
from insightquest.data import fetch_flat_table_rows
from insightquest.exports import render_excel_download
from insightquest.page_helpers import (
    get_source_context,
    render_analysis_page,
    render_date_filters,
    show_source_readiness,
    warn_if_row_limit_reached,
)

st.set_page_config(page_title="Territory & Product Mix | InsightQuest", layout="wide")

render_analysis_page(
    "Territory, customer & product mix",
    "Compare installation activity across territories, customer-size segments, and product types.",
)
st.sidebar.header("Analysis filters")
from_date, to_date = render_date_filters()
period = st.sidebar.selectbox("Period", ["Day", "Week", "Month"], index=2)
row_limit = int(
    st.sidebar.number_input(
        "Maximum rows per analysis",
        min_value=100,
        max_value=200_000,
        value=20_000,
        step=100,
    )
)

if from_date > to_date:
    st.error("From date must be earlier than or equal to To date.")
    st.stop()

try:
    config, engine, source_columns = get_source_context()
except (SQLAlchemyError, ValueError) as ex:
    st.error("Unable to inspect the configured flat-table source.")
    st.exception(ex)
    st.stop()

dimensions = [
    ("Territory", "col_territory"),
    ("Customer size", "col_customer_size"),
    ("Product type", "col_product_type"),
]
available_dimensions: list[tuple[str, str]] = []
export_sheets: dict[str, pd.DataFrame] = {}
for label, attribute in dimensions:
    st.subheader(label)
    requirements = {
        "Installation date": "col_installed_at",
        label: attribute,
    }
    if not show_source_readiness(config, source_columns, requirements):
        continue

    try:
        data = fetch_flat_table_rows(
            engine,
            config,
            {"installed_at": "col_installed_at", "segment": attribute},
            date_field="col_installed_at",
            from_date=from_date,
            to_date=to_date,
            limit=row_limit,
        )
        warn_if_row_limit_reached(len(data), row_limit)
    except (SQLAlchemyError, ValueError) as ex:
        st.error(f"Unable to load the {label.lower()} breakdown.")
        st.exception(ex)
        continue

    if data.empty:
        st.info(f"No installation rows are available for {label.lower()} in this period.")
        continue

    series = build_segment_periods(data, "installed_at", "segment", period)
    totals = (
        series.groupby("segment", as_index=False)["count"]
        .sum()
        .sort_values("count", ascending=False)
    )
    available_dimensions.append((label, attribute))
    export_sheets[f"{label} by period"] = series
    export_sheets[f"{label} totals"] = totals
    left, right = st.columns([2, 1])
    with left:
        st.line_chart(
            series,
            x="period_start",
            y="count",
            color="segment",
            use_container_width=True,
        )
    with right:
        st.bar_chart(totals, x="segment", y="count", use_container_width=True)
        st.dataframe(totals, use_container_width=True, hide_index=True)

if not available_dimensions:
    st.warning(
        "No segment breakdown can be calculated yet. Map the installation-date "
        "column and at least one territory, customer-size, or product-type column "
        "to fields in the flat table."
    )
else:
    render_excel_download(export_sheets, "territory-customer-product.xlsx")
    st.caption(
        "These are installation-row counts. Customer-size values are shown as "
        "stored in the source; no size bands or customer classifications are inferred."
    )
