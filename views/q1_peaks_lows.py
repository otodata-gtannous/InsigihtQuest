from __future__ import annotations

import pandas as pd
import streamlit as st

from insightquest import charts
from insightquest.aggregates import PERIOD_GRAIN, compare_event_series, counts_to_series
from insightquest.analytics import rank_peaks_and_lows
from insightquest.cache import cached_event_counts
from insightquest.i18n import format_date, format_int, format_number, t
from insightquest.page_helpers import PageContext, data_table, export_frame, run_analysis_page, with_dates

MEASURES = (("purchases", "col_sales_date"), ("installs", "col_installed_at"))


def body(ctx: PageContext) -> dict[str, pd.DataFrame] | None:
    period = st.sidebar.selectbox(
        t("common.period"), ["Day", "Week", "Month"], index=2,
        format_func=lambda value: t(f"period.{value.lower()}"), help=t("common.period_help"),
    )
    top_n = st.sidebar.slider(t("q1.top_n"), 1, 10, 5, help=t("q1.top_n_help"))
    ctx.filters[t("common.period")] = t(f"period.{period.lower()}")
    ctx.filters[t("q1.top_n")] = top_n

    series: dict[str, pd.DataFrame] = {}
    for measure, attribute in MEASURES:
        if ctx.has(attribute):
            counts = cached_event_counts(ctx.config, measure, PERIOD_GRAIN[period], ctx.from_date, ctx.to_date)
            series[measure] = counts_to_series(counts, period, ctx.from_date, ctx.to_date)
    comparison = compare_event_series(series.get("purchases"), series.get("installs"))
    if comparison.empty or not any(comparison[m].sum() for m in series):
        ctx.empty()
        return None
    st.caption(t("q1.definition"))

    labels = {"purchases": t("q1.purchases"), "installs": t("q1.installs")}
    ranked: dict[str, pd.DataFrame] = {}
    columns = st.columns(2 * len(series))
    for index, measure in enumerate(series):
        frame = comparison[["period_start", measure]].rename(columns={measure: "value"})
        ranked[measure] = rank_peaks_and_lows(frame, n=top_n)
        if ranked[measure].empty:
            columns[2 * index].info(t("q1.flat_series", measure=labels[measure]))
            continue
        peak = ranked[measure].query("kind == 'peak'").iloc[0]
        low = ranked[measure].query("kind == 'low'").iloc[0]
        columns[2 * index].metric(
            t("q1.peak", measure=labels[measure]), format_int(peak["value"]),
            format_date(peak["period_start"]), delta_color="off",
        )
        columns[2 * index + 1].metric(
            t("q1.low", measure=labels[measure]), format_int(low["value"]),
            format_date(low["period_start"]), delta_color="off",
        )

    long = comparison.melt(
        id_vars="period_start", value_vars=list(series), var_name="series", value_name="devices"
    )
    long["series"] = long["series"].map(labels)
    charts.show(charts.line_chart(
        long, "period_start", "devices", t("common.period_start"), t("q1.axis_devices"),
        series="series", series_title=t("q1.series"),
    ))
    summary = [
        t(
            "q1.summary", measure=labels[m], peak=format_int(ranked[m].query("kind == 'peak'").iloc[0]["value"]),
            peak_date=format_date(ranked[m].query("kind == 'peak'").iloc[0]["period_start"]),
            low=format_int(ranked[m].query("kind == 'low'").iloc[0]["value"]),
            low_date=format_date(ranked[m].query("kind == 'low'").iloc[0]["period_start"]),
            mean=format_number(comparison[m].mean(), 1),
        )
        for m in series if not ranked[m].empty
    ]
    st.caption(" ".join(summary))
    data_table(comparison.pipe(with_dates))

    if len(series) == 2:
        st.subheader(t("q1.gap_title"))
        charts.show(charts.line_chart(
            comparison, "period_start", "cumulative_gap", t("common.period_start"), t("q1.axis_gap"),
        ))
        last = comparison.iloc[-1]["cumulative_gap"]
        st.caption(t("q1.gap_summary", gap=format_int(last)))

    with st.expander(t("q1.ranking_title")):
        for measure, frame in ranked.items():
            st.markdown(f"**{labels[measure]}**")
            st.dataframe(export_frame(readable_kinds(frame)), use_container_width=True, hide_index=True)

    sheets = {t("export.sheet.period_series"): export_frame(comparison)}
    sheets.update({
        f"{t('export.sheet.peaks_lows')} – {labels[m]}": export_frame(readable_kinds(f)) for m, f in ranked.items()
    })
    return sheets


def readable_kinds(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.assign(kind=frame["kind"].map({"peak": t("q1.kind_peak"), "low": t("q1.kind_low")}))


run_analysis_page(
    1, body, export_page="q1_peaks_lows", uses_purchase_timing=True,
    requires_any=("col_sales_date", "col_installed_at"),
)
