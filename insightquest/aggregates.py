"""Adapters that turn SQL-side aggregates (see insightquest.cache) into analytics inputs.

Views never loop over raw rows: the database (or the demo reference implementation)
returns small count tables and these pure helpers reshape them for the analytics
package. Everything here is Streamlit-free and unit-tested.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from insightquest.analytics import (
    DEFAULT_INVENTORY_PARAMS,
    AnalysisResult,
    MarketPredictability,
    assess_market_predictability,
    classify_stock,
    summarize_lags,
    lag_histogram,
    validate_inventory_params,
)
from insightquest.analytics._common import date_basis_metadata, period_calendar
from insightquest.analytics.q2_segments import BREAKDOWN_COLUMNS
from insightquest.analytics.q3_lag import COHORT_COLUMNS
from insightquest.analytics.q4_rate import DENOMINATORS as RATE_DENOMINATORS, RATE_COLUMNS
from insightquest.analytics.q6_inventory import INVENTORY_CAVEAT, INVENTORY_COLUMNS, STATUS_RULES, STATUSES
from insightquest.data import summarize_lag_by_cohort

PERIOD_GRAIN = {"Day": "day", "Week": "week", "Month": "month"}


def counts_to_series(
    counts: pd.DataFrame,
    period: str,
    from_date: Any = None,
    to_date: Any = None,
    value_column: str = "value",
) -> pd.DataFrame:
    """Gap-filled [period_start, value] series from fetch_event_counts output (no group_by)."""
    columns = ["period_start", value_column]
    if counts is None or counts.empty:
        if from_date is None or to_date is None:
            return pd.DataFrame(columns=columns)
        counts = pd.DataFrame({"period": [], "events": []})
    frame = counts.copy()
    frame["period"] = pd.to_datetime(frame["period"])
    start = pd.Timestamp(from_date) if from_date is not None else frame["period"].min()
    end = pd.Timestamp(to_date) if to_date is not None else frame["period"].max()
    calendar = period_calendar(start, end, period)
    summed = frame.groupby("period")["events"].sum().reindex(calendar, fill_value=0)
    return summed.astype("int64").rename_axis("period_start").rename(value_column).reset_index()


def compare_event_series(
    purchases: pd.DataFrame | None, installs: pd.DataFrame | None
) -> pd.DataFrame:
    """Outer-join purchase and install series on one calendar with net change and running gap."""
    columns = ["period_start", "purchases", "installs", "net_change", "cumulative_gap"]
    parts = []
    if purchases is not None and not purchases.empty:
        parts.append(purchases.rename(columns={"value": "purchases"}))
    if installs is not None and not installs.empty:
        parts.append(installs.rename(columns={"value": "installs"}))
    if not parts:
        return pd.DataFrame(columns=columns)
    merged = parts[0]
    for part in parts[1:]:
        merged = merged.merge(part, on="period_start", how="outer")
    for column in ("purchases", "installs"):
        if column not in merged:
            merged[column] = 0
    merged = merged.sort_values("period_start").reset_index(drop=True)
    merged[["purchases", "installs"]] = merged[["purchases", "installs"]].fillna(0).astype("int64")
    merged["net_change"] = merged["purchases"] - merged["installs"]
    merged["cumulative_gap"] = merged["net_change"].cumsum()
    return merged[columns]


def assess_markets_from_counts(counts: pd.DataFrame, **options: Any) -> MarketPredictability:
    """assess_market_predictability fed by monthly (period, segment, events) counts, with no row expansion.

    Each aggregate row is passed as one 'amount' row whose value is its event count, so every
    series, score, growth and volatility figure is identical to the one-row-per-purchase run.
    Only the row-based fields differ by construction, so they are restored here: purchase_rows
    (= purchases, since one device is one purchase), small_market and the rows_used metadata.
    """
    small_rows = options.get("small_market_min_rows", 20)
    small_share = options.get("small_market_share_pct", 2.0)
    for forbidden in ("amount_column", "measure"):
        options.pop(forbidden, None)
    frame = pd.DataFrame(
        {
            "month": pd.to_datetime(counts["period"]) if counts is not None and not counts.empty else [],
            "market": counts["segment"].astype(str) if counts is not None and not counts.empty else [],
            "events": counts["events"] if counts is not None and not counts.empty else [],
        }
    )
    result = assess_market_predictability(
        frame, "month", "market", amount_column="events", measure="amount", **options
    )
    table = result.table.copy()
    if not table.empty:
        table["purchase_rows"] = table["total_value"].round().astype("int64")
        table["small_market"] = (table["purchase_rows"] < small_rows) | (
            table["share_pct"].notna() & (table["share_pct"] < small_share)
        )
    metadata = {**result.metadata, "measure": "count", "amount_column": None}
    if metadata.get("rows_used"):
        metadata["rows_used"] = int(frame["events"].sum()) - int(
            frame.loc[frame["month"] > metadata["last_month"], "events"].sum()
        )
    return MarketPredictability(table, result.forecasts, result.overall, metadata)


def rate_from_cohorts(cohorts: pd.DataFrame, as_of: Any, is_proxy: bool = False) -> AnalysisResult:
    """monthly_installation_rate from fetch_purchase_install_cohorts (identical output).

    ``cohorts`` must be fetched with the same ``as_of`` so installs after it are censored.
    Returns data with RATE_COLUMNS and the same metadata as the row-based function, except that
    rows_missing_install_date also counts devices installed after ``as_of``.
    """
    as_of_ts = pd.Timestamp(as_of).normalize()
    metadata: dict[str, Any] = {
        **date_basis_metadata(is_proxy, "purchase_month"),
        "denominators": dict(RATE_DENOMINATORS),
        "as_of": as_of_ts,
        "rows_total": 0,
        "rows_missing_purchase_date": 0,
        "rows_missing_install_date": 0,
        "negative_lag_devices": 0,
    }
    if cohorts is None or cohorts.empty:
        return AnalysisResult(pd.DataFrame(columns=RATE_COLUMNS), metadata)
    purchase_month = pd.to_datetime(cohorts["purchase_month"])
    install_month = pd.to_datetime(cohorts["install_month"])
    devices = cohorts["devices"].astype("int64")
    negatives = cohorts["negative_lag_devices"].astype("int64")
    metadata.update(
        rows_total=int(devices.sum()),
        rows_missing_purchase_date=int(devices[purchase_month.isna()].sum()),
        rows_missing_install_date=int(devices[install_month.isna()].sum()),
        negative_lag_devices=int(negatives.sum()),
    )
    known = pd.concat([purchase_month, install_month]).dropna()
    last_month = as_of_ts.to_period("M").to_timestamp()
    if known.empty or last_month < known.min():
        return AnalysisResult(pd.DataFrame(columns=RATE_COLUMNS), metadata)

    rows = []
    for month in pd.date_range(known.min(), last_month, freq="MS"):
        in_cohort = purchase_month == month
        installed = in_cohort & install_month.notna()
        by_month_end = installed & (install_month <= month)
        rows.append(
            {
                "month": month,
                "is_partial_month": bool(as_of_ts < month + pd.offsets.MonthEnd(0)),
                "purchased": int(devices[in_cohort].sum()),
                "installed_by_month_end": int(devices[by_month_end].sum() - negatives[by_month_end].sum()),
                "installed_cohort_to_date": int(devices[installed].sum() - negatives[installed].sum()),
                "negative_lag_devices": int(negatives[in_cohort].sum()),
                "installs_in_month": int(devices[install_month == month].sum()),
                "purchased_to_date": int(devices[purchase_month <= month].sum()),
                "installed_to_date": int(devices[install_month <= month].sum()),
            }
        )
    result = pd.DataFrame(rows)
    result["cohort_rate_pct"] = _pct(result["installed_by_month_end"], result["purchased"])
    result["cohort_rate_to_date_pct"] = _pct(result["installed_cohort_to_date"], result["purchased"])
    result["rate_vs_population_pct"] = _pct(result["installs_in_month"], result["purchased_to_date"])
    return AnalysisResult(result[RATE_COLUMNS], metadata)


def _pct(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    return (numerator.astype(float) / denominator.astype(float).replace(0, np.nan) * 100).round(2)


def cohort_trend_from_lag(lag_by_cohort: pd.DataFrame, purchased: pd.Series | None = None) -> pd.DataFrame:
    """Per-cohort lag statistics from fetch_lag_by_cohort (same columns as analyze_install_lag's cohort_trend).

    ``purchased`` (optional): devices per cohort month, e.g. summed from
    fetch_purchase_install_cohorts. With it every purchased cohort is listed
    (installed_devices 0 when none) and COHORT_COLUMNS are returned; without it only cohorts
    with at least one valid lag appear and the purchased_devices column is omitted.
    """
    summary = summarize_lag_by_cohort(lag_by_cohort)
    stats = summary.rename(columns={"devices": "installed_devices"}).set_index("cohort_month")
    stats = stats[stats["installed_devices"] > 0]
    if purchased is None:
        return stats.reset_index()[["cohort_month", "installed_devices", "mean_days", "median_days", "p90_days"]]
    base = purchased.rename("purchased_devices").rename_axis("cohort_month").to_frame()
    result = (
        base.join(stats[["installed_devices", "mean_days", "median_days", "p90_days"]], how="left")
        .fillna({"installed_devices": 0})
        .astype({"installed_devices": int})
        .sort_index()
        .reset_index()
    )
    return result[COHORT_COLUMNS]


def lag_distribution_from_cohorts(lag_by_cohort: pd.DataFrame) -> pd.DataFrame:
    """Collapse fetch_lag_by_cohort to the (lag_days, devices) histogram used by lag_analysis_from_distribution."""
    if lag_by_cohort is None or lag_by_cohort.empty:
        return pd.DataFrame({"lag_days": pd.Series(dtype="int64"), "devices": pd.Series(dtype="int64")})
    return lag_by_cohort.groupby("lag_days", as_index=False)["devices"].sum().sort_values("lag_days").reset_index(drop=True)


def total_by_month(counts: pd.DataFrame, end_month: pd.Timestamp | None = None) -> pd.DataFrame:
    """Gap-filled monthly total [period_start, value] from per-segment monthly counts."""
    if counts is None or counts.empty:
        return pd.DataFrame(columns=["period_start", "value"])
    frame = counts.assign(period=pd.to_datetime(counts["period"]))
    totals = frame.groupby("period")["events"].sum()
    last = totals.index.max() if end_month is None else min(pd.Timestamp(end_month), totals.index.max())
    calendar = pd.date_range(totals.index.min(), last, freq="MS")
    series = totals.reindex(calendar, fill_value=0).astype("int64")
    return series.rename_axis("period_start").rename("value").reset_index()


def monthly_seasonality(monthly: pd.DataFrame, value_column: str = "installs") -> pd.DataFrame:
    """Average count per calendar month across years from a monthly [period_start, installs] series."""
    columns = ["month_number", "month_name", "avg_installs", "total_installs"]
    if monthly is None or monthly.empty:
        return pd.DataFrame(columns=columns)
    frame = monthly.assign(month_number=pd.to_datetime(monthly["period_start"]).dt.month)
    summary = (
        frame.groupby("month_number")[value_column]
        .agg(avg_installs="mean", total_installs="sum")
        .reset_index()
    )
    summary["month_name"] = pd.to_datetime(summary["month_number"], format="%m").dt.strftime("%b")
    summary["avg_installs"] = summary["avg_installs"].round(2)
    return summary[columns]


def breakdown_from_segment_counts(
    segments: pd.DataFrame,
    companies: pd.DataFrame | None = None,
    small_group_threshold: int = 5,
    order: tuple[str, ...] | None = None,
) -> AnalysisResult:
    """Per-group installed rate and gap from fetch_segment_counts (same columns as breakdown_by_group).

    devices/not_installed are all-time; purchased/installed are inside the date range.
    ``companies`` is the optional fetch_customer_size_distribution table (customer size only);
    ``order`` pins a display order (e.g. Small, Medium, Large) instead of sorting by devices.
    """
    metadata: dict[str, Any] = {
        "small_group_threshold": small_group_threshold,
        "overall_devices": 0,
        "overall_installed_devices": 0,
        "overall_installed_rate_pct": None,
        "total_companies": 0,
        "rate_definition": "installed devices / devices in the group",
        "company_definition": "distinct company ids in the group",
    }
    if segments is None or segments.empty:
        return AnalysisResult(pd.DataFrame(columns=BREAKDOWN_COLUMNS), metadata)

    frame = segments.rename(columns={"segment": "group"}).copy()
    frame["installed_devices"] = frame["devices"] - frame["not_installed"]
    total_devices = int(frame["devices"].sum())
    total_installed = int(frame["installed_devices"].sum())
    total_in_period = int(frame["installed"].sum())
    overall_rate = total_installed / total_devices * 100 if total_devices else None

    result = pd.DataFrame(
        {
            "group": frame["group"].astype(str),
            "devices": frame["devices"].astype("int64"),
            "installed_devices": frame["installed_devices"].astype("int64"),
            "not_installed_devices": frame["not_installed"].astype("int64"),
        }
    )
    result["installed_rate_pct"] = (result["installed_devices"] / result["devices"] * 100).round(2)
    result["gap_vs_overall_pp"] = (
        (result["installed_rate_pct"] - overall_rate).round(2) if overall_rate is not None else np.nan
    )
    result["installs_in_period"] = frame["installed"].astype("int64").to_numpy()
    if companies is not None and not companies.empty:
        lookup = companies.set_index(companies["segment"].astype(str))["companies"]
        result["companies"] = result["group"].map(lookup).astype("Int64")
        metadata["total_companies"] = int(companies["companies"].sum())
    else:
        result["companies"] = pd.array([pd.NA] * len(result), dtype="Int64")
    result["share_of_devices_pct"] = (result["devices"] / total_devices * 100).round(2)
    result["share_of_installs_pct"] = (
        (result["installs_in_period"] / total_in_period * 100).round(2) if total_in_period else np.nan
    )
    result["small_group"] = result["devices"] < small_group_threshold
    result = result.sort_values(["devices", "group"], ascending=[False, True]).reset_index(drop=True)
    if order:
        rank = {name: position for position, name in enumerate(order)}
        result = (
            result.assign(_order=result["group"].map(lambda name: rank.get(name, len(rank))))
            .sort_values(["_order", "group"])
            .drop(columns="_order")
            .reset_index(drop=True)
        )
    metadata.update(
        overall_devices=total_devices,
        overall_installed_devices=total_installed,
        overall_installed_rate_pct=round(overall_rate, 2) if overall_rate is not None else None,
    )
    return AnalysisResult(result[BREAKDOWN_COLUMNS], metadata)


def lag_analysis_from_distribution(distribution: pd.DataFrame, bin_edges: tuple[int, ...] | None = None) -> dict[str, Any]:
    """Summary, histogram and exclusion counts from fetch_shipping_lag_distribution.

    Returns {'summary', 'histogram', 'valid', 'negative_lag', 'total'}; negative lags
    (installed before shipped) are excluded from the statistics, as in analyze_install_lag.
    """
    empty = distribution is None or distribution.empty
    lags = np.array([], dtype=float)
    negative = 0
    total = 0
    if not empty:
        counts = distribution["devices"].to_numpy(dtype=np.int64)
        values = distribution["lag_days"].to_numpy(dtype=float)
        total = int(counts.sum())
        negative = int(counts[values < 0].sum())
        lags = np.repeat(values[values >= 0], counts[values >= 0])
    series = pd.Series(lags, dtype=float)
    kwargs = {"bin_edges": bin_edges} if bin_edges else {}
    return {
        "summary": summarize_lags(series),
        "histogram": lag_histogram(series, **kwargs),
        "valid": int(len(series)),
        "negative_lag": negative,
        "total": total,
    }


def inventory_from_summary(
    summary: pd.DataFrame,
    as_of: Any,
    lead_time_days: float = DEFAULT_INVENTORY_PARAMS["lead_time_days"],
    safety_days: float = DEFAULT_INVENTORY_PARAMS["safety_days"],
    max_cover_days: float = DEFAULT_INVENTORY_PARAMS["max_cover_days"],
    demand_window_days: float = DEFAULT_INVENTORY_PARAMS["demand_window_days"],
) -> AnalysisResult:
    """Stock status per product type from fetch_inventory_summary (same rules as simulate_inventory_risk).

    ``summary`` must have been fetched with trailing_days=(demand_window_days,) so that its
    ``installs_{N}d`` column is the demand window. Raises ValueError for invalid parameters.
    """
    params = validate_inventory_params(lead_time_days, safety_days, max_cover_days, demand_window_days)
    as_of_ts = pd.Timestamp(as_of).normalize()
    window_column = f"installs_{int(params['demand_window_days'])}d"
    metadata: dict[str, Any] = {
        "params": params,
        "as_of": as_of_ts,
        "status_rules": dict(STATUS_RULES),
        "status_counts": {status: 0 for status in STATUSES},
        "total_on_hand": 0,
        "caveat": INVENTORY_CAVEAT,
    }
    if summary is None or summary.empty:
        return AnalysisResult(pd.DataFrame(columns=INVENTORY_COLUMNS), metadata)
    if window_column not in summary.columns:
        raise ValueError(f"The summary has no {window_column} column for the demand window.")

    rows = []
    for record in summary.to_dict("records"):
        installs = int(record[window_column])
        on_hand = int(record["on_hand"])
        demand = installs / params["demand_window_days"]
        reorder_point = demand * params["lead_time_days"]
        safety_stock = demand * params["safety_days"]
        max_stock = demand * params["max_cover_days"]
        cover = on_hand / demand if demand > 0 else np.nan
        stockout = (as_of_ts + pd.to_timedelta(cover, unit="D")).normalize() if demand > 0 else pd.NaT
        rows.append(
            {
                "device_type": str(record["product_type"]),
                "total_devices": int(record["total_devices"]),
                "on_hand": on_hand,
                "installs_in_window": installs,
                "demand_per_day": demand,
                "days_of_cover": cover,
                "lead_time_days": params["lead_time_days"],
                "safety_days": params["safety_days"],
                "max_cover_days": params["max_cover_days"],
                "reorder_point": reorder_point,
                "safety_stock": safety_stock,
                "reorder_level": reorder_point + safety_stock,
                "max_stock": max_stock,
                "excess_units": max(0.0, on_hand - max_stock) if demand > 0 else 0.0,
                "status": classify_stock(
                    on_hand, demand, params["lead_time_days"], params["safety_days"], params["max_cover_days"]
                ),
                "projected_stockout_date": stockout,
            }
        )
    result = pd.DataFrame(rows, columns=INVENTORY_COLUMNS)
    order = {status: position for position, status in enumerate(STATUSES)}
    result = (
        result.assign(_order=result["status"].map(order))
        .sort_values(["_order", "device_type"])
        .drop(columns="_order")
        .reset_index(drop=True)
    )
    result["projected_stockout_date"] = pd.to_datetime(result["projected_stockout_date"])
    metadata["status_counts"] = {status: int((result["status"] == status).sum()) for status in STATUSES}
    metadata["total_on_hand"] = int(result["on_hand"].sum())
    return AnalysisResult(result, metadata)
