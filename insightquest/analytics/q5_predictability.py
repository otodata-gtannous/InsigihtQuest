"""Q5 - how predictable are purchases (overall; per-market lives in q5_markets).

The lagged correlation with an external market series (``compare_with_market``)
is an OPTIONAL extra, used only when the caller supplies such a series.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from ._common import date_basis_metadata, period_calendar, validate_period
from .legacy import _forecast_values, _season_length_for_period

BACKTEST_COLUMNS = ["period_start", "actual", "predicted", "method"]
MARKET_COLUMNS = ["lag_months", "n_obs", "correlation", "n_obs_diff", "correlation_diff"]
MARKET_CAVEAT = (
    "Descriptive only: a correlation between purchases and a market indicator does not "
    "show that the market causes purchases. 'lag_months' = the market value N months "
    "earlier is compared with this month's purchases."
)
SCORE_NOTE = (
    "predictability_score = 100 x (1 - WAPE), floored at 0, where WAPE = sum|error| / sum(actual) "
    "over the rolling one-step-ahead backtest. >=75 High, >=50 Medium, otherwise Low."
)
FORECAST_COLUMNS = ["period_start", "forecast", "method"]
PERIODS_PER_MONTH = {"Day": 365 / 12, "Week": 52 / 12, "Month": 1.0}


@dataclass(frozen=True)
class PredictabilityResult:
    """status is 'ok' or 'insufficient_data'; metrics values are None when unavailable.

    ``forecast`` is only filled when status is 'ok' and horizon > 0.
    """

    status: str
    metrics: dict[str, Any]
    backtest: pd.DataFrame
    message: str
    metadata: dict[str, Any] = field(default_factory=dict)
    forecast: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(columns=FORECAST_COLUMNS))


@dataclass(frozen=True)
class MarketComparison:
    """status is 'ok' or 'insufficient_data'; table = MARKET_COLUMNS, one row per lag."""

    status: str
    table: pd.DataFrame
    aligned_months: int
    message: str
    best_lag_months: int | None
    best_correlation: float | None
    metadata: dict[str, Any] = field(default_factory=dict)


def _label_for_score(score: float | None) -> str | None:
    if score is None:
        return None
    if score >= 75:
        return "High"
    if score >= 50:
        return "Medium"
    return "Low"


def trend_summary(
    values: np.ndarray | pd.Series, period: str = "Month", flat_threshold_pct: float = 1.0
) -> dict[str, Any]:
    """Linear trend of a gap-filled series.

    Returns slope_per_month (units per month), slope_pct_per_month (slope as a
    percentage of the series mean) and direction: 'up' / 'down' when the
    percentage exceeds +/- ``flat_threshold_pct``, otherwise 'flat'. All None
    with fewer than 3 points; the percentage and direction are None when the
    mean is 0.
    """
    validate_period(period)
    y = np.asarray(values, dtype=float)
    y = y[~np.isnan(y)]
    result: dict[str, Any] = {"slope_per_month": None, "slope_pct_per_month": None, "direction": None}
    if len(y) < 3:
        return result
    slope = float(np.polyfit(np.arange(len(y)), y, 1)[0]) * PERIODS_PER_MONTH[period]
    result["slope_per_month"] = slope
    mean = float(y.mean())
    if mean > 0:
        pct = slope / mean * 100
        result["slope_pct_per_month"] = pct
        result["direction"] = "up" if pct > flat_threshold_pct else "down" if pct < -flat_threshold_pct else "flat"
    return result


def forecast_series(series: pd.DataFrame | np.ndarray, horizon: int, period: str = "Month",
                    last_period_start: pd.Timestamp | None = None) -> pd.DataFrame:
    """Next ``horizon`` periods using Holt-Winters (>= 2 seasons) else seasonal naive.

    ``series`` is a gap-filled value array, or a DataFrame(period_start, value)
    (then sorted and gap-filled). Returns [period_start, forecast, method];
    forecasts are floored at 0 and not rounded. Empty for horizon <= 0 or fewer
    than 2 points.
    """
    validate_period(period)
    if isinstance(series, pd.DataFrame):
        history = _history_values(series, period, "value", "period_start")
        values = history["value"].to_numpy(dtype=float)
        last = history["period_start"].iloc[-1] if not history.empty else None
    else:
        values = np.asarray(series, dtype=float)
        last = last_period_start
    if horizon <= 0 or len(values) < 2 or last is None:
        return pd.DataFrame(columns=FORECAST_COLUMNS)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        predicted, method = _forecast_values(values, horizon, period)
    freq = {"Day": "D", "Week": "7D", "Month": "MS"}[period]
    future = pd.date_range(pd.Timestamp(last), periods=horizon + 1, freq=freq)[1:]
    return pd.DataFrame({"period_start": future, "forecast": predicted.astype(float), "method": method})


def _seasonality_strength(values: np.ndarray, season: int) -> float | None:
    """Share of non-trend variance that is seasonal (0..1); needs two full seasons."""
    if len(values) < 2 * season or season < 2:
        return None
    from statsmodels.tsa.seasonal import seasonal_decompose

    try:
        parts = seasonal_decompose(values, model="additive", period=season, two_sided=True)
    except ValueError:
        return None
    resid = parts.resid[~np.isnan(parts.resid)]
    seasonal = parts.seasonal[~np.isnan(parts.resid)]
    denom = np.var(resid + seasonal)
    if denom == 0:
        return None
    return float(max(0.0, 1 - np.var(resid) / denom))


def _history_values(series: pd.DataFrame, period: str, value_column: str, period_column: str) -> pd.DataFrame:
    frame = series[[period_column, value_column]].copy()
    frame.columns = ["period_start", "value"]
    frame["period_start"] = pd.to_datetime(frame["period_start"], errors="coerce")
    frame["value"] = pd.to_numeric(frame["value"], errors="coerce")
    frame = frame.dropna(subset=["period_start"])
    if frame.empty:
        return frame
    calendar = period_calendar(frame["period_start"].min(), frame["period_start"].max(), period)
    summed = frame.groupby("period_start")["value"].sum(min_count=1).reindex(calendar)
    return summed.fillna(0.0).rename_axis("period_start").reset_index()


def assess_purchase_predictability(
    series: pd.DataFrame,
    period: str = "Month",
    value_column: str = "value",
    period_column: str = "period_start",
    min_train_points: int | None = None,
    max_folds: int = 24,
    min_folds: int = 3,
    is_proxy: bool = False,
    horizon: int = 0,
    trend_flat_pct: float = 1.0,
) -> PredictabilityResult:
    """Rolling one-step-ahead backtest of the existing Holt-Winters/seasonal-naive forecaster.

    ``series`` is a period series such as ``build_activity_series(...).data``;
    missing periods are filled with 0. At most the last ``max_folds`` points are
    forecast; training needs ``min_train_points`` (default: one season) and the
    result is 'insufficient_data' with fewer than ``min_folds`` folds.
    metrics: mae, rmse, mape_pct (actual == 0 rows excluded, count in
    mape_zero_actuals_excluded; None if no non-zero actual), wape_pct, mase
    (MAE / in-sample seasonal-naive MAE; <1 beats seasonal naive), cv
    (std / mean of the series), seasonality_strength (0..1, None under two
    seasons), trend_slope_per_month, trend_pct_per_month, trend_direction
    ('up'/'flat'/'down', see trend_summary), predictability_score (0..100),
    predictability_label (High/Medium/Low), folds, n_points, model_name.
    Descriptive metrics (cv, seasonality, trend) are filled even when the
    backtest is insufficient. ``horizon`` > 0 adds ``forecast`` for that many
    future periods when the status is 'ok'.
    """
    validate_period(period)
    if max_folds < 1 or min_folds < 1:
        raise ValueError("max_folds and min_folds must be at least 1.")
    season = _season_length_for_period(period)
    min_train = int(min_train_points) if min_train_points is not None else season
    if min_train < 2:
        raise ValueError("min_train_points must be at least 2.")
    metadata = {
        **date_basis_metadata(is_proxy, None),
        "period": period,
        "season_length": season,
        "min_train_points": min_train,
        "score_definition": SCORE_NOTE,
    }
    metrics: dict[str, Any] = {k: None for k in (
        "mae", "rmse", "mape_pct", "wape_pct", "mase", "cv", "seasonality_strength",
        "predictability_score", "predictability_label", "model_name",
        "trend_slope_per_month", "trend_pct_per_month", "trend_direction",
    )}
    metrics.update(folds=0, n_points=0, mape_zero_actuals_excluded=0)
    empty = pd.DataFrame(columns=BACKTEST_COLUMNS)

    history = _history_values(series, period, value_column, period_column) if series is not None and not series.empty else pd.DataFrame()
    if history.empty:
        return PredictabilityResult("insufficient_data", metrics, empty, "No usable data points.", metadata)

    y = history["value"].to_numpy(dtype=float)
    n = len(y)
    metrics["n_points"] = n
    mean = float(y.mean())
    metrics["cv"] = float(y.std(ddof=1) / mean) if n > 1 and mean > 0 else None
    metrics["seasonality_strength"] = _seasonality_strength(y, season)
    trend = trend_summary(y, period, trend_flat_pct)
    metrics.update(
        trend_slope_per_month=trend["slope_per_month"],
        trend_pct_per_month=trend["slope_pct_per_month"],
        trend_direction=trend["direction"],
    )
    if n - min_train < min_folds:
        message = (
            f"Insufficient data: {n} periods available, need at least {min_train + min_folds} "
            f"({min_train} to train + {min_folds} to test)."
        )
        return PredictabilityResult("insufficient_data", metrics, empty, message, metadata)

    rows = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for index in range(max(min_train, n - max_folds), n):
            prediction, method = _forecast_values(y[:index], 1, period)
            rows.append(
                {
                    "period_start": history["period_start"].iloc[index],
                    "actual": float(y[index]),
                    "predicted": float(prediction[0]),
                    "method": method,
                }
            )
    backtest = pd.DataFrame(rows)[BACKTEST_COLUMNS]
    actual = backtest["actual"].to_numpy()
    errors = backtest["predicted"].to_numpy() - actual
    abs_errors = np.abs(errors)
    mae = float(abs_errors.mean())
    nonzero = actual != 0
    lag = season if n > season else 1
    scale = float(np.abs(y[lag:] - y[:-lag]).mean())
    wape = float(abs_errors.sum() / np.abs(actual).sum()) if np.abs(actual).sum() > 0 else None
    score = round(100 * max(0.0, 1 - wape), 1) if wape is not None else None
    metrics.update(
        mae=mae,
        rmse=float(np.sqrt(np.mean(errors**2))),
        mape_pct=float((abs_errors[nonzero] / np.abs(actual[nonzero])).mean() * 100) if nonzero.any() else None,
        mape_zero_actuals_excluded=int((~nonzero).sum()),
        wape_pct=wape * 100 if wape is not None else None,
        mase=mae / scale if scale > 0 else None,
        predictability_score=score,
        predictability_label=_label_for_score(score),
        folds=len(backtest),
        model_name=" + ".join(sorted(set(backtest["method"]))),
    )
    metadata["mase_scale_lag"] = lag
    forecast = forecast_series(y, horizon, period, history["period_start"].iloc[-1])
    return PredictabilityResult("ok", metrics, backtest, "Backtest completed.", metadata, forecast)


def _monthly_series(data: pd.DataFrame | pd.Series, value_column: str, month_column: str, summed: bool) -> pd.Series:
    if isinstance(data, pd.Series):
        series = pd.to_numeric(data, errors="coerce")
        index = pd.to_datetime(series.index, errors="coerce")
        series = pd.Series(series.to_numpy(), index=index)
    else:
        if month_column not in data.columns or value_column not in data.columns:
            raise KeyError(f"Expected columns '{month_column}' and '{value_column}'.")
        series = pd.Series(
            pd.to_numeric(data[value_column], errors="coerce").to_numpy(),
            index=pd.to_datetime(data[month_column], errors="coerce"),
        )
    series = series[series.index.notna()]
    series.index = series.index.to_period("M").to_timestamp()
    grouped = series.groupby(level=0)
    return (grouped.sum(min_count=1) if summed else grouped.mean()).sort_index()


def compare_with_market(
    purchases: pd.DataFrame | pd.Series,
    market: pd.DataFrame | pd.Series,
    max_lag: int = 6,
    min_months: int = 12,
    min_lag_obs: int = 6,
    purchase_value_column: str = "value",
    purchase_month_column: str = "period_start",
    market_value_column: str = "value",
    market_month_column: str = "month",
) -> MarketComparison:
    """Lagged correlation (lags 0..max_lag months) of monthly purchases with a market series.

    Purchases may be a Series indexed by date or a DataFrame(period_start, value);
    the market a Series indexed by month or a DataFrame(month, value). Both are
    reduced to month starts (purchases summed, market averaged within a month).
    Lag k pairs this month's purchases with the market value k months earlier.
    ``correlation`` is Pearson on levels; ``correlation_diff`` uses month-on-month
    changes (guards against two trending series looking related). A lag whose
    pair count is below ``min_lag_obs`` or where either side is constant gets
    NaN. If fewer than ``min_months`` months align at lag 0 the status is
    'insufficient_data' and every correlation is NaN. Descriptive, not causal.
    """
    if max_lag < 0:
        raise ValueError("max_lag must be zero or positive.")
    p = _monthly_series(purchases, purchase_value_column, purchase_month_column, summed=True)
    m = _monthly_series(market, market_value_column, market_month_column, summed=False)
    p, m = p.dropna(), m.dropna()
    lags = list(range(max_lag + 1))
    table = pd.DataFrame({"lag_months": lags})
    table["n_obs"] = 0
    table["correlation"] = np.nan
    table["n_obs_diff"] = 0
    table["correlation_diff"] = np.nan
    aligned = int(len(p.index.intersection(m.index)))
    metadata = {"caveat": MARKET_CAVEAT, "min_months": min_months, "min_lag_obs": min_lag_obs,
                "max_lag": max_lag}
    if p.empty or m.empty:
        return MarketComparison("insufficient_data", table[MARKET_COLUMNS], aligned,
                                "Insufficient data: purchases or market series is empty.",
                                None, None, metadata)

    calendar = pd.date_range(min(p.index.min(), m.index.min()), max(p.index.max(), m.index.max()), freq="MS")
    pf, mf = p.reindex(calendar), m.reindex(calendar)
    sufficient = aligned >= min_months

    def _corr(a: pd.Series, b: pd.Series) -> tuple[int, float]:
        pair = pd.concat([a, b], axis=1).dropna()
        if len(pair) < max(min_lag_obs, 3) or pair.iloc[:, 0].nunique() < 2 or pair.iloc[:, 1].nunique() < 2:
            return len(pair), np.nan
        return len(pair), float(pair.iloc[:, 0].corr(pair.iloc[:, 1]))

    for lag in lags:
        n_lvl, c_lvl = _corr(pf, mf.shift(lag))
        n_dif, c_dif = _corr(pf.diff(), mf.diff().shift(lag))
        table.loc[table["lag_months"] == lag, ["n_obs", "n_obs_diff"]] = [n_lvl, n_dif]
        if sufficient:
            table.loc[table["lag_months"] == lag, ["correlation", "correlation_diff"]] = [c_lvl, c_dif]
    table[["n_obs", "n_obs_diff"]] = table[["n_obs", "n_obs_diff"]].astype(int)
    table = table[MARKET_COLUMNS]

    if not sufficient:
        message = f"Insufficient data: {aligned} aligned months, need at least {min_months}."
        return MarketComparison("insufficient_data", table, aligned, message, None, None, metadata)
    valid = table.dropna(subset=["correlation"])
    if valid.empty:
        return MarketComparison("insufficient_data", table, aligned,
                                "Insufficient variation to compute a correlation.", None, None, metadata)
    best = valid.loc[valid["correlation"].abs().idxmax()]
    return MarketComparison("ok", table, aligned, "Correlations computed (descriptive, not causal).",
                            int(best["lag_months"]), float(best["correlation"]), metadata)
