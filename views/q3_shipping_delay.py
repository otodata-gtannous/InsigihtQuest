from __future__ import annotations

import pandas as pd
import streamlit as st

from insightquest import charts
from insightquest.aggregates import (
    cohort_trend_from_lag,
    lag_analysis_from_distribution,
    lag_distribution_from_cohorts,
)
from insightquest.cache import cached_lag_by_cohort
from insightquest.i18n import format_date, format_int, format_number, format_percent, t
from insightquest.page_helpers import PageContext, data_table, export_frame, run_analysis_page


def render_cohort_trend(lag: pd.DataFrame) -> pd.DataFrame:
    trend = cohort_trend_from_lag(lag)
    if trend.empty:
        return trend
    st.subheader(t("q3.cohort_title"))
    long = trend.melt(
        id_vars="cohort_month", value_vars=["median_days", "p90_days"], var_name="series", value_name="days"
    )
    long["series"] = long["series"].map({"median_days": t("q3.median"), "p90_days": t("q3.p90")})
    charts.show(charts.line_chart(
        long.dropna(subset=["days"]), "cohort_month", "days", t("q3.axis_cohort"), t("q3.axis_cohort_days"),
        series="series", series_title=t("q1.series"),
    ))
    first, last = trend.iloc[0], trend.iloc[-1]
    st.caption(t(
        "q3.cohort_summary", first=format_number(first["median_days"], 1), first_month=format_date(first["cohort_month"]),
        last=format_number(last["median_days"], 1), last_month=format_date(last["cohort_month"]),
    ))
    st.caption(t("q3.cohort_note"))
    data_table(trend)
    return trend


def body(ctx: PageContext) -> dict[str, pd.DataFrame] | None:
    # Shipments inside the range, installs counted up to the To date (later ones are not installed yet).
    lag = cached_lag_by_cohort(ctx.config, ctx.from_date, ctx.to_date, "shipped", ctx.to_date)
    analysis = lag_analysis_from_distribution(lag_distribution_from_cohorts(lag))
    if analysis["total"] == 0:
        ctx.empty()
        return None
    st.caption(t("q3.definition"))

    summary, valid, negative = analysis["summary"], analysis["valid"], analysis["negative_lag"]
    if valid == 0:
        st.warning(t("q3.no_valid_lags", negative=format_int(negative)))
        return None

    cols = st.columns(5)
    cols[0].metric(t("q3.matched"), format_int(valid), help=t("q3.matched_help"))
    cols[1].metric(t("q3.median"), f"{format_number(summary['median_days'], 1)} {t('common.days')}")
    cols[2].metric(t("q3.p90"), f"{format_number(summary['p90_days'], 1)} {t('common.days')}")
    cols[3].metric(t("q3.mean"), f"{format_number(summary['mean_days'], 1)} {t('common.days')}")
    cols[4].metric(t("q3.negative"), format_int(negative), help=t("q3.negative_help"))

    histogram = analysis["histogram"].copy()
    histogram["bin_label"] = histogram["bin_label"] + " " + t("common.days_short")
    charts.show(charts.bar_chart(
        histogram, "bin_label", "count", t("q3.axis_bins"), t("q3.axis_devices"),
    ))
    top = histogram.loc[histogram["count"].idxmax()]
    st.caption(t(
        "q3.summary", median=format_number(summary["median_days"], 1), p90=format_number(summary["p90_days"], 1),
        top_bin=top["bin_label"], top_share=format_percent(top["share_pct"] / 100, 1),
    ))
    st.caption(t("q3.censoring_note"))
    data_table(histogram)

    sheets = {t("export.sheet.lag_histogram"): export_frame(histogram)}
    trend = render_cohort_trend(lag)
    if not trend.empty:
        sheets[t("export.sheet.lag_cohorts")] = export_frame(trend)
    return sheets


run_analysis_page(
    3, body, export_page="q3_shipping_delay", uses_purchase_timing=True,
    requires=("col_shipped_at", "col_installed_at"),
)
