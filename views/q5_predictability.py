from __future__ import annotations

import numpy as np
import pandas as pd
import streamlit as st

from insightquest import charts
from insightquest.aggregates import total_by_month
from insightquest.analytics import assess_purchase_predictability
from insightquest.analytics.q5_markets import INSUFFICIENT_LABEL
from insightquest.cache import cached_monthly_purchase_counts
from insightquest.i18n import format_int, format_number, t
from insightquest.page_helpers import (
    PageContext,
    cached_market_assessment,
    data_table,
    export_frame,
    localize_columns,
    run_analysis_page,
)

DIMENSIONS = (("territory", "col_territory", "q5.territories"), ("product_type", "col_product_type", "q5.product_types"))
LABEL_KEYS = {"High": "label.high", "Medium": "label.medium", "Low": "label.low", INSUFFICIENT_LABEL: "label.insufficient"}
GROWTH_KEYS = {k: f"growth.{k}" for k in ("growing", "declining", "stable", "new", "inactive")}
TABLE_COLUMNS = [
    "market", "predictability_label", "predictability_score", "growth_pct", "growth_label",
    "share_pct", "share_change_pp", "volatility_contribution_pct", "n_months", "purchase_rows", "small_market",
]


def render_overall(ctx: PageContext, counts: pd.DataFrame, end_month: pd.Timestamp, horizon: int) -> pd.DataFrame:
    history = total_by_month(counts, end_month)
    assessed = assess_purchase_predictability(history, "Month", horizon=horizon, is_proxy=ctx.proxy)
    metrics = assessed.metrics
    cols = st.columns(4)
    label = LABEL_KEYS.get(metrics["predictability_label"] or "")
    cols[0].metric(
        t("q5.score"), f"{format_number(metrics['predictability_score'], 0)} / 100" if metrics["predictability_score"] is not None else "–",
        help=t("q5.score_help"),
    )
    cols[1].metric(t("q5.label"), t(label) if label else t("label.insufficient"))
    direction = metrics["trend_direction"]
    cols[2].metric(
        t("q5.trend"), t(f"trend.{direction}") if direction else "–",
        f"{format_number(metrics['trend_pct_per_month'], 1)}% {t('q5.per_month')}" if metrics["trend_pct_per_month"] is not None else None,
        delta_color="off",
    )
    cols[3].metric(
        t("q5.wape"), f"{format_number(metrics['wape_pct'], 1)}%" if metrics["wape_pct"] is not None else "–",
        help=t("q5.wape_help"),
    )
    forecast = assessed.forecast.copy()
    if assessed.status != "ok":
        st.info(t("q5.insufficient_overall", message=assessed.message))
    if not forecast.empty:
        mae = metrics["mae"] or 0.0
        forecast["low"] = (forecast["forecast"] - mae).clip(lower=0)
        forecast["high"] = forecast["forecast"] + mae
        charts.show(charts.forecast_chart(
            history, forecast, "period_start", "value", "forecast", t("common.month"), t("q5.axis_purchases"),
            t("common.actual"), t("common.forecast"), "low", "high",
        ))
        st.caption(t(
            "q5.overall_summary", score=format_number(metrics["predictability_score"], 0),
            label=t(label) if label else "", folds=metrics["folds"], wape=format_number(metrics["wape_pct"], 1),
            mae=format_number(mae, 1), model=metrics["model_name"], horizon=horizon,
        ))
    else:
        charts.show(charts.line_chart(history, "period_start", "value", t("common.month"), t("q5.axis_purchases")))
        st.caption(t("q5.history_summary", months=format_int(len(history))))
    data_table(pd.concat([
        history.assign(type=t("common.actual")),
        forecast.rename(columns={"forecast": "value"})[["period_start", "value"]].assign(type=t("common.forecast"))
        if not forecast.empty else None,
    ], ignore_index=True))
    if not assessed.backtest.empty:
        with st.expander(t("q5.backtest_title")):
            st.dataframe(export_frame(assessed.backtest), use_container_width=True, hide_index=True)
    return export_frame(forecast) if not forecast.empty else export_frame(history)


def render_markets(ctx: PageContext, dimension: str, end_month: pd.Timestamp) -> pd.DataFrame | None:
    result = cached_market_assessment(ctx.config, dimension, ctx.from_date, ctx.to_date, end_month)
    table = result.table
    if table.empty:
        ctx.empty()
        return None
    shown = table[TABLE_COLUMNS].copy()
    shown["predictability_label"] = shown["predictability_label"].map(lambda v: t(LABEL_KEYS.get(v, "label.insufficient")))
    shown["growth_label"] = shown["growth_label"].map(lambda v: t(GROWTH_KEYS[v]) if v in GROWTH_KEYS else "–")
    shown["small_market"] = np.where(shown["small_market"], "⚠ " + t("q5.small_market"), "")

    scored = table.dropna(subset=["predictability_score"]).assign(
        label=lambda f: f["predictability_label"].map(lambda v: t(LABEL_KEYS[v]))
    )
    if not scored.empty:
        charts.show(charts.bar_chart(
            scored, "market", "predictability_score", t("q5.market"), t("q5.axis_score"),
            horizontal=True, color="label",
        ))
        best, worst = scored.iloc[scored["predictability_score"].argmax()], scored.iloc[scored["predictability_score"].argmin()]
        st.caption(t(
            "q5.market_summary", best=best["market"], best_score=format_number(best["predictability_score"], 0),
            worst=worst["market"], worst_score=format_number(worst["predictability_score"], 0),
        ))
    short = table[table["status"] != "ok"]
    if not short.empty:
        st.warning(t("q5.insufficient_markets", markets=", ".join(short["market"]), months=result.metadata["min_months"]))
    if result.metadata["comparison_status"] != "ok":
        st.caption(t("q5.comparison_unavailable", months=result.metadata["compare_months"] * 2))
    st.dataframe(localize_columns(shown), use_container_width=True, hide_index=True)
    with st.expander(t("q5.definitions_title")):
        for key, text in result.metadata["definitions"].items():
            st.markdown(f"**{key}** — {text}")
    return export_frame(shown)


def body(ctx: PageContext) -> dict[str, pd.DataFrame] | None:
    horizon = st.sidebar.slider(t("q5.horizon"), 1, 12, 6, help=t("q4.horizon_help"))
    include_last = st.sidebar.checkbox(t("q5.include_last"), value=False, help=t("q5.include_last_help"))
    ctx.filters[t("q5.horizon")] = horizon
    ctx.filters[t("q5.include_last")] = include_last
    available = [(d, a, k) for d, a, k in DIMENSIONS if ctx.has(a)]
    st.caption(t("q5.definition"))

    counts = cached_monthly_purchase_counts(ctx.config, available[0][0], ctx.from_date, ctx.to_date)
    if counts.empty:
        ctx.empty()
        return None
    end_month = pd.to_datetime(counts["period"]).max()
    if not include_last:
        end_month = end_month - pd.offsets.MonthBegin(1)

    sheets: dict[str, pd.DataFrame] = {}
    st.subheader(t("q5.overall_title"))
    sheets[t("export.sheet.overall")] = render_overall(ctx, counts, end_month, horizon)
    for dimension, _, label in available:
        st.subheader(t("q5.markets_title", dimension=t(label)))
        frame = render_markets(ctx, dimension, end_month)
        if frame is not None:
            sheets[t(label)] = frame
    return sheets


run_analysis_page(
    5, body, export_page="q5_predictability", uses_purchase_timing=True,
    requires=("col_sales_date",), requires_any=("col_territory", "col_product_type"),
)
