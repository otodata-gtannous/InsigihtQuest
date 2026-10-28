"""Q4 - monthly installation rate."""

from __future__ import annotations

import numpy as np
import pandas as pd

from ._common import AnalysisResult, date_basis_metadata, parse_dates, require_columns

RATE_COLUMNS = [
    "month",
    "is_partial_month",
    "purchased",
    "installed_by_month_end",
    "cohort_rate_pct",
    "installed_cohort_to_date",
    "cohort_rate_to_date_pct",
    "negative_lag_devices",
    "installs_in_month",
    "purchased_to_date",
    "installed_to_date",
    "rate_vs_population_pct",
]

DENOMINATORS = {
    "cohort_rate_pct": (
        "devices from the cohort month (purchased in that month) that have an install date "
        "on or before the month-end / devices purchased in that month. Installs recorded "
        "before the purchase date are not counted as installed (see negative_lag_devices) "
        "but stay in the denominator."
    ),
    "cohort_rate_to_date_pct": (
        "same cohort numerator but using every install up to as_of / devices purchased in "
        "that month (cohort maturity)."
    ),
    "rate_vs_population_pct": (
        "all installs dated in the month (any purchase cohort) / devices purchased up to "
        "and including that month's end."
    ),
}


def _pct(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    """Percentage rounded to 2 decimals; NaN where the denominator is 0."""
    denom = denominator.astype(float).replace(0, np.nan)
    return (numerator.astype(float) / denom * 100).round(2)


def monthly_installation_rate(
    data: pd.DataFrame,
    purchase_column: str,
    install_column: str,
    as_of: pd.Timestamp | str | None = None,
    is_proxy: bool = False,
) -> AnalysisResult:
    """Monthly installation rate, two ways (see metadata['denominators']).

    One row per calendar month from the first purchase/install month to
    ``as_of`` (default: the latest date in the data). The month-end cut-off is
    capped at ``as_of`` and ``is_partial_month`` marks that last month.
    Zero denominators give NaN, never an error or inf. Rows lacking a purchase
    date are ignored by the purchase-based measures but their installs still
    count in ``installs_in_month``.
    """
    metadata = {
        **date_basis_metadata(is_proxy, purchase_column),
        "denominators": dict(DENOMINATORS),
        "as_of": None,
        "rows_total": 0,
        "rows_missing_purchase_date": 0,
        "rows_missing_install_date": 0,
        "negative_lag_devices": 0,
    }
    if data is None or data.empty:
        return AnalysisResult(pd.DataFrame(columns=RATE_COLUMNS), metadata)
    require_columns(data, [purchase_column, install_column])

    purchase, _ = parse_dates(data[purchase_column])
    install, _ = parse_dates(data[install_column])
    purchase, install = purchase.dt.normalize(), install.dt.normalize()
    known = pd.concat([purchase, install]).dropna()
    metadata.update(
        rows_total=int(len(data)),
        rows_missing_purchase_date=int(purchase.isna().sum()),
        rows_missing_install_date=int(install.isna().sum()),
    )
    if known.empty:
        return AnalysisResult(pd.DataFrame(columns=RATE_COLUMNS), metadata)

    as_of_ts = pd.Timestamp(as_of).normalize() if as_of is not None else known.max()
    metadata["as_of"] = as_of_ts
    first_month = known.min().to_period("M").to_timestamp()
    last_month = as_of_ts.to_period("M").to_timestamp()
    if last_month < first_month:
        return AnalysisResult(pd.DataFrame(columns=RATE_COLUMNS), metadata)
    months = pd.date_range(first_month, last_month, freq="MS")

    negative = install.notna() & purchase.notna() & (install < purchase)
    metadata["negative_lag_devices"] = int(negative.sum())
    valid_install = install.where(~negative)
    cohort = purchase.dt.to_period("M").dt.to_timestamp()
    install_month = install.dt.to_period("M").dt.to_timestamp()

    rows = []
    for month in months:
        month_end = month + pd.offsets.MonthEnd(0)
        cut = min(month_end, as_of_ts)
        in_cohort = cohort == month
        rows.append(
            {
                "month": month,
                "is_partial_month": bool(cut < month_end),
                "purchased": int(in_cohort.sum()),
                "installed_by_month_end": int((in_cohort & (valid_install <= cut)).sum()),
                "installed_cohort_to_date": int((in_cohort & (valid_install <= as_of_ts)).sum()),
                "negative_lag_devices": int((in_cohort & negative).sum()),
                "installs_in_month": int(((install_month == month) & (install <= cut)).sum()),
                "purchased_to_date": int((cohort <= month).sum()),
                "installed_to_date": int((install <= cut).sum()),
            }
        )
    result = pd.DataFrame(rows)
    result["cohort_rate_pct"] = _pct(result["installed_by_month_end"], result["purchased"])
    result["cohort_rate_to_date_pct"] = _pct(result["installed_cohort_to_date"], result["purchased"])
    result["rate_vs_population_pct"] = _pct(result["installs_in_month"], result["purchased_to_date"])
    return AnalysisResult(result[RATE_COLUMNS], metadata)
