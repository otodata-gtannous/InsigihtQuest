"""Q2 - gaps by territory, customer size and product type."""

from __future__ import annotations

from datetime import date
from typing import Sequence

import numpy as np
import pandas as pd

from ._common import AnalysisResult, clean_labels, parse_dates, require_columns

DEFAULT_SIZE_EDGES = (10, 100)
UNASSIGNED_LABEL = "Unassigned"
BREAKDOWN_COLUMNS = [
    "group",
    "devices",
    "installed_devices",
    "not_installed_devices",
    "installed_rate_pct",
    "gap_vs_overall_pp",
    "installs_in_period",
    "companies",
    "share_of_devices_pct",
    "share_of_installs_pct",
    "small_group",
]


def _validated_edges(edges: Sequence[float]) -> list[float]:
    values = list(edges)
    if not values:
        raise ValueError("edges must contain at least one threshold.")
    if any(isinstance(e, bool) or not isinstance(e, (int, float, np.integer, np.floating)) or not np.isfinite(e) or e <= 0 for e in values):
        raise ValueError("edges must be positive numbers.")
    if any(b <= a for a, b in zip(values, values[1:])):
        raise ValueError("edges must be strictly ascending.")
    return values


def _fmt(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def default_size_labels(edges: Sequence[float] = DEFAULT_SIZE_EDGES) -> list[str]:
    """Small/Medium/Large for two edges, otherwise range labels such as '<10', '10-99', '100+'."""
    values = _validated_edges(edges)
    if len(values) == 2:
        return ["Small", "Medium", "Large"]
    labels = [f"<{_fmt(values[0])}"]
    for low, high in zip(values, values[1:]):
        upper = high - 1 if float(high).is_integer() else high
        labels.append(f"{_fmt(low)}-{_fmt(upper)}")
    labels.append(f"{_fmt(values[-1])}+")
    return labels


def customer_size_definitions(
    edges: Sequence[float] = DEFAULT_SIZE_EDGES, labels: Sequence[str] | None = None
) -> pd.DataFrame:
    """Legend for the buckets: [label, min_devices, max_devices, description]."""
    values = _validated_edges(edges)
    names = list(labels) if labels is not None else default_size_labels(values)
    if len(names) != len(values) + 1:
        raise ValueError("labels must have exactly len(edges) + 1 entries.")
    rows = []
    bounds = [None, *values, None]
    for i, name in enumerate(names):
        low, high = bounds[i], bounds[i + 1]
        if low is None:
            desc = f"fewer than {_fmt(high)} devices"
        elif high is None:
            desc = f"{_fmt(low)} or more devices"
        else:
            desc = f"{_fmt(low)} to fewer than {_fmt(high)} devices"
        rows.append({"label": name, "min_devices": low if low is not None else 0,
                     "max_devices_exclusive": high, "description": desc})
    rows.append({"label": UNASSIGNED_LABEL, "min_devices": None,
                 "max_devices_exclusive": None, "description": "device has no company"})
    return pd.DataFrame(rows)


def add_customer_size(
    data: pd.DataFrame,
    company_column: str,
    edges: Sequence[float] = DEFAULT_SIZE_EDGES,
    labels: Sequence[str] | None = None,
    output_column: str = "customer_size",
    count_column: str = "company_device_count",
) -> pd.DataFrame:
    """Copy of ``data`` with a customer-size bucket per device.

    A company's size is its number of device rows (one row per device). Buckets
    are left-closed: with edges (10, 100) a count of 9 is Small, 10 Medium and
    100 Large. Devices with a missing/blank company get 'Unassigned' and a null
    ``count_column``.
    """
    values = _validated_edges(edges)
    names = list(labels) if labels is not None else default_size_labels(values)
    if len(names) != len(values) + 1:
        raise ValueError("labels must have exactly len(edges) + 1 entries.")
    require_columns(data, [company_column])
    result = data.copy()
    if result.empty:
        result[count_column] = pd.Series(dtype="float64")
        result[output_column] = pd.Series(dtype="object")
        return result

    company = result[company_column]
    keys = company.astype("object").where(company.notna(), None)
    keys = keys.map(lambda v: str(v).strip() if v is not None else None)
    keys = keys.where(keys != "", None)
    counts = keys.map(keys.value_counts())
    bucket = np.searchsorted(np.asarray(values, dtype=float), counts.fillna(0).to_numpy(), side="right")
    sizes = pd.Series(np.asarray(names, dtype=object)[bucket], index=result.index)
    result[count_column] = counts.astype("float64")
    result[output_column] = sizes.where(keys.notna(), UNASSIGNED_LABEL)
    return result


def breakdown_by_group(
    data: pd.DataFrame,
    group_column: str,
    install_column: str,
    company_column: str | None = None,
    device_column: str | None = None,
    from_date: date | pd.Timestamp | None = None,
    to_date: date | pd.Timestamp | None = None,
    small_group_threshold: int = 5,
    missing_label: str = "Unknown",
) -> AnalysisResult:
    """Devices, installs and installed rate per group (territory, size, product...).

    One row per device is assumed (deduplicated on ``device_column`` when given).
    columns: group, devices, installed_devices (has an install date),
    not_installed_devices, installed_rate_pct (installed_devices / devices),
    gap_vs_overall_pp (rate minus overall rate), installs_in_period (installs
    inside from_date..to_date; all installs when no bounds), companies (distinct
    ``company_column`` values via nunique - never a sum of per-device values),
    share_of_devices_pct, share_of_installs_pct and small_group (devices <
    ``small_group_threshold``; nothing is suppressed). Metadata carries overall
    totals, total distinct companies and the threshold.
    """
    require_columns(data, [group_column, install_column, company_column, device_column])
    metadata = {
        "group_column": group_column,
        "small_group_threshold": small_group_threshold,
        "overall_devices": 0,
        "overall_installed_devices": 0,
        "overall_installed_rate_pct": None,
        "total_companies": 0,
        "rate_definition": "installed devices / devices in the group",
        "company_definition": "distinct company ids in the group (nunique)",
    }
    if data.empty:
        return AnalysisResult(pd.DataFrame(columns=BREAKDOWN_COLUMNS), metadata)

    frame = data.copy()
    if device_column is not None:
        frame = frame[frame[device_column].isna() | ~frame[device_column].duplicated()]
    installs, _ = parse_dates(frame[install_column])
    frame["_group"] = clean_labels(frame[group_column], missing_label)
    frame["_installed"] = installs.notna()
    in_period = installs.notna()
    if from_date is not None:
        in_period &= installs >= pd.Timestamp(from_date).normalize()
    if to_date is not None:
        upper = pd.Timestamp(to_date).normalize() + pd.offsets.Day(1)
        in_period &= installs < upper
    frame["_in_period"] = in_period
    if company_column is not None:
        company = frame[company_column].astype("object")
        company = company.map(lambda v: str(v).strip() if pd.notna(v) else None)
        frame["_company"] = company.where(company != "", None)

    grouped = frame.groupby("_group", sort=False)
    result = grouped.agg(
        devices=("_group", "size"),
        installed_devices=("_installed", "sum"),
        installs_in_period=("_in_period", "sum"),
    ).reset_index().rename(columns={"_group": "group"})
    if company_column is None:
        result["companies"] = pd.array([pd.NA] * len(result), dtype="Int64")
    else:
        result["companies"] = (
            grouped["_company"].nunique().reindex(result["group"]).to_numpy().astype(int)
        )

    total_devices = int(result["devices"].sum())
    total_installed = int(result["installed_devices"].sum())
    total_in_period = int(result["installs_in_period"].sum())
    overall_rate = total_installed / total_devices * 100 if total_devices else None
    result["not_installed_devices"] = result["devices"] - result["installed_devices"]
    result["installed_rate_pct"] = (result["installed_devices"] / result["devices"] * 100).round(2)
    result["gap_vs_overall_pp"] = (
        (result["installed_rate_pct"] - overall_rate).round(2) if overall_rate is not None else np.nan
    )
    result["share_of_devices_pct"] = (result["devices"] / total_devices * 100).round(2)
    result["share_of_installs_pct"] = (
        (result["installs_in_period"] / total_in_period * 100).round(2)
        if total_in_period
        else np.nan
    )
    result["small_group"] = result["devices"] < small_group_threshold
    result = result.sort_values(["devices", "group"], ascending=[False, True]).reset_index(drop=True)
    metadata.update(
        overall_devices=total_devices,
        overall_installed_devices=total_installed,
        overall_installed_rate_pct=round(overall_rate, 2) if overall_rate is not None else None,
        total_companies=int(frame["_company"].nunique()) if company_column is not None else 0,
    )
    return AnalysisResult(result[BREAKDOWN_COLUMNS], metadata)


def build_gap_breakdowns(
    data: pd.DataFrame,
    install_column: str,
    territory_column: str | None = None,
    product_column: str | None = None,
    company_column: str | None = None,
    device_column: str | None = None,
    from_date: date | pd.Timestamp | None = None,
    to_date: date | pd.Timestamp | None = None,
    size_edges: Sequence[float] = DEFAULT_SIZE_EDGES,
    size_labels: Sequence[str] | None = None,
    small_group_threshold: int = 5,
) -> dict[str, AnalysisResult]:
    """Run :func:`breakdown_by_group` for territory, customer_size and product_type.

    Only breakdowns whose source column is provided are returned (customer_size
    needs ``company_column``), keyed 'territory', 'customer_size', 'product_type'.
    """
    results: dict[str, AnalysisResult] = {}
    common = dict(
        install_column=install_column,
        company_column=company_column,
        device_column=device_column,
        from_date=from_date,
        to_date=to_date,
        small_group_threshold=small_group_threshold,
    )
    if territory_column is not None:
        results["territory"] = breakdown_by_group(data, territory_column, **common)
    if company_column is not None:
        sized = add_customer_size(data, company_column, size_edges, size_labels)
        result = breakdown_by_group(sized, "customer_size", **common)
        result.metadata["size_definitions"] = customer_size_definitions(size_edges, size_labels)
        results["customer_size"] = result
    if product_column is not None:
        results["product_type"] = breakdown_by_group(data, product_column, **common)
    return results
