"""One-sentence live answers for the Home question cards, built from the cached aggregate fetchers."""

from __future__ import annotations

import logging
from datetime import date

import pandas as pd
import streamlit as st

from insightquest.aggregates import (
    breakdown_from_segment_counts,
    counts_to_series,
    lag_analysis_from_distribution,
    lag_distribution_from_cohorts,
    inventory_from_summary,
    rate_from_cohorts,
)
from insightquest.analytics import DEFAULT_INVENTORY_PARAMS, rank_peaks_and_lows
from insightquest.cache import (
    cached_event_counts,
    cached_inventory_summary,
    cached_lag_by_cohort,
    cached_monthly_purchase_counts,
    cached_purchase_install_cohorts,
    cached_segment_counts,
)
from insightquest.config import AppConfig, config_fingerprint
from insightquest.i18n import format_int, format_number, t

logger = logging.getLogger(__name__)


class NoAnswer(Exception):
    """The data cannot answer right now; the message is a translated, user-facing reason."""


def _month(value: object) -> str:
    return pd.Timestamp(value).strftime("%Y-%m")


def _has(config: AppConfig, columns: tuple[str, ...], attribute: str) -> bool:
    return bool(getattr(config, attribute, "")) and getattr(config, attribute).casefold() in {c.casefold() for c in columns}


def answer_q1(config: AppConfig, columns: tuple[str, ...], start: date, end: date) -> str:
    measure, attribute = ("installs", "col_installed_at") if _has(config, columns, "col_installed_at") else ("purchases", "col_sales_date")
    counts = cached_event_counts(config, measure, "month", start, end)
    series = counts_to_series(counts, "Month", start, end)
    if series.empty or not series["value"].sum():
        raise NoAnswer(t("home.reason.no_rows"))
    ranked = rank_peaks_and_lows(series[["period_start", "value"]], n=1)
    if ranked.empty:
        raise NoAnswer(t("home.reason.flat"))
    peak, low = ranked.query("kind == 'peak'").iloc[0], ranked.query("kind == 'low'").iloc[0]
    return t(
        f"home.answer.q1_{measure}", peak_month=_month(peak["period_start"]), peak=format_int(peak["value"]),
        low_month=_month(low["period_start"]), low=format_int(low["value"]),
    )


def answer_q2(config: AppConfig, columns: tuple[str, ...], start: date, end: date) -> str:
    dimension, label = next(
        ((d, k) for d, a, k in (("territory", "col_territory", "q2.territory"), ("product_type", "col_product_type", "q2.product_type"))
         if _has(config, columns, a)),
        (None, None),
    )
    if dimension is None:
        raise NoAnswer(t("home.reason.no_segment"))
    result = breakdown_from_segment_counts(cached_segment_counts(config, dimension, start, end), None, 5, None)
    table = result.data
    gaps = table[~table["small_group"]].sort_values("gap_vs_overall_pp") if not table.empty else table
    if gaps.empty:
        raise NoAnswer(t("home.reason.no_rows"))
    worst = gaps.iloc[0]
    return t(
        "home.answer.q2", dimension=t(label).lower(), group=worst["group"],
        rate=format_number(worst["installed_rate_pct"], 0), overall=format_number(result.metadata["overall_installed_rate_pct"], 0),
    )


def answer_q3(config: AppConfig, columns: tuple[str, ...], start: date, end: date) -> str:
    analysis = lag_analysis_from_distribution(
        lag_distribution_from_cohorts(cached_lag_by_cohort(config, start, end, "shipped", end))
    )
    if not analysis["valid"]:
        raise NoAnswer(t("home.reason.no_rows"))
    summary = analysis["summary"]
    return t("home.answer.q3", median=format_number(summary["median_days"], 0), p90=format_number(summary["p90_days"], 0))


def answer_q4(config: AppConfig, columns: tuple[str, ...], start: date, end: date) -> str:
    cohorts = cached_purchase_install_cohorts(config, start, end, None, end)
    if cohorts.empty:
        raise NoAnswer(t("home.reason.no_rows"))
    rates = rate_from_cohorts(cohorts, end).data
    complete = rates[~rates["is_partial_month"]]
    if complete.empty:
        raise NoAnswer(t("home.reason.no_rows"))
    latest = complete.iloc[-1]
    return t(
        "home.answer.q4", rate=format_number(latest["cohort_rate_pct"], 0), month=_month(latest["month"]),
        mature=format_number(latest["cohort_rate_to_date_pct"], 0),
    )


def answer_q5(config: AppConfig, columns: tuple[str, ...], start: date, end: date) -> str:
    from insightquest.page_helpers import cached_market_assessment  # page_helpers imports this module's siblings

    dimension = next((d for d, a in (("territory", "col_territory"), ("product_type", "col_product_type")) if _has(config, columns, a)), None)
    if dimension is None:
        raise NoAnswer(t("home.reason.no_segment"))
    counts = cached_monthly_purchase_counts(config, dimension, start, end)
    if counts.empty:
        raise NoAnswer(t("home.reason.no_rows"))
    end_month = pd.to_datetime(counts["period"]).max() - pd.offsets.MonthBegin(1)
    table = cached_market_assessment(config, dimension, start, end, end_month).table
    scored = table.dropna(subset=["predictability_score"]) if not table.empty else table
    if scored.empty:
        raise NoAnswer(t("home.reason.not_enough_history"))
    best = scored.sort_values("predictability_score", ascending=False).iloc[0]
    label = t({"High": "label.high", "Medium": "label.medium", "Low": "label.low"}.get(best["predictability_label"], "label.insufficient"))
    growth = best["growth_pct"]
    trend = t("home.answer.q5_trend", pct=("+" if growth > 0 else "") + format_number(growth, 1)) if pd.notna(growth) else ""
    return t("home.answer.q5", market=best["market"], label=label.lower(), trend=trend)


def answer_q6(config: AppConfig, columns: tuple[str, ...], start: date, end: date) -> str:
    from insightquest.page_helpers import latest_data_date

    as_of = latest_data_date(config)
    summary = cached_inventory_summary(config, as_of, (DEFAULT_INVENTORY_PARAMS["demand_window_days"],))
    table = inventory_from_summary(summary, as_of, **DEFAULT_INVENTORY_PARAMS).data
    if table.empty:
        raise NoAnswer(t("home.reason.no_rows"))
    risk = int(table["status"].isin(["stockout_risk", "reorder"]).sum())
    return t("home.answer.q6", risk=risk, total=len(table), excess=int((table["status"] == "overstock").sum()))


ANSWERS = {1: answer_q1, 2: answer_q2, 3: answer_q3, 4: answer_q4, 5: answer_q5, 6: answer_q6}


@st.cache_data(ttl=300, show_spinner=False)
def _answer(fingerprint: str, _config: AppConfig, columns: tuple[str, ...], number: int, start: date, end: date, language: str) -> str:
    return ANSWERS[number](_config, columns, start, end)


def question_answer(config: AppConfig, columns: tuple[str, ...], number: int, start: date, end: date) -> tuple[bool, str]:
    """(ok, text). Never raises: failures become 'Not available: <reason>'."""
    from insightquest.i18n import get_language

    try:
        return True, _answer(config_fingerprint(config), config, columns, number, start, end, get_language())
    except NoAnswer as reason:
        return False, t("home.answer.unavailable", reason=str(reason))
    except Exception:
        logger.exception("Home answer for question %s failed.", number)
        return False, t("home.answer.unavailable", reason=t("home.reason.error"))
