from __future__ import annotations

import pandas as pd
import streamlit as st
from sqlalchemy.exc import SQLAlchemyError

from insightquest.analytics import build_shipping_lag
from insightquest.data import fetch_flat_table_rows
from insightquest.exports import render_excel_download
from insightquest.page_helpers import (
    get_source_context,
    render_analysis_page,
    render_date_filters,
    show_source_readiness,
    warn_if_row_limit_reached,
)

st.set_page_config(page_title="Shipping to Installation | InsightQuest", layout="wide")

render_analysis_page(
    "Shipping to installation",
    "Measure elapsed time from shipment to installation using paired dates on each flat-table record.",
)
st.sidebar.header("Analysis filters")
from_date, to_date = render_date_filters()
row_limit = int(
    st.sidebar.number_input(
        "Maximum rows",
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

requirements = {
    "Shipment date": "col_shipped_at",
    "Installation date": "col_installed_at",
    "Stable shipment/device key": "col_shipping_key",
}
if not show_source_readiness(config, source_columns, requirements):
    st.stop()

try:
    data = fetch_flat_table_rows(
        engine,
        config,
        {
            "record_key": "col_shipping_key",
            "shipping_date": "col_shipped_at",
            "installed_at": "col_installed_at",
        },
        date_field="col_shipped_at",
        from_date=from_date,
        to_date=to_date,
        limit=row_limit,
    )
    warn_if_row_limit_reached(len(data), row_limit)
except (SQLAlchemyError, ValueError) as ex:
    st.error("Unable to load shipment and installation dates from the flat table.")
    st.exception(ex)
    st.stop()

duplicate_keys = int(data["record_key"].duplicated(keep=False).sum())
if duplicate_keys:
    st.error(
        f"{duplicate_keys:,} rows share a shipment/device key. "
        "The source grain is ambiguous, so the lag analysis is withheld to avoid double-counting."
    )
    st.stop()

lags, summary = build_shipping_lag(data)
render_excel_download(
    {
        "Valid shipment lags": lags,
        "Source records": data.rename(
            columns={
                "record_key": "Record key",
                "shipping_date": "Shipment date",
                "installed_at": "Installation date",
            }
        ),
        "Summary": pd.DataFrame(
            [
                {
                    "Matched records": summary.matched_rows,
                    "Missing/invalid dates": summary.missing_date_rows,
                    "Installation before shipment": summary.negative_lag_rows,
                    "Median days": summary.median_days,
                    "90th percentile days": summary.percentile_90_days,
                }
            ]
        ),
    },
    "shipping-to-installation.xlsx",
)
if summary.matched_rows == 0:
    st.warning("No records have both valid shipment and installation dates.")
    st.metric("Rows with a missing/invalid date", f"{summary.missing_date_rows:,}")
    st.metric("Installation before shipment", f"{summary.negative_lag_rows:,}")
    st.stop()

metrics = st.columns(4)
metrics[0].metric("Matched records", f"{summary.matched_rows:,}")
metrics[1].metric("Median days", f"{summary.median_days:.1f}")
metrics[2].metric("90th percentile days", f"{summary.percentile_90_days:.1f}")
metrics[3].metric(
    "Missing installation date",
    f"{summary.missing_date_rows:,}",
    help="Shipment dates are filtered to the selected range; this counts rows with a missing or invalid installation date.",
)
st.metric("Installation before shipment", f"{summary.negative_lag_rows:,}")
st.caption(
    "Lag is calculated per flat-table row as installation date minus shipment date. "
    "Confirm that each row represents one matched shipment/device record before interpreting it."
)

bin_count = min(20, max(1, int(lags["lag_days"].nunique())))
histogram = (
    pd.cut(lags["lag_days"], bins=bin_count, include_lowest=True)
    .value_counts(sort=False)
    .rename_axis("Days from shipment to installation")
    .rename("Records")
    .reset_index()
)
histogram["Days from shipment to installation"] = histogram[
    "Days from shipment to installation"
].astype(str)
st.subheader("Shipping-to-installation delay distribution")
st.bar_chart(
    histogram,
    x="Days from shipment to installation",
    y="Records",
    use_container_width=True,
)
