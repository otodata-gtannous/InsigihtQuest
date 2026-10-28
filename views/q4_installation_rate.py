from __future__ import annotations

import pandas as pd
import streamlit as st

from insightquest import charts
from insightquest.aggregates import rate_from_cohorts
from insightquest.analytics import assess_purchase_predictability
from insightquest.cache import cached_purchase_install_cohorts
from insightquest.i18n import format_date, format_int, format_number, t
from insightquest.page_helpers import PageContext, data_table, export_frame, run_analysis_page

RATE_SERIES = (
    ("cohort_rate_pct", "q4.rate_cohort"),
    ("cohort_rate_to_date_pct", "q4.rate_cohort_to_date"),
    ("rate_vs_population_pct", "q4.rate_population"),
)


def body(ctx: PageContext) -> dict[str, pd.DataFrame] | None:
    horizon = st.sidebar.slider(t("q4.horizon"), 1, 12, 3, help=t("q4.horizon_help"))
    ctx.filters[t("q4.horizon")] = horizon

    # as_of is passed to both the fetcher and the adapter: installs after it count as not installed yet.
    cohorts = cached_purchase_install_cohorts(ctx.config, ctx.from_date, ctx.to_date, None, ctx.to_date)
    if cohorts.empty:
        ctx.empty()
        return None
    result = rate_from_cohorts(cohorts, ctx.to_date, is_proxy=ctx.proxy)
    rates = result.data
    complete = rates[~rates["is_partial_month"]]
    if complete.empty:
        ctx.empty()
        return None
    st.caption(t("q4.definition", rows=format_int(result.metadata["rows_total"])))

    latest = complete.iloc[-1]
    cols = st.columns(3)
    cols[0].metric(
        t("q4.rate_cohort"), f"{format_number(latest['cohort_rate_pct'], 1)}%",
        help=t("q4.metric_help", numerator=format_int(latest["installed_by_month_end"]),
               denominator=format_int(latest["purchased"]), month=format_date(latest["month"])),
    )
    cols[1].metric(
        t("q4.rate_cohort_to_date"), f"{format_number(latest['cohort_rate_to_date_pct'], 1)}%",
        help=t("q4.metric_help", numerator=format_int(latest["installed_cohort_to_date"]),
               denominator=format_int(latest["purchased"]), month=format_date(latest["month"])),
    )
    cols[2].metric(
        t("q4.rate_population"), f"{format_number(latest['rate_vs_population_pct'], 2)}%",
        help=t("q4.metric_help", numerator=format_int(latest["installs_in_month"]),
               denominator=format_int(latest["purchased_to_date"]), month=format_date(latest["month"])),
    )

    long = complete.melt(
        id_vars="month", value_vars=[c for c, _ in RATE_SERIES], var_name="series", value_name="rate"
    )
    long["series"] = long["series"].map({c: t(k) for c, k in RATE_SERIES})
    charts.show(charts.line_chart(
        long.dropna(subset=["rate"]), "month", "rate", t("common.month"), t("q4.axis_rate"),
        series="series", series_title=t("q1.series"),
    ))
    st.caption(t(
        "q4.summary", month=format_date(latest["month"]), cohort=format_number(latest["cohort_rate_pct"], 1),
        mature=format_number(latest["cohort_rate_to_date_pct"], 1),
    ))
    st.caption(t("q4.maturity_note"))
    if result.metadata["negative_lag_devices"]:
        st.caption(t("q4.negative_note", count=format_int(result.metadata["negative_lag_devices"])))
    if len(complete) < len(rates):
        st.caption(t("q4.partial_note", month=format_date(rates.iloc[-1]["month"])))

    with st.expander(t("q4.definitions_title")):
        for metric, text in result.metadata["denominators"].items():
            st.markdown(f"**{metric}** — {text}")
    data_table(rates)

    st.subheader(t("q4.forecast_title"))
    series = complete[["month", "installs_in_month"]].rename(
        columns={"month": "period_start", "installs_in_month": "value"}
    )
    assessed = assess_purchase_predictability(series, "Month", horizon=horizon, is_proxy=ctx.proxy)
    forecast = assessed.forecast.copy()
    if forecast.empty:
        st.info(t("q4.forecast_unavailable", message=assessed.message))
        charts.show(charts.line_chart(series, "period_start", "value", t("common.month"), t("q4.axis_installs")))
    else:
        mae = assessed.metrics["mae"] or 0.0
        forecast["low"] = (forecast["forecast"] - mae).clip(lower=0)
        forecast["high"] = forecast["forecast"] + mae
        charts.show(charts.forecast_chart(
            series, forecast, "period_start", "value", "forecast", t("common.month"), t("q4.axis_installs"),
            t("common.actual"), t("common.forecast"), "low", "high",
        ))
        st.caption(t(
            "q4.forecast_summary", horizon=horizon, mae=format_number(mae, 1),
            model=assessed.metrics["model_name"], folds=assessed.metrics["folds"],
        ))
    data_table(pd.concat(
        [series.assign(type=t("common.actual")),
         forecast.rename(columns={"forecast": "value"})[["period_start", "value"]].assign(type=t("common.forecast"))
         if not forecast.empty else None],
        ignore_index=True,
    ))
    return {
        t("export.sheet.rate"): export_frame(rates),
        t("export.sheet.forecast"): export_frame(forecast) if not forecast.empty else pd.DataFrame(),
    }


run_analysis_page(
    4, body, export_page="q4_installation_rate", uses_purchase_timing=True,
    requires=("col_sales_date", "col_installed_at"),
)
