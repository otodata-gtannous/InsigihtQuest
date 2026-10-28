from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from insightquest.analytics import analyze_install_lag, monthly_installation_rate
from insightquest.config import get_config
from insightquest.data import (
    fetch_lag_by_cohort,
    fetch_purchase_install_cohorts,
    summarize_lag_by_cohort,
)

CSV = Path(__file__).resolve().parents[1] / "demo_data.csv"


def rate_from_cohorts(cohorts: pd.DataFrame, as_of: pd.Timestamp) -> pd.DataFrame:
    """Reference adapter: monthly_installation_rate's count columns from cohort aggregates.

    Requires cohorts fetched with the same as_of (installs after it are censored).
    """
    known = pd.concat([cohorts["purchase_month"], cohorts["install_month"]]).dropna()
    months = pd.date_range(known.min(), as_of.to_period("M").to_timestamp(), freq="MS")
    rows = []
    for month in months:
        cohort = cohorts[cohorts["purchase_month"] == month]
        installed = cohort[cohort["install_month"].notna()]
        by_month_end = installed[installed["install_month"] <= month]
        rows.append({
            "month": month,
            "purchased": int(cohort["devices"].sum()),
            "installed_by_month_end": int(by_month_end["devices"].sum() - by_month_end["negative_lag_devices"].sum()),
            "installed_cohort_to_date": int(installed["devices"].sum() - installed["negative_lag_devices"].sum()),
            "negative_lag_devices": int(cohort["negative_lag_devices"].sum()),
            "installs_in_month": int(cohorts.loc[cohorts["install_month"] == month, "devices"].sum()),
            "purchased_to_date": int(cohorts.loc[cohorts["purchase_month"] <= month, "devices"].sum()),
            "installed_to_date": int(cohorts.loc[cohorts["install_month"] <= month, "devices"].sum()),
        })
    return pd.DataFrame(rows)


class CohortAnalyticsFeedTests(unittest.TestCase):
    """Cohort aggregates reproduce the analytics outputs computed from raw rows."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.raw = pd.read_csv(CSV)

    def setUp(self) -> None:
        self.config = replace(
            get_config(), demo_mode=True, demo_csv_path=CSV,
            col_installed_at="TankInstallDate", col_sales_date="PurchaseDate",
            col_shipped_at="PurchaseDate", col_territory="Territory",
            col_customer="CompanyId", col_product_type="DeviceType",
        )

    def test_monthly_installation_rate_counts_match_raw_row_analytics(self) -> None:
        for as_of in (pd.Timestamp("2026-06-15"), pd.Timestamp("2026-12-31")):
            with self.subTest(as_of=as_of):
                expected = monthly_installation_rate(
                    self.raw, "PurchaseDate", "TankInstallDate", as_of=as_of,
                ).data
                cohorts = fetch_purchase_install_cohorts(None, self.config, as_of=as_of)
                actual = rate_from_cohorts(cohorts, as_of)
                columns = [c for c in actual.columns if c != "month"]
                self.assertEqual(list(actual["month"]), list(expected["month"]))
                for column in columns:
                    self.assertEqual(list(actual[column]), list(expected[column].astype(int)), column)
                pct = actual["installed_by_month_end"] / actual["purchased"].replace(0, np.nan) * 100
                np.testing.assert_allclose(
                    pct.round(2).to_numpy(float), expected["cohort_rate_pct"].to_numpy(float), equal_nan=True,
                )

    def test_install_lag_cohort_trend_matches_raw_row_analytics(self) -> None:
        analysis = analyze_install_lag(self.raw, "PurchaseDate", "TankInstallDate")
        lag = fetch_lag_by_cohort(None, self.config, basis="purchased")
        summary = summarize_lag_by_cohort(lag).set_index("cohort_month")
        trend = analysis.cohort_trend.set_index("cohort_month")
        installed = trend[trend["installed_devices"] > 0]
        self.assertEqual(list(summary.index[summary["devices"] > 0]), list(installed.index))
        self.assertEqual(list(summary.loc[installed.index, "devices"]), list(installed["installed_devices"]))
        for ours, theirs in (("mean_days", "mean_days"), ("median_days", "median_days"), ("p90_days", "p90_days")):
            np.testing.assert_allclose(
                summary.loc[installed.index, ours].to_numpy(float), installed[theirs].to_numpy(float),
            )
        self.assertEqual(int(summary["devices"].sum()), analysis.excluded["valid"])
        self.assertEqual(int(summary["negative_lag_devices"].sum()), analysis.excluded["negative_lag"])
        purchased = fetch_purchase_install_cohorts(None, self.config).dropna(subset=["purchase_month"])
        by_month = purchased.groupby("purchase_month")["devices"].sum()
        self.assertEqual(list(trend["purchased_devices"]), list(by_month.reindex(trend.index)))


if __name__ == "__main__":
    unittest.main()
