from __future__ import annotations

import pandas as pd
import streamlit as st
from sqlalchemy.exc import SQLAlchemyError

from insightquest.analytics import build_installation_periods, build_sales_periods
from insightquest.data import fetch_flat_table_rows
from insightquest.exports import render_excel_download
from insightquest.page_helpers import (
    app_page_icon,
    get_source_context,
    render_analysis_page,
    render_date_filters,
    show_source_readiness,
    warn_if_row_limit_reached,
)

st.set_page_config(
    page_title="Purchases & Installations | InsightQuest",
    page_icon=app_page_icon(),
    layout="wide",
)

render_analysis_page(
    "Purchases & installations",
    "Compare purchase measures and installation counts to find peaks, lows, and timing patterns.",
)
st.sidebar.header("Analysis filters")
from_date, to_date = render_date_filters()
period = st.sidebar.selectbox("Period", ["Day", "Week", "Month"], index=1)
row_limit = int(
    st.sidebar.number_input(
        "Maximum rows per series",
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

purchase_requirements = {
    "Purchase date": "col_sales_date",
    "Purchase measure": "col_sales_amount",
}
install_requirements = {"Installation date": "col_installed_at"}
purchase_ready = show_source_readiness(config, source_columns, purchase_requirements)
install_ready = show_source_readiness(config, source_columns, install_requirements)
if not purchase_ready and not install_ready:
    st.stop()

try:
    purchases = pd.DataFrame()
    installations = pd.DataFrame()
    if purchase_ready:
        purchases = fetch_flat_table_rows(
            engine,
            config,
            {"purchase_date": "col_sales_date", "sales": "col_sales_amount"},
            date_field="col_sales_date",
            from_date=from_date,
            to_date=to_date,
            limit=row_limit,
        )
        warn_if_row_limit_reached(len(purchases), row_limit)
    if install_ready:
        installations = fetch_flat_table_rows(
            engine,
            config,
            {"installed_at": "col_installed_at"},
            date_field="col_installed_at",
            from_date=from_date,
            to_date=to_date,
            limit=row_limit,
        )
        warn_if_row_limit_reached(len(installations), row_limit)
except (SQLAlchemyError, ValueError) as ex:
    st.error("Unable to load purchase or installation data from the flat table.")
    st.exception(ex)
    st.stop()

series: list[str] = []
if purchase_ready:
    purchases_by_period = build_sales_periods(
        purchases, period, from_date, to_date
    ).rename(columns={"sales": "purchases"})
    series.append("purchases")
if install_ready:
    installations_by_period = build_installation_periods(
        installations, period, from_date, to_date
    )
    series.append("installs")

if purchase_ready and install_ready:
    comparison = purchases_by_period.merge(
        installations_by_period, on="period_start", how="outer"
    ).fillna({"purchases": 0, "installs": 0})
elif purchase_ready:
    comparison = purchases_by_period
else:
    comparison = installations_by_period

if comparison.empty:
    st.info("No periods are available for the selected date range.")
    st.stop()

metric_titles = {
    "purchases": ("Purchase measure", 2),
    "installs": ("Installations", 0),
}
metrics = st.columns(len(series) * 2)
for index, measure in enumerate(series):
    title, decimals = metric_titles[measure]
    peak = comparison.loc[comparison[measure].idxmax()]
    low = comparison.loc[comparison[measure].idxmin()]
    metrics[index * 2].metric(
        f"Peak {title.lower()} ({period.lower()})",
        f"{peak[measure]:,.{decimals}f}",
        pd.Timestamp(peak["period_start"]).strftime("%Y-%m-%d"),
    )
    metrics[index * 2 + 1].metric(
        f"Low {title.lower()} ({period.lower()})",
        f"{low[measure]:,.{decimals}f}",
        pd.Timestamp(low["period_start"]).strftime("%Y-%m-%d"),
    )

st.caption(
    "Purchase measure is the sum of the configured purchase amount column; "
    "installations are flat-table rows with an installation date. Compare their "
    "magnitudes only if both source fields represent comparable units."
)
series_title = " and ".join(metric_titles[measure][0] for measure in series)
st.subheader(f"{series_title} by {period.lower()}")
render_excel_download(
    {"Period summary": comparison},
    "purchases-and-installations.xlsx",
)
st.line_chart(
    comparison,
    x="period_start",
    y=series,
    use_container_width=True,
)
with st.expander("Highest and lowest periods"):
    for measure in series:
        title = metric_titles[measure][0]
        st.markdown(f"**Highest {title.lower()} periods**")
        st.dataframe(
            comparison.nlargest(10, measure),
            use_container_width=True,
            hide_index=True,
        )
        st.markdown(f"**Lowest {title.lower()} periods**")
        st.dataframe(
            comparison.nsmallest(10, measure),
            use_container_width=True,
            hide_index=True,
        )
