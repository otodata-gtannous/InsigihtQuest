from __future__ import annotations

import pandas as pd
import streamlit as st
from sqlalchemy.exc import SQLAlchemyError

from insightquest.analytics import build_inventory_risk
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
    page_title="Inventory Risk | InsightQuest",
    page_icon=app_page_icon(),
    layout="wide",
)

render_analysis_page(
    "Inventory risk",
    "Identify items that may fall below reorder levels during replenishment lead time or exceed maximum stock.",
)
st.sidebar.header("Snapshot filters")
from_date, to_date = render_date_filters(default_days=30)
row_limit = int(
    st.sidebar.number_input(
        "Maximum snapshot rows",
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
    "Inventory item or SKU": "col_inventory_item",
    "Inventory snapshot date": "col_inventory_as_of",
    "On-hand quantity": "col_inventory_on_hand",
    "Daily demand in matching units": "col_inventory_demand",
    "Replenishment lead time in days": "col_replenishment_days",
    "Reorder point": "col_reorder_point",
    "Maximum stock threshold": "col_max_stock",
}
if not show_source_readiness(config, source_columns, requirements):
    st.warning(
        "Stockout and overstock risks are withheld until every field is mapped "
        "and the quantity units and thresholds are confirmed."
    )
    st.stop()

try:
    snapshots = fetch_flat_table_rows(
        engine,
        config,
        {
            "item_name": "col_inventory_item",
            "inventory_as_of": "col_inventory_as_of",
            "inventory_on_hand": "col_inventory_on_hand",
            "inventory_daily_demand": "col_inventory_demand",
            "replenishment_days": "col_replenishment_days",
            "reorder_point": "col_reorder_point",
            "max_stock": "col_max_stock",
        },
        date_field="col_inventory_as_of",
        from_date=from_date,
        to_date=to_date,
        limit=row_limit,
    )
    warn_if_row_limit_reached(len(snapshots), row_limit)
except (SQLAlchemyError, ValueError) as ex:
    st.error("Unable to load inventory snapshots from the flat table.")
    st.exception(ex)
    st.stop()

if snapshots.empty:
    st.info("No inventory snapshots are available for the selected date range.")
    st.stop()

snapshots["inventory_as_of"] = pd.to_datetime(
    snapshots["inventory_as_of"], errors="coerce"
)
snapshots = snapshots.dropna(subset=["inventory_as_of", "item_name"])
latest_dates = snapshots.groupby("item_name")["inventory_as_of"].transform("max")
latest = snapshots.loc[snapshots["inventory_as_of"].eq(latest_dates)].copy()
value_columns = [
    "inventory_on_hand",
    "inventory_daily_demand",
    "replenishment_days",
    "reorder_point",
    "max_stock",
]
ambiguous_items = (
    latest.groupby("item_name")[value_columns]
    .nunique(dropna=False)
    .gt(1)
    .any(axis=1)
)
ambiguous_names = set(ambiguous_items[ambiguous_items].index)
if ambiguous_names:
    st.warning(
        f"{len(ambiguous_names):,} items have conflicting rows at their latest "
        "snapshot and are excluded until the flat-table grain is clarified."
    )
    latest = latest[~latest["item_name"].isin(ambiguous_names)]
latest = latest.drop_duplicates(
    subset=["item_name", "inventory_as_of", *value_columns]
)

risk = build_inventory_risk(latest)
if risk.empty:
    st.warning(
        "No valid inventory rows remain. Check that quantities and lead times are "
        "non-negative and maximum stock is at least the reorder point."
    )
    st.stop()

as_of = latest["inventory_as_of"].max().strftime("%Y-%m-%d")
st.caption(f"Latest included inventory snapshot: {as_of}")
counts = risk["risk"].value_counts()
cols = st.columns(3)
cols[0].metric("Stockout risk", f"{int(counts.get('Stockout risk', 0)):,}")
cols[1].metric("Overstock risk", f"{int(counts.get('Overstock risk', 0)):,}")
cols[2].metric("Within thresholds", f"{int(counts.get('Within thresholds', 0)):,}")

st.subheader("On-hand quantity versus thresholds")
st.bar_chart(
    risk.set_index("item")[["on_hand", "reorder_point", "max_stock"]],
    use_container_width=True,
)
st.subheader("Risk details")
render_excel_download({"Inventory risk": risk}, "inventory-risk.xlsx")
st.dataframe(risk, use_container_width=True, hide_index=True)
st.caption(
    "Projected on-hand is current on-hand minus daily demand multiplied by "
    "replenishment lead time. Demand, inventory, reorder, and maximum quantities "
    "must use the same units; demand must be a daily rate."
)
