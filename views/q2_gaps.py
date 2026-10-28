from __future__ import annotations

import numpy as np
import pandas as pd
import streamlit as st

from insightquest import charts
from insightquest.aggregates import breakdown_from_segment_counts
from insightquest.analytics import customer_size_definitions
from insightquest.cache import cached_customer_size_distribution, cached_segment_counts
from insightquest.data import customer_size_labels, customer_size_order
from insightquest.i18n import format_number, t
from insightquest.page_helpers import (
    PageContext,
    data_table,
    describe_missing,
    export_frame,
    mapped_status,
    run_analysis_page,
)

DIMENSIONS = (
    ("territory", "col_territory", "q2.territory"),
    ("customer_size", "col_customer", "q2.customer_size"),
    ("product_type", "col_product_type", "q2.product_type"),
)
SMALL_GROUP_THRESHOLD = 5


def render_dimension(ctx: PageContext, dimension: str, label: str) -> pd.DataFrame | None:
    segments = cached_segment_counts(ctx.config, dimension, ctx.from_date, ctx.to_date)
    companies = order = None
    if dimension == "customer_size":
        companies = cached_customer_size_distribution(ctx.config)
        order = customer_size_order(ctx.config)
    result = breakdown_from_segment_counts(segments, companies, SMALL_GROUP_THRESHOLD, order)
    table = result.data
    if table.empty:
        ctx.empty()
        return None
    overall = result.metadata["overall_installed_rate_pct"]

    cols = st.columns(3)
    cols[0].metric(t("q2.metric_devices"), format_number(result.metadata["overall_devices"], 0),
                   help=t("q2.metric_devices_help"))
    cols[1].metric(t("q2.metric_rate"), f"{format_number(overall, 1)}%", help=t("q2.rate_definition"))
    cols[2].metric(t("q2.metric_groups"), format_number(len(table), 0))

    chart_data = table.assign(
        flag=np.where(table["small_group"], t("q2.small_group"), t("q2.regular_group"))
    )
    charts.show(charts.bar_chart(
        chart_data, "group", "installed_rate_pct", label, t("q2.axis_rate"),
        horizontal=True, color="flag", reference=overall, reference_label=t("q2.overall_rate"),
    ))
    gaps = table[~table["small_group"]].sort_values("gap_vs_overall_pp")
    if not gaps.empty:
        worst, best = gaps.iloc[0], gaps.iloc[-1]
        st.caption(t(
            "q2.summary", worst=worst["group"], worst_rate=format_number(worst["installed_rate_pct"], 1),
            worst_gap=format_number(worst["gap_vs_overall_pp"], 1), best=best["group"],
            best_rate=format_number(best["installed_rate_pct"], 1), overall=format_number(overall, 1),
        ))
    st.caption(t("q2.small_group_note", threshold=SMALL_GROUP_THRESHOLD))

    if dimension == "customer_size":
        legend = customer_size_definitions(
            ctx.config.customer_size_edges, list(customer_size_labels(ctx.config.customer_size_edges))
        ).replace({"label": {"Unassigned": "Unknown"}})
        st.markdown(f"**{t('q2.size_legend')}**")
        st.dataframe(legend[["label", "description"]].rename(
            columns={"label": t("col.label"), "description": t("col.description")}),
            use_container_width=True, hide_index=True)

    shown = table.assign(small_group=np.where(table["small_group"], "⚠ " + t("q2.small_group"), ""))
    if shown["companies"].isna().all():
        shown = shown.drop(columns="companies")
    data_table(shown)
    return export_frame(shown)


def body(ctx: PageContext) -> dict[str, pd.DataFrame] | None:
    st.caption(t("q2.definition"))
    sheets: dict[str, pd.DataFrame] = {}
    tabs = st.tabs([t(label) for _, _, label in DIMENSIONS])
    for tab, (dimension, attribute, label) in zip(tabs, DIMENSIONS):
        with tab:
            status = mapped_status(ctx.config, ctx.columns, (attribute,))
            if status[attribute] != "ok":
                st.info(t("state.columns_missing", columns=describe_missing(ctx.config, status)))
                continue
            frame = render_dimension(ctx, dimension, t(label))
            if frame is not None:
                sheets[t(label)] = frame
    return sheets


run_analysis_page(
    2, body, export_page="q2_gaps", requires=("col_installed_at",),
    requires_any=("col_territory", "col_customer", "col_product_type"),
)
