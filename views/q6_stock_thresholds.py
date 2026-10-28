from __future__ import annotations

import pandas as pd
import streamlit as st

from insightquest import charts
from insightquest.aggregates import inventory_from_summary
from insightquest.analytics import DEFAULT_INVENTORY_PARAMS
from insightquest.analytics.q6_inventory import STATUSES
from insightquest.cache import cached_inventory_summary
from insightquest.charts import STATUS_STYLE
from insightquest.i18n import format_date, format_int, t
from insightquest.page_helpers import PageContext, data_table, export_frame, latest_data_date, run_analysis_page

PARAMETERS = (
    ("lead_time_days", 1, 365, "inventory.lead_time_days"),
    ("safety_days", 0, 180, "inventory.safety_days"),
    ("max_cover_days", 2, 730, "inventory.max_cover_days"),
    ("demand_window_days", 7, 365, "inventory.demand_window_days"),
)


def status_text(status: str) -> str:
    return f"{STATUS_STYLE[status][1]} {t(f'status.{status}')}"


def body(ctx: PageContext) -> dict[str, pd.DataFrame] | None:
    inputs = {
        name: int(st.sidebar.number_input(
            t(label), min_value=low, max_value=high, value=int(DEFAULT_INVENTORY_PARAMS[name]), step=1,
            help=t(f"{label}_help"), key=f"iq_inventory_{name}",
        ))
        for name, low, high, label in PARAMETERS
    }
    as_of = st.sidebar.date_input(
        t("q6.as_of"), value=latest_data_date(ctx.config), help=t("q6.as_of_help"), key="iq_inventory_as_of"
    )
    ctx.filters.update({t(label): inputs[name] for name, _, _, label in PARAMETERS})
    ctx.filters[t("q6.as_of")] = as_of
    ctx.filters[t("q6.caveat_label")] = t("q6.on_hand_note")

    st.info(t("q6.caveat"))
    summary = cached_inventory_summary(ctx.config, as_of, (inputs["demand_window_days"],))
    try:
        result = inventory_from_summary(summary, as_of, **inputs)
    except ValueError as error:
        st.error(t("q6.invalid_params", detail=str(error)))
        return None
    table = result.data
    if table.empty:
        ctx.empty()
        return None

    counts = result.metadata["status_counts"]
    cols = st.columns(len(STATUSES))
    for column, status in zip(cols, STATUSES):
        column.metric(status_text(status), format_int(counts[status]), help=t(f"q6.rule.{status}"))

    shown = table.assign(status=table["status"].map(status_text))
    charts.show(charts.stock_chart(
        table, "device_type", "on_hand", "reorder_point", "max_stock", t("col.device_type"), t("q6.axis_units"),
    ))
    st.caption(t("q6.chart_legend"))
    risky = table[table["status"].isin(["stockout_risk", "reorder"])]
    if risky.empty:
        st.caption(t("q6.summary_ok", window=inputs["demand_window_days"]))
    else:
        first = risky.dropna(subset=["projected_stockout_date"]).sort_values("projected_stockout_date")
        names = ", ".join(risky["device_type"])
        date_text = format_date(first.iloc[0]["projected_stockout_date"]) if not first.empty else "–"
        st.caption(t("q6.summary_risk", names=names, date=date_text, window=inputs["demand_window_days"]))

    shown = shown[[
        "device_type", "status", "on_hand", "demand_per_day", "days_of_cover", "projected_stockout_date",
        "reorder_point", "reorder_level", "max_stock", "excess_units",
    ]]
    shown = shown.assign(
        demand_per_day=shown["demand_per_day"].round(2),
        days_of_cover=shown["days_of_cover"].round(1),
        reorder_point=shown["reorder_point"].round(1),
        reorder_level=shown["reorder_level"].round(1),
        max_stock=shown["max_stock"].round(1),
        excess_units=shown["excess_units"].round(1),
    )
    st.dataframe(export_frame(shown), use_container_width=True, hide_index=True)
    with st.expander(t("q6.rules_title")):
        for status in STATUSES:
            st.markdown(f"**{status_text(status)}** — {t(f'q6.rule.{status}')}")
    data_table(table)
    return {t("export.sheet.stock_status"): export_frame(shown)}


run_analysis_page(
    6, body, export_page="q6_stock_thresholds", requires=("col_product_type", "col_installed_at"),
    date_filters=False,
)
