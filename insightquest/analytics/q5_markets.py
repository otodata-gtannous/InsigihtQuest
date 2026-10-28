"""Q5 - per-market purchase predictability (markets = territories and product types).

Purely from purchase history: no external market data is needed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

import numpy as np
import pandas as pd

from ._common import clean_labels, date_basis_metadata, parse_dates, require_columns
from .q5_predictability import (
    PredictabilityResult,
    assess_purchase_predictability,
)

INSUFFICIENT_LABEL = "Insufficient history"
MARKET_TABLE_COLUMNS = [
    "market",
    "status",
    "small_market",
    "n_months",
    "first_month",
    "purchase_rows",
    "total_value",
    "share_pct",
    "mean_monthly",
    "trend_slope_per_month",
    "trend_pct_per_month",
    "trend_direction",
    "cv",
    "seasonality_strength",
    "mae",
    "rmse",
    "mape_pct",
    "wape_pct",
    "mase",
    "predictability_score",
    "predictability_label",
    "volume_recent",
    "volume_prior",
    "share_recent_pct",
    "share_prior_pct",
    "share_change_pp",
    "growth_pct",
    "growth_label",
    "volatility_contribution_pct",
    "volatility_vs_share",
]
FORECAST_COLUMNS = ["market", "period_start", "forecast", "method"]


@dataclass(frozen=True)
class MarketPredictability:
    """table: one row per market (MARKET_TABLE_COLUMNS); forecasts: long frame
    (market, period_start, forecast, method), empty unless horizon > 0; overall:
    the same assessment on the summed series; metadata: windows, thresholds,
    definitions, date-basis label (is_proxy)."""

    table: pd.DataFrame
    forecasts: pd.DataFrame
    overall: PredictabilityResult | None
    metadata: dict[str, Any] = field(default_factory=dict)


DEFINITIONS = {
    "status": "'ok', or 'insufficient_history' when the market has fewer than min_months monthly points",
    "n_months": "months from the market's first purchase to the last month (months without purchases count as 0)",
    "predictability_score": "100 x (1 - WAPE) of a rolling one-step-ahead backtest, floored at 0; High >= 75, Medium >= 50, else Low",
    "share_recent_pct / share_prior_pct": "market's share of all purchases in the last compare_months months vs the compare_months before",
    "volatility_contribution_pct": "cov(market monthly series, total monthly series) / var(total); sums to 100% across markets and can be negative",
    "volatility_vs_share": "volatility_contribution_pct / share_pct (> 1 means the market adds more volatility than its size)",
    "small_market": "fewer purchase rows than small_market_min_rows or a share below small_market_share_pct",
}


def _empty_result(metadata: dict[str, Any]) -> MarketPredictability:
    return MarketPredictability(
        pd.DataFrame(columns=MARKET_TABLE_COLUMNS),
        pd.DataFrame(columns=FORECAST_COLUMNS),
        None,
        metadata,
    )


def assess_market_predictability(
    data: pd.DataFrame,
    date_column: str,
    market_column: str,
    amount_column: str | None = None,
    measure: str = "auto",
    as_of: pd.Timestamp | str | None = None,
    include_last_month: bool = True,
    min_months: int = 12,
    min_folds: int = 3,
    max_folds: int = 12,
    horizon: int = 0,
    flat_threshold_pct: float = 1.0,
    compare_months: int = 12,
    growth_stable_pct: float = 5.0,
    small_market_min_rows: int = 20,
    small_market_share_pct: float = 2.0,
    missing_label: str = "Unknown",
    is_proxy: bool = False,
) -> MarketPredictability:
    """Predictability, trend, share change and volatility contribution per market.

    ``market_column`` is the dimension (territory, product type, ...); call it
    once per dimension or use :func:`assess_all_markets`. The monthly value is a
    row count (one row per device) unless ``amount_column`` is given
    (``measure`` 'auto'/'count'/'amount', same meaning as Q1). Months run from
    the first purchase to ``as_of``'s month (default the latest purchase month;
    ``include_last_month=False`` drops it when partial). A market's own series
    starts at its first purchase month.

    A market with fewer than ``min_months`` months (default 12) is marked
    status 'insufficient_history', label 'Insufficient history', has no
    backtest metrics or forecast, but still gets share, trend, cv and volatility
    columns. Small markets are flagged, never dropped. Share/growth compare the
    last ``compare_months`` months with the previous ``compare_months``; these
    are NaN/None (metadata['comparison_status'] = 'insufficient_history') when
    the history is shorter than twice that. growth_label: 'growing'/'declining'
    beyond +/-``growth_stable_pct`` % volume growth, else 'stable'; 'new' (none
    before) and 'inactive' (none recently) otherwise. ``horizon`` > 0 forecasts
    that many months per market (Holt-Winters, seasonal-naive fallback).
    """
    if measure not in {"auto", "count", "amount"}:
        raise ValueError("measure must be 'auto', 'count' or 'amount'.")
    if measure == "amount" and amount_column is None:
        raise ValueError("measure='amount' requires amount_column.")
    if min_folds < 1 or min_months < min_folds + 2:
        raise ValueError("min_months must be at least min_folds + 2 and min_folds at least 1.")
    if compare_months < 1 or horizon < 0:
        raise ValueError("compare_months must be >= 1 and horizon >= 0.")
    resolved = "amount" if measure == "amount" or (measure == "auto" and amount_column) else "count"

    metadata: dict[str, Any] = {
        **date_basis_metadata(is_proxy, date_column),
        "market_column": market_column,
        "measure": resolved,
        "amount_column": amount_column if resolved == "amount" else None,
        "min_months": min_months,
        "compare_months": compare_months,
        "comparison_status": "insufficient_history",
        "recent_window": None,
        "prior_window": None,
        "rows_missing_date": 0,
        "rows_used": 0,
        "definitions": dict(DEFINITIONS),
    }
    if data is None or data.empty:
        return _empty_result(metadata)
    require_columns(data, [date_column, market_column, amount_column if resolved == "amount" else None])

    dates, _ = parse_dates(data[date_column])
    value = (
        pd.to_numeric(data[amount_column], errors="coerce").fillna(0.0)
        if resolved == "amount"
        else pd.Series(1.0, index=data.index)
    )
    frame = pd.DataFrame(
        {
            "month": dates.dt.to_period("M").dt.to_timestamp(),
            "market": clean_labels(data[market_column], missing_label),
            "value": value,
            "rows": 1,
        }
    )
    metadata["rows_missing_date"] = int(dates.isna().sum())
    frame = frame.dropna(subset=["month"])
    if frame.empty:
        return _empty_result(metadata)

    end_month = (
        pd.Timestamp(as_of).to_period("M").to_timestamp() if as_of is not None else frame["month"].max()
    )
    if not include_last_month:
        end_month = end_month - pd.offsets.MonthBegin(1)
    frame = frame[frame["month"] <= end_month]
    if frame.empty:
        return _empty_result(metadata)
    calendar = pd.date_range(frame["month"].min(), end_month, freq="MS")
    metadata.update(rows_used=int(len(frame)), first_month=calendar[0], last_month=end_month)

    pivot = (
        frame.pivot_table(index="month", columns="market", values="value", aggfunc="sum", fill_value=0.0)
        .reindex(calendar, fill_value=0.0)
    )
    rows_by_market = frame.groupby("market")["rows"].sum()
    first_month = frame.groupby("market")["month"].min()
    total = pivot.sum(axis=1)
    grand_total = float(total.sum())

    n_cal = len(calendar)
    can_compare = n_cal >= 2 * compare_months
    if can_compare:
        recent, prior = pivot.iloc[-compare_months:], pivot.iloc[-2 * compare_months:-compare_months]
        metadata.update(
            comparison_status="ok",
            recent_window=(recent.index[0], recent.index[-1]),
            prior_window=(prior.index[0], prior.index[-1]),
        )
    var_total = float(total.var(ddof=1)) if n_cal >= 3 else 0.0
    min_train = min_months - min_folds
    options = dict(
        period="Month",
        min_train_points=min_train,
        max_folds=max_folds,
        min_folds=min_folds,
        horizon=horizon,
        trend_flat_pct=flat_threshold_pct,
        is_proxy=is_proxy,
    )

    rows: list[dict[str, Any]] = []
    forecasts: list[pd.DataFrame] = []
    for market in pivot.columns:
        series = pivot.loc[first_month[market]:, market]
        series_df = series.rename("value").rename_axis("period_start").reset_index()
        assessed = assess_purchase_predictability(series_df, **options)
        m = assessed.metrics
        ok = assessed.status == "ok"
        market_total = float(pivot[market].sum())
        share = market_total / grand_total * 100 if grand_total > 0 else np.nan
        row: dict[str, Any] = {
            "market": market,
            "status": "ok" if ok else "insufficient_history",
            "small_market": bool(
                rows_by_market[market] < small_market_min_rows
                or (not np.isnan(share) and share < small_market_share_pct)
            ),
            "n_months": int(len(series)),
            "first_month": first_month[market],
            "purchase_rows": int(rows_by_market[market]),
            "total_value": market_total,
            "share_pct": share,
            "mean_monthly": float(series.mean()),
            "trend_slope_per_month": m["trend_slope_per_month"],
            "trend_pct_per_month": m["trend_pct_per_month"],
            "trend_direction": m["trend_direction"],
            "cv": m["cv"],
            "seasonality_strength": m["seasonality_strength"],
        }
        for key in ("mae", "rmse", "mape_pct", "wape_pct", "mase", "predictability_score"):
            row[key] = m[key] if ok else None
        row["predictability_label"] = m["predictability_label"] if ok else INSUFFICIENT_LABEL
        if ok and horizon > 0 and not assessed.forecast.empty:
            forecasts.append(assessed.forecast.assign(market=market))

        if can_compare:
            vol_recent, vol_prior = float(recent[market].sum()), float(prior[market].sum())
            tot_recent, tot_prior = float(recent.sum().sum()), float(prior.sum().sum())
            share_recent = vol_recent / tot_recent * 100 if tot_recent > 0 else np.nan
            share_prior = vol_prior / tot_prior * 100 if tot_prior > 0 else np.nan
            growth = (vol_recent - vol_prior) / vol_prior * 100 if vol_prior > 0 else np.nan
            if vol_prior == 0 and vol_recent > 0:
                label = "new"
            elif vol_recent == 0 and vol_prior > 0:
                label = "inactive"
            elif np.isnan(growth):
                label = None
            else:
                label = "growing" if growth > growth_stable_pct else "declining" if growth < -growth_stable_pct else "stable"
            row.update(
                volume_recent=vol_recent,
                volume_prior=vol_prior,
                share_recent_pct=share_recent,
                share_prior_pct=share_prior,
                share_change_pp=share_recent - share_prior,
                growth_pct=growth,
                growth_label=label,
            )
        if var_total > 0:
            contribution = float(np.cov(pivot[market], total, ddof=1)[0, 1] / var_total * 100)
            row["volatility_contribution_pct"] = contribution
            row["volatility_vs_share"] = contribution / share if share and not np.isnan(share) and share > 0 else np.nan
        rows.append(row)

    table = pd.DataFrame(rows).reindex(columns=MARKET_TABLE_COLUMNS)
    table = table.sort_values(["total_value", "market"], ascending=[False, True]).reset_index(drop=True)
    total_df = total.rename("value").rename_axis("period_start").reset_index()
    overall = assess_purchase_predictability(total_df, **options)
    forecast_table = (
        pd.concat(forecasts, ignore_index=True)[FORECAST_COLUMNS]
        if forecasts
        else pd.DataFrame(columns=FORECAST_COLUMNS)
    )
    return MarketPredictability(table, forecast_table, overall, metadata)


def assess_all_markets(
    data: pd.DataFrame,
    date_column: str,
    market_columns: Mapping[str, str],
    **options: Any,
) -> dict[str, MarketPredictability]:
    """Run :func:`assess_market_predictability` per dimension.

    ``market_columns`` maps a label to a column, e.g.
    ``{'territory': 'Territory', 'product_type': 'DeviceType'}``; the result is
    keyed by the same labels. Options are forwarded unchanged.
    """
    return {
        label: assess_market_predictability(data, date_column, column, **options)
        for label, column in market_columns.items()
    }
