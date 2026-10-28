"""Shared helpers for the pure analytics modules (no config/streamlit access)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

PERIODS = ("Day", "Week", "Month")

PROXY_LABEL = (
    "PurchaseDate is a warranty-start proxy for the purchase/ship date. "
    "It is not a true shipping date."
)
TRUE_DATE_LABEL = "Purchase/ship date taken as recorded."


@dataclass(frozen=True)
class AnalysisResult:
    """A result table plus the metadata the UI needs to explain it."""

    data: pd.DataFrame
    metadata: dict[str, Any] = field(default_factory=dict)


def validate_period(period: str) -> str:
    if period not in PERIODS:
        raise ValueError("Period must be Day, Week, or Month.")
    return period


def date_basis_metadata(is_proxy: bool, column: str | None = None) -> dict[str, Any]:
    """Metadata describing whether the purchase date is a proxy."""
    return {
        "is_proxy": bool(is_proxy),
        "date_basis_column": column,
        "date_basis_label": PROXY_LABEL if is_proxy else TRUE_DATE_LABEL,
    }


def require_columns(data: pd.DataFrame, columns: list[str | None]) -> None:
    missing = [c for c in columns if c is not None and c not in data.columns]
    if missing:
        raise KeyError(f"Missing required column(s): {', '.join(missing)}")


def parse_dates(series: pd.Series) -> tuple[pd.Series, int]:
    """Parse to datetime; returns (parsed, number of non-null values that were unparseable)."""
    parsed = pd.to_datetime(series, errors="coerce")
    if getattr(parsed.dt, "tz", None) is not None:
        parsed = parsed.dt.tz_localize(None)
    invalid = int((parsed.isna() & series.notna()).sum())
    return parsed, invalid


def to_period_start(dates: pd.Series, period: str) -> pd.Series:
    """Day -> the day, Week -> Monday, Month -> first of month (all normalized)."""
    validate_period(period)
    days = dates.dt.normalize()
    if period == "Day":
        return days
    if period == "Week":
        return days - pd.to_timedelta(days.dt.dayofweek, unit="D")
    return days.dt.to_period("M").dt.to_timestamp()


def period_calendar(start: pd.Timestamp, end: pd.Timestamp, period: str) -> pd.DatetimeIndex:
    """Every period start between two timestamps (inclusive of the periods they fall in)."""
    validate_period(period)
    first = to_period_start(pd.Series([pd.Timestamp(start)]), period).iloc[0]
    last = to_period_start(pd.Series([pd.Timestamp(end)]), period).iloc[0]
    freq = {"Day": "D", "Week": "7D", "Month": "MS"}[period]
    return pd.date_range(first, last, freq=freq)


def clean_labels(series: pd.Series, missing_label: str = "Unknown") -> pd.Series:
    labels = series.astype("object").where(series.notna(), None)
    labels = labels.map(lambda v: str(v).strip() if v is not None else "")
    return labels.where(labels != "", missing_label)


def safe_ratio(numerator: float, denominator: float) -> float | None:
    if denominator is None or not np.isfinite(denominator) or denominator == 0:
        return None
    return float(numerator) / float(denominator)
