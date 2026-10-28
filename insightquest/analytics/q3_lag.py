"""Q3 - delay between purchase/ship date and installation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

import numpy as np
import pandas as pd

from ._common import date_basis_metadata, parse_dates, require_columns

DEFAULT_BIN_EDGES = (0, 7, 14, 30, 60, 90, 180, 365)
LAG_COLUMNS = ["purchase_date", "installed_at", "lag_days", "cohort_month"]
HISTOGRAM_COLUMNS = ["bin_label", "lower_days", "upper_days", "count", "share_pct"]
COHORT_COLUMNS = [
    "cohort_month",
    "purchased_devices",
    "installed_devices",
    "mean_days",
    "median_days",
    "p90_days",
]
CENSORING_NOTE = (
    "Only devices that already have an install date are measured, so recent cohorts "
    "look faster than they will end up (slow installs are not observed yet)."
)


@dataclass(frozen=True)
class LagAnalysis:
    """Per-device lags, summary stats, histogram, cohort trend and exclusion counts."""

    lags: pd.DataFrame
    summary: dict[str, Any]
    histogram: pd.DataFrame
    cohort_trend: pd.DataFrame
    excluded: dict[str, int]
    metadata: dict[str, Any] = field(default_factory=dict)


def summarize_lags(lag_days: pd.Series) -> dict[str, Any]:
    """count, mean, median, p50 (same as median), p90, min, max; None when empty."""
    values = pd.to_numeric(lag_days, errors="coerce").dropna()
    if values.empty:
        return {k: None for k in ("mean_days", "median_days", "p50_days", "p90_days", "min_days", "max_days")} | {"count": 0}
    median = float(values.median())
    return {
        "count": int(len(values)),
        "mean_days": float(values.mean()),
        "median_days": median,
        "p50_days": median,
        "p90_days": float(values.quantile(0.9)),
        "min_days": float(values.min()),
        "max_days": float(values.max()),
    }


def lag_histogram(
    lag_days: pd.Series, bin_edges: Sequence[float] = DEFAULT_BIN_EDGES
) -> pd.DataFrame:
    """Histogram with left-closed bins [lower, upper) and a final open-ended bin.

    With the default edges a lag of exactly 7 days falls in '7-13', and
    365+ is open-ended. Negative lags are not expected (they are excluded
    upstream) and are ignored. Returns [bin_label, lower_days, upper_days,
    count, share_pct]; upper_days is None for the last bin.
    """
    edges = list(bin_edges)
    if len(edges) < 1 or any(b <= a for a, b in zip(edges, edges[1:])):
        raise ValueError("bin_edges must be non-empty and strictly ascending.")
    values = pd.to_numeric(lag_days, errors="coerce").dropna()
    values = values[values >= edges[0]]
    labels, lowers, uppers = [], [], []
    for low, high in zip(edges, edges[1:]):
        shown = high - 1 if float(high).is_integer() and float(low).is_integer() else high
        labels.append(f"{low:g}-{shown:g}")
        lowers.append(low)
        uppers.append(high)
    labels.append(f"{edges[-1]:g}+")
    lowers.append(edges[-1])
    uppers.append(None)
    index = np.searchsorted(np.asarray(edges, dtype=float), values.to_numpy(), side="right") - 1
    counts = np.bincount(index, minlength=len(labels)) if len(values) else np.zeros(len(labels), int)
    total = int(counts.sum())
    result = pd.DataFrame(
        {
            "bin_label": labels,
            "lower_days": lowers,
            "upper_days": uppers,
            "count": counts.astype(int),
            "share_pct": np.round(counts / total * 100, 2) if total else np.zeros(len(labels)),
        }
    )
    return result[HISTOGRAM_COLUMNS]


def analyze_install_lag(
    data: pd.DataFrame,
    purchase_column: str,
    install_column: str,
    is_proxy: bool = False,
    bin_edges: Sequence[float] = DEFAULT_BIN_EDGES,
) -> LagAnalysis:
    """Lag in days from purchase (or ship/warranty proxy) date to install date.

    Rows are excluded from the lag statistics when either date is missing
    (``missing_purchase_date``, ``missing_install_date``; a row missing both
    counts in both, ``missing_any_date`` counts it once), or when the install
    precedes the purchase (``negative_lag``). ``invalid_date_values`` counts
    non-null values that could not be parsed as dates (they are also missing).
    ``valid + missing_any_date + negative_lag == total_rows``. Same-day installs
    (lag 0) are valid.
    """
    metadata = {
        **date_basis_metadata(is_proxy, purchase_column),
        "install_column": install_column,
        "lag_definition": "install date minus purchase date, in days",
        "censoring_note": CENSORING_NOTE,
    }
    excluded = dict(
        total_rows=0, valid=0, missing_purchase_date=0, missing_install_date=0,
        missing_any_date=0, negative_lag=0, invalid_date_values=0,
    )
    if data is None or data.empty:
        return LagAnalysis(
            pd.DataFrame(columns=LAG_COLUMNS), summarize_lags(pd.Series(dtype=float)),
            lag_histogram(pd.Series(dtype=float), bin_edges),
            pd.DataFrame(columns=COHORT_COLUMNS), excluded, metadata,
        )
    require_columns(data, [purchase_column, install_column])

    purchase, bad_p = parse_dates(data[purchase_column])
    install, bad_i = parse_dates(data[install_column])
    missing_p, missing_i = purchase.isna(), install.isna()
    missing_any = missing_p | missing_i
    lag = (install - purchase).dt.total_seconds() / 86_400
    negative = ~missing_any & (lag < 0)
    valid_mask = ~missing_any & ~negative
    lags = pd.DataFrame(
        {
            "purchase_date": purchase[valid_mask],
            "installed_at": install[valid_mask],
            "lag_days": lag[valid_mask],
        }
    )
    lags["cohort_month"] = lags["purchase_date"].dt.to_period("M").dt.to_timestamp()
    lags = lags[LAG_COLUMNS].reset_index(drop=True)

    excluded = dict(
        total_rows=int(len(data)),
        valid=int(valid_mask.sum()),
        missing_purchase_date=int(missing_p.sum()),
        missing_install_date=int(missing_i.sum()),
        missing_any_date=int(missing_any.sum()),
        negative_lag=int(negative.sum()),
        invalid_date_values=bad_p + bad_i,
    )
    purchased = (
        purchase.dropna().dt.to_period("M").dt.to_timestamp().value_counts().rename("purchased_devices")
    )
    cohort_trend = _cohort_trend(lags, purchased)
    return LagAnalysis(
        lags=lags,
        summary=summarize_lags(lags["lag_days"]),
        histogram=lag_histogram(lags["lag_days"], bin_edges),
        cohort_trend=cohort_trend,
        excluded=excluded,
        metadata=metadata,
    )


def _cohort_trend(lags: pd.DataFrame, purchased: pd.Series) -> pd.DataFrame:
    if purchased.empty:
        return pd.DataFrame(columns=COHORT_COLUMNS)
    stats = lags.groupby("cohort_month")["lag_days"].agg(
        installed_devices="size",
        mean_days="mean",
        median_days="median",
        p90_days=lambda s: s.quantile(0.9),
    )
    result = (
        purchased.rename_axis("cohort_month").to_frame()
        .join(stats, how="left")
        .fillna({"installed_devices": 0})
        .astype({"installed_devices": int})
        .sort_index()
        .reset_index()
    )
    return result[COHORT_COLUMNS]
