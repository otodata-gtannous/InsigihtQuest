"""Q1 - peaks and lows of purchases and installations."""

from __future__ import annotations

from datetime import date

import pandas as pd

from ._common import (
    AnalysisResult,
    date_basis_metadata,
    parse_dates,
    period_calendar,
    require_columns,
    to_period_start,
    validate_period,
)

PEAK_LOW_COLUMNS = ["kind", "rank", "period_start", "value", "vs_mean_pct"]


def build_activity_series(
    data: pd.DataFrame,
    date_column: str,
    period: str = "Month",
    from_date: date | pd.Timestamp | None = None,
    to_date: date | pd.Timestamp | None = None,
    amount_column: str | None = None,
    measure: str = "auto",
    is_proxy: bool = False,
) -> AnalysisResult:
    """Gap-filled series of purchases (or installs) per Day/Week/Month.

    measure: 'count' counts rows with a valid date; 'amount' sums ``amount_column``;
    'auto' uses the amount when ``amount_column`` is given, otherwise counts rows.
    Returns data[period_start, value] and metadata (measure, period, is_proxy,
    date_basis_label, rows_used, rows_missing_date, rows_missing_amount,
    from_date, to_date).
    """
    validate_period(period)
    if measure not in {"auto", "count", "amount"}:
        raise ValueError("measure must be 'auto', 'count' or 'amount'.")
    if measure == "amount" and amount_column is None:
        raise ValueError("measure='amount' requires amount_column.")
    resolved = measure
    if measure == "auto":
        resolved = "amount" if amount_column is not None else "count"
    if (
        from_date is not None
        and to_date is not None
        and pd.Timestamp(from_date) > pd.Timestamp(to_date)
    ):
        raise ValueError("From date must be earlier than or equal to To date.")

    metadata = {
        "measure": resolved,
        "period": period,
        "amount_column": amount_column if resolved == "amount" else None,
        **date_basis_metadata(is_proxy, date_column),
    }
    columns = ["period_start", "value"]
    empty_meta = dict(
        rows_used=0, rows_missing_date=0, rows_missing_amount=0, from_date=None, to_date=None
    )
    if data is None or data.empty:
        return AnalysisResult(pd.DataFrame(columns=columns), {**metadata, **empty_meta})

    require_columns(data, [date_column, amount_column if resolved == "amount" else None])
    dates, _ = parse_dates(data[date_column])
    if resolved == "amount":
        amounts = pd.to_numeric(data[amount_column], errors="coerce")
    else:
        amounts = pd.Series(1.0, index=data.index)
    missing_date = dates.isna()
    missing_amount = amounts.isna() & ~missing_date
    frame = pd.DataFrame({"date": dates, "value": amounts.fillna(0.0)})[~missing_date].copy()
    frame["day"] = frame["date"].dt.normalize()

    start = pd.Timestamp(from_date).normalize() if from_date is not None else frame["day"].min()
    end = pd.Timestamp(to_date).normalize() if to_date is not None else frame["day"].max()
    if pd.isna(start) or pd.isna(end):
        meta = {**metadata, **empty_meta, "rows_missing_date": int(missing_date.sum())}
        return AnalysisResult(pd.DataFrame(columns=columns), meta)
    frame = frame[(frame["day"] >= start) & (frame["day"] <= end)].copy()

    calendar = period_calendar(start, end, period)
    frame["period_start"] = to_period_start(frame["day"], period)
    grouped = frame.groupby("period_start")["value"].sum().reindex(calendar, fill_value=0.0)
    result = grouped.rename_axis("period_start").rename("value").reset_index()
    if resolved == "count":
        result["value"] = result["value"].astype(int)
    metadata.update(
        rows_used=int(len(frame)),
        rows_missing_date=int(missing_date.sum()),
        rows_missing_amount=int(missing_amount.sum()),
        from_date=start,
        to_date=end,
    )
    return AnalysisResult(result[columns], metadata)


def rank_peaks_and_lows(
    series: pd.DataFrame,
    n: int = 5,
    value_column: str = "value",
    period_column: str = "period_start",
) -> pd.DataFrame:
    """Top ``n`` peaks and bottom ``n`` lows of a period series.

    Peaks and lows never overlap: ``n`` is capped at half the number of periods
    (at least 1), and ties are broken by the earliest period. A flat or empty
    series has no peaks or lows. Returns [kind, rank, period_start, value,
    vs_mean_pct]; vs_mean_pct is None when the mean is 0.
    """
    if n < 1:
        raise ValueError("n must be at least 1.")
    if series is None or series.empty:
        return pd.DataFrame(columns=PEAK_LOW_COLUMNS)
    frame = series[[period_column, value_column]].copy()
    frame.columns = ["period_start", "value"]
    frame["value"] = pd.to_numeric(frame["value"], errors="coerce")
    frame = frame.dropna().sort_values("period_start").reset_index(drop=True)
    if len(frame) < 2 or frame["value"].nunique() < 2:
        return pd.DataFrame(columns=PEAK_LOW_COLUMNS)

    mean = float(frame["value"].mean())
    n_eff = max(1, min(n, len(frame) // 2))
    peaks = frame.sort_values("value", ascending=False, kind="stable").head(n_eff)
    lows = (
        frame.drop(peaks.index)
        .sort_values("value", ascending=True, kind="stable")
        .head(n_eff)
    )
    parts = []
    for kind, part in (("peak", peaks), ("low", lows)):
        part = part.copy()
        part.insert(0, "kind", kind)
        part.insert(1, "rank", range(1, len(part) + 1))
        parts.append(part)
    result = pd.concat(parts, ignore_index=True)
    if mean != 0:
        result["vs_mean_pct"] = ((result["value"] - mean) / mean * 100).round(2)
    else:
        result["vs_mean_pct"] = None
    return result[PEAK_LOW_COLUMNS]


def compare_purchases_installs(
    data: pd.DataFrame,
    purchase_column: str,
    install_column: str,
    period: str = "Month",
    from_date: date | pd.Timestamp | None = None,
    to_date: date | pd.Timestamp | None = None,
    is_proxy: bool = False,
) -> AnalysisResult:
    """Purchases vs installs on one shared calendar (row counts, one row per device).

    Returns data[period_start, purchases, installs, net_change,
    cumulative_gap] where net_change = purchases - installs and cumulative_gap
    is the running total (devices bought but not yet installed within the
    window). Both series share the same calendar even when one side is empty.
    """
    validate_period(period)
    columns = ["period_start", "purchases", "installs", "net_change", "cumulative_gap"]
    metadata = {
        "measure": "count",
        "period": period,
        **date_basis_metadata(is_proxy, purchase_column),
    }
    if data is None or data.empty:
        return AnalysisResult(pd.DataFrame(columns=columns), metadata)
    require_columns(data, [purchase_column, install_column])

    p_dates, _ = parse_dates(data[purchase_column])
    i_dates, _ = parse_dates(data[install_column])
    all_days = pd.concat([p_dates, i_dates]).dropna().dt.normalize()
    if (from_date is None or to_date is None) and all_days.empty:
        return AnalysisResult(pd.DataFrame(columns=columns), metadata)
    start = pd.Timestamp(from_date) if from_date is not None else all_days.min()
    end = pd.Timestamp(to_date) if to_date is not None else all_days.max()

    purchases = build_activity_series(
        pd.DataFrame({"d": p_dates}), "d", period, start, end, is_proxy=is_proxy
    ).data.rename(columns={"value": "purchases"})
    installs = build_activity_series(
        pd.DataFrame({"d": i_dates}), "d", period, start, end
    ).data.rename(columns={"value": "installs"})
    merged = purchases.merge(installs, on="period_start", how="outer").sort_values("period_start")
    merged[["purchases", "installs"]] = merged[["purchases", "installs"]].fillna(0).astype(int)
    merged["net_change"] = merged["purchases"] - merged["installs"]
    merged["cumulative_gap"] = merged["net_change"].cumsum()
    metadata.update(from_date=start.normalize(), to_date=end.normalize())
    return AnalysisResult(merged[columns].reset_index(drop=True), metadata)
