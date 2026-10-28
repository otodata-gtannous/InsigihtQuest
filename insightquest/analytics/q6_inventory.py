"""Q6 - stockout / overstock risk simulated from device data."""

from __future__ import annotations

import math
from numbers import Real
from typing import Any, Mapping

import numpy as np
import pandas as pd

from ._common import AnalysisResult, clean_labels, parse_dates, require_columns

DEFAULT_PARAMS = {
    "lead_time_days": 30,
    "safety_days": 7,
    "max_cover_days": 120,
    "demand_window_days": 90,
}
OVERRIDABLE = ("lead_time_days", "safety_days", "max_cover_days")
STATUSES = ("stockout_risk", "reorder", "healthy", "overstock", "no_demand")
INVENTORY_COLUMNS = [
    "device_type",
    "total_devices",
    "on_hand",
    "installs_in_window",
    "demand_per_day",
    "days_of_cover",
    "lead_time_days",
    "safety_days",
    "max_cover_days",
    "reorder_point",
    "safety_stock",
    "reorder_level",
    "max_stock",
    "excess_units",
    "status",
    "projected_stockout_date",
]
STATUS_RULES = {
    "no_demand": "no installs in the demand window",
    "stockout_risk": "on_hand < demand_per_day x lead_time_days (reorder_point)",
    "overstock": "on_hand > demand_per_day x max_cover_days (max_stock)",
    "reorder": "on_hand <= reorder_point + demand_per_day x safety_days (reorder_level)",
    "healthy": "none of the above",
}
INVENTORY_CAVEAT = (
    "'On hand' = devices that have no install date (owned but not installed); it is not a "
    "warehouse stock count. Demand is the recent install rate and is a simulation input."
)


def _number(name: str, value: Any, allow_zero: bool) -> float:
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number, got {value!r}.")
    if value < 0 or (value == 0 and not allow_zero):
        qualifier = "zero or greater" if allow_zero else "greater than zero"
        raise ValueError(f"{name} must be {qualifier}, got {value}.")
    return float(value)


def validate_inventory_params(
    lead_time_days: float = DEFAULT_PARAMS["lead_time_days"],
    safety_days: float = DEFAULT_PARAMS["safety_days"],
    max_cover_days: float = DEFAULT_PARAMS["max_cover_days"],
    demand_window_days: float = DEFAULT_PARAMS["demand_window_days"],
) -> dict[str, float]:
    """Return the validated parameters or raise ValueError with a clear message.

    lead_time_days, max_cover_days and demand_window_days must be > 0,
    safety_days >= 0, and max_cover_days must exceed lead_time_days.
    """
    params = {
        "lead_time_days": _number("lead_time_days", lead_time_days, False),
        "safety_days": _number("safety_days", safety_days, True),
        "max_cover_days": _number("max_cover_days", max_cover_days, False),
        "demand_window_days": _number("demand_window_days", demand_window_days, False),
    }
    if params["max_cover_days"] <= params["lead_time_days"]:
        raise ValueError(
            "max_cover_days must be greater than lead_time_days "
            f"({params['max_cover_days']:g} <= {params['lead_time_days']:g})."
        )
    return params


def classify_stock(
    on_hand: float, demand_per_day: float, lead_time_days: float, safety_days: float, max_cover_days: float
) -> str:
    """Status for one device type (see STATUS_RULES; checked in that order)."""
    if demand_per_day <= 0:
        return "no_demand"
    if on_hand < demand_per_day * lead_time_days:
        return "stockout_risk"
    if on_hand > demand_per_day * max_cover_days:
        return "overstock"
    if on_hand <= demand_per_day * (lead_time_days + safety_days):
        return "reorder"
    return "healthy"


def simulate_inventory_risk(
    data: pd.DataFrame,
    device_type_column: str,
    install_column: str,
    as_of: pd.Timestamp | str | None = None,
    lead_time_days: float = DEFAULT_PARAMS["lead_time_days"],
    safety_days: float = DEFAULT_PARAMS["safety_days"],
    max_cover_days: float = DEFAULT_PARAMS["max_cover_days"],
    demand_window_days: float = DEFAULT_PARAMS["demand_window_days"],
    overrides: Mapping[str, Mapping[str, float]] | None = None,
    unknown_label: str = "Unknown",
) -> AnalysisResult:
    """Per device type: on-hand, demand, cover, thresholds, status, projected stock-out.

    on_hand = devices with no (valid) install date. demand_per_day = installs
    dated in (as_of - demand_window_days, as_of] / demand_window_days; ``as_of``
    defaults to the latest install date in the data. days_of_cover = on_hand /
    demand_per_day (NaN without demand). reorder_point = demand x lead_time_days;
    safety_stock = demand x safety_days; reorder_level = reorder_point +
    safety_stock; max_stock = demand x max_cover_days. projected_stockout_date =
    as_of + days_of_cover (as_of when nothing is on hand, NaT without demand).
    ``overrides`` maps device_type -> {lead_time_days|safety_days|max_cover_days:
    value} and is validated like the global parameters. Rows sort by severity
    (stockout_risk, reorder, healthy, overstock, no_demand) then device_type.
    """
    base = validate_inventory_params(lead_time_days, safety_days, max_cover_days, demand_window_days)
    overrides = dict(overrides or {})
    effective: dict[str, dict[str, float]] = {}
    for device_type, values in overrides.items():
        unknown = set(values) - set(OVERRIDABLE)
        if unknown:
            raise ValueError(
                f"Unsupported override(s) for '{device_type}': {', '.join(sorted(unknown))}. "
                f"Allowed: {', '.join(OVERRIDABLE)}."
            )
        merged = {**base, **values}
        try:
            effective[str(device_type)] = validate_inventory_params(**merged)
        except ValueError as ex:
            raise ValueError(f"Invalid override for '{device_type}': {ex}") from ex

    metadata: dict[str, Any] = {
        "params": base,
        "overrides": {k: dict(v) for k, v in overrides.items()},
        "unused_overrides": [],
        "as_of": None,
        "window_start": None,
        "status_rules": dict(STATUS_RULES),
        "status_counts": {s: 0 for s in STATUSES},
        "total_on_hand": 0,
        "invalid_install_dates": 0,
        "caveat": INVENTORY_CAVEAT,
    }
    if data is None or data.empty:
        metadata["unused_overrides"] = sorted(effective)
        return AnalysisResult(pd.DataFrame(columns=INVENTORY_COLUMNS), metadata)
    require_columns(data, [device_type_column, install_column])

    installs, invalid = parse_dates(data[install_column])
    metadata["invalid_install_dates"] = invalid
    as_of_ts = (
        pd.Timestamp(as_of).normalize()
        if as_of is not None
        else (installs.max().normalize() if installs.notna().any() else pd.Timestamp.today().normalize())
    )
    window_start = as_of_ts - pd.to_timedelta(base["demand_window_days"], unit="D")
    metadata.update(as_of=as_of_ts, window_start=window_start)

    frame = pd.DataFrame(
        {
            "device_type": clean_labels(data[device_type_column], unknown_label),
            "on_hand": installs.isna(),
            "in_window": installs.notna()
            & (installs.dt.normalize() > window_start)
            & (installs.dt.normalize() <= as_of_ts),
        }
    )
    grouped = frame.groupby("device_type").agg(
        total_devices=("on_hand", "size"),
        on_hand=("on_hand", "sum"),
        installs_in_window=("in_window", "sum"),
    ).reset_index()

    rows = []
    for rec in grouped.itertuples(index=False):
        params = effective.get(rec.device_type, base)
        demand = rec.installs_in_window / base["demand_window_days"]
        on_hand = int(rec.on_hand)
        reorder_point = demand * params["lead_time_days"]
        safety_stock = demand * params["safety_days"]
        cover = on_hand / demand if demand > 0 else np.nan
        status = classify_stock(
            on_hand, demand, params["lead_time_days"], params["safety_days"], params["max_cover_days"]
        )
        max_stock = demand * params["max_cover_days"]
        if demand > 0:
            stockout_date = as_of_ts + pd.to_timedelta(float(cover), unit="D")
            stockout_date = stockout_date.normalize()
        else:
            stockout_date = pd.NaT
        rows.append(
            {
                "device_type": rec.device_type,
                "total_devices": int(rec.total_devices),
                "on_hand": on_hand,
                "installs_in_window": int(rec.installs_in_window),
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
                "status": status,
                "projected_stockout_date": stockout_date,
            }
        )
    result = pd.DataFrame(rows, columns=INVENTORY_COLUMNS)
    order = {s: i for i, s in enumerate(STATUSES)}
    result = (
        result.assign(_o=result["status"].map(order))
        .sort_values(["_o", "device_type"])
        .drop(columns="_o")
        .reset_index(drop=True)
    )
    result["projected_stockout_date"] = pd.to_datetime(result["projected_stockout_date"])
    metadata["status_counts"] = {s: int((result["status"] == s).sum()) for s in STATUSES}
    metadata["total_on_hand"] = int(result["on_hand"].sum())
    metadata["unused_overrides"] = sorted(set(effective) - set(result["device_type"]))
    return AnalysisResult(result, metadata)
