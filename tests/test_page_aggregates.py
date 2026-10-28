"""The aggregate adapters must agree with the row-based analytics they replace."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from insightquest import data
from insightquest.aggregates import (
    assess_markets_from_counts,
    breakdown_from_segment_counts,
    cohort_trend_from_lag,
    compare_event_series,
    counts_to_series,
    inventory_from_summary,
    lag_analysis_from_distribution,
    lag_distribution_from_cohorts,
    monthly_seasonality,
    rate_from_cohorts,
    total_by_month,
)
from insightquest.analytics import (
    analyze_install_lag,
    assess_market_predictability,
    breakdown_by_group,
    build_activity_series,
    monthly_installation_rate,
    simulate_inventory_risk,
)
from insightquest.config import get_config

CSV = Path(__file__).resolve().parents[1] / "demo_data.csv"
FROM, TO = date(2024, 1, 1), date(2026, 6, 30)


def expand_counts_to_rows(counts: pd.DataFrame) -> pd.DataFrame:
    """Reference: one row per purchase, the input assess_market_predictability natively takes."""
    index = np.repeat(np.arange(len(counts)), counts["events"].to_numpy(dtype=np.int64))
    return pd.DataFrame({
        "month": pd.to_datetime(counts["period"]).to_numpy()[index],
        "market": counts["segment"].astype(str).to_numpy()[index],
    })


class AggregateParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = replace(
            get_config(), demo_mode=True, demo_csv_path=CSV, col_device="Id", col_customer="CompanyId",
            col_installed_at="TankInstallDate", col_sales_date="PurchaseDate", col_shipped_at="PurchaseDate",
            col_territory="Territory", col_product_type="DeviceType", customer_size_edges=(10, 100),
        )
        cls.rows = pd.read_csv(CSV)

    def test_event_series_matches_build_activity_series(self) -> None:
        counts = data.fetch_event_counts(None, self.config, "purchases", "month", FROM, TO)
        series = counts_to_series(counts, "Month", FROM, TO)
        expected = build_activity_series(self.rows, "PurchaseDate", "Month", FROM, TO, measure="count").data
        self.assertEqual(series["value"].tolist(), expected["value"].tolist())
        self.assertEqual(series["period_start"].tolist(), expected["period_start"].tolist())

    def test_empty_counts_fill_the_requested_calendar_with_zeros(self) -> None:
        series = counts_to_series(pd.DataFrame(columns=["period", "events"]), "Month", FROM, date(2024, 3, 31))
        self.assertEqual(series["value"].tolist(), [0, 0, 0])

    def test_compare_event_series_tracks_cumulative_gap(self) -> None:
        purchases = pd.DataFrame({"period_start": pd.to_datetime(["2024-01-01", "2024-02-01"]), "value": [5, 1]})
        installs = pd.DataFrame({"period_start": pd.to_datetime(["2024-02-01", "2024-03-01"]), "value": [2, 4]})
        merged = compare_event_series(purchases, installs)
        self.assertEqual(merged["net_change"].tolist(), [5, -1, -4])
        self.assertEqual(merged["cumulative_gap"].tolist(), [5, 4, 0])
        self.assertTrue(compare_event_series(None, None).empty)

    def test_segment_breakdown_matches_row_breakdown(self) -> None:
        segments = data.fetch_segment_counts(None, self.config, "territory", FROM, TO)
        actual = breakdown_from_segment_counts(segments).data.set_index("group")
        expected = breakdown_by_group(
            self.rows, "Territory", "TankInstallDate", "CompanyId", "Id", FROM, TO,
            missing_label="Unknown Territory",
        ).data.set_index("group")
        self.assertEqual(set(actual.index), set(expected.index))
        for column in ("devices", "installed_devices", "not_installed_devices", "installs_in_period"):
            self.assertEqual(actual.loc[expected.index, column].tolist(), expected[column].tolist(), column)
        np.testing.assert_allclose(
            actual.loc[expected.index, "installed_rate_pct"], expected["installed_rate_pct"], atol=0.01
        )
        np.testing.assert_allclose(
            actual.loc[expected.index, "gap_vs_overall_pp"], expected["gap_vs_overall_pp"], atol=0.01
        )

    def test_customer_size_breakdown_keeps_company_counts_and_order(self) -> None:
        segments = data.fetch_segment_counts(None, self.config, "customer_size", FROM, TO)
        companies = data.fetch_customer_size_distribution(None, self.config)
        result = breakdown_from_segment_counts(segments, companies, order=data.customer_size_order(self.config))
        order = list(data.customer_size_order(self.config))
        groups = result.data["group"].tolist()
        self.assertEqual(groups, sorted(groups, key=order.index))
        self.assertEqual(int(result.data["companies"].sum()), int(companies["companies"].sum()))

    def test_empty_segments_give_empty_breakdown(self) -> None:
        self.assertTrue(breakdown_from_segment_counts(pd.DataFrame()).data.empty)

    def test_lag_distribution_matches_row_analysis(self) -> None:
        distribution = data.fetch_shipping_lag_distribution(None, self.config)
        actual = lag_analysis_from_distribution(distribution)
        expected = analyze_install_lag(self.rows, "PurchaseDate", "TankInstallDate")
        self.assertEqual(actual["valid"], expected.excluded["valid"])
        self.assertEqual(actual["negative_lag"], expected.excluded["negative_lag"])
        self.assertAlmostEqual(actual["summary"]["median_days"], expected.summary["median_days"], places=6)
        self.assertAlmostEqual(actual["summary"]["p90_days"], expected.summary["p90_days"], places=6)
        self.assertEqual(actual["histogram"]["count"].tolist(), expected.histogram["count"].tolist())

    def test_empty_lag_distribution(self) -> None:
        actual = lag_analysis_from_distribution(pd.DataFrame(columns=["lag_days", "devices"]))
        self.assertEqual((actual["valid"], actual["total"], actual["negative_lag"]), (0, 0, 0))

    def test_inventory_matches_row_simulation(self) -> None:
        as_of = date(2026, 9, 30)
        params = dict(lead_time_days=21, safety_days=5, max_cover_days=100, demand_window_days=60)
        summary = data.fetch_inventory_summary(None, self.config, as_of, (60,))
        actual = inventory_from_summary(summary, as_of, **params).data.set_index("device_type")
        expected = simulate_inventory_risk(
            self.rows, "DeviceType", "TankInstallDate", as_of=pd.Timestamp(as_of), **params
        ).data.set_index("device_type")
        self.assertEqual(set(actual.index), set(expected.index))
        for column in ("on_hand", "installs_in_window", "status"):
            self.assertEqual(actual.loc[expected.index, column].tolist(), expected[column].tolist(), column)
        for column in ("demand_per_day", "days_of_cover", "reorder_point", "max_stock", "excess_units"):
            np.testing.assert_allclose(actual.loc[expected.index, column], expected[column], equal_nan=True)
        self.assertEqual(
            actual.loc[expected.index, "projected_stockout_date"].tolist(),
            expected["projected_stockout_date"].tolist(),
        )

    def test_inventory_rejects_invalid_parameters(self) -> None:
        summary = data.fetch_inventory_summary(None, self.config, date(2026, 9, 30), (90,))
        with self.assertRaises(ValueError):
            inventory_from_summary(summary, date(2026, 9, 30), lead_time_days=60, max_cover_days=30)

    def test_expanded_rows_preserve_counts_for_market_analytics(self) -> None:
        counts = data.fetch_monthly_purchase_counts(None, self.config, "territory", FROM, TO)
        rows = expand_counts_to_rows(counts)
        self.assertEqual(len(rows), int(counts["events"].sum()))
        self.assertEqual(set(rows["market"]), set(counts["segment"].astype(str)))

    def test_markets_from_counts_match_the_one_row_per_purchase_run(self) -> None:
        for dimension in ("territory", "product_type"):
            with self.subTest(dimension=dimension):
                counts = data.fetch_monthly_purchase_counts(None, self.config, dimension, FROM, TO)
                end = pd.Timestamp("2026-08-01")
                actual = assess_markets_from_counts(counts, as_of=end, horizon=3)
                expected = assess_market_predictability(
                    expand_counts_to_rows(counts), "month", "market", as_of=end, horizon=3
                )
                pd.testing.assert_frame_equal(
                    actual.table.reset_index(drop=True), expected.table.reset_index(drop=True), check_dtype=False
                )
                pd.testing.assert_frame_equal(actual.forecasts, expected.forecasts, check_dtype=False)
                self.assertEqual(actual.overall.metrics["predictability_score"], expected.overall.metrics["predictability_score"])
                self.assertEqual(actual.metadata["rows_used"], expected.metadata["rows_used"])
                self.assertEqual(actual.metadata["measure"], "count")

    def test_markets_from_empty_counts(self) -> None:
        self.assertTrue(assess_markets_from_counts(pd.DataFrame(columns=["period", "segment", "events"])).table.empty)

    def test_rate_from_cohorts_matches_monthly_installation_rate_exactly(self) -> None:
        for as_of in (pd.Timestamp("2026-06-15"), pd.Timestamp("2026-12-31")):
            with self.subTest(as_of=as_of):
                expected = monthly_installation_rate(self.rows, "PurchaseDate", "TankInstallDate", as_of=as_of)
                cohorts = data.fetch_purchase_install_cohorts(None, self.config, as_of=as_of.date())
                actual = rate_from_cohorts(cohorts, as_of)
                pd.testing.assert_frame_equal(
                    actual.data.reset_index(drop=True), expected.data.reset_index(drop=True), check_dtype=False
                )
                # rows_missing_install_date also counts installs after as_of (censored in the cohorts).
                for key in ("as_of", "rows_total", "rows_missing_purchase_date", "negative_lag_devices", "denominators"):
                    self.assertEqual(actual.metadata[key], expected.metadata[key], key)
                self.assertGreaterEqual(
                    actual.metadata["rows_missing_install_date"], expected.metadata["rows_missing_install_date"]
                )

    def test_rate_from_cohorts_handles_empty_and_flags_proxy(self) -> None:
        empty = rate_from_cohorts(pd.DataFrame(), "2026-01-31", is_proxy=True)
        self.assertTrue(empty.data.empty)
        self.assertTrue(empty.metadata["is_proxy"])

    def test_cohort_trend_matches_analyze_install_lag(self) -> None:
        expected = analyze_install_lag(self.rows, "PurchaseDate", "TankInstallDate").cohort_trend
        lag = data.fetch_lag_by_cohort(None, self.config, basis="purchased")
        purchased = (
            data.fetch_purchase_install_cohorts(None, self.config).dropna(subset=["purchase_month"])
            .groupby("purchase_month")["devices"].sum()
        )
        actual = cohort_trend_from_lag(lag, purchased)
        self.assertEqual(actual["cohort_month"].tolist(), expected["cohort_month"].tolist())
        for column in ("purchased_devices", "installed_devices"):
            self.assertEqual(actual[column].tolist(), expected[column].tolist(), column)
        for column in ("mean_days", "median_days", "p90_days"):
            np.testing.assert_allclose(actual[column].to_numpy(float), expected[column].to_numpy(float), equal_nan=True)
        without = cohort_trend_from_lag(lag)
        self.assertNotIn("purchased_devices", without.columns)
        self.assertTrue(cohort_trend_from_lag(pd.DataFrame()).empty)

    def test_cohort_lags_collapse_to_the_unfiltered_distribution(self) -> None:
        lag = data.fetch_lag_by_cohort(None, self.config, basis="shipped")
        collapsed = lag_distribution_from_cohorts(lag)
        direct = data.fetch_shipping_lag_distribution(None, self.config)
        self.assertEqual(collapsed["lag_days"].tolist(), direct["lag_days"].tolist())
        self.assertEqual(collapsed["devices"].tolist(), direct["devices"].tolist())
        self.assertTrue(lag_distribution_from_cohorts(pd.DataFrame()).empty)

    def test_monthly_totals_and_seasonality(self) -> None:
        counts = data.fetch_monthly_purchase_counts(None, self.config, "territory", FROM, TO)
        totals = total_by_month(counts)
        self.assertEqual(int(totals["value"].sum()), int(counts["events"].sum()))
        capped = total_by_month(counts, pd.Timestamp("2025-01-01"))
        self.assertEqual(capped["period_start"].max(), pd.Timestamp("2025-01-01"))
        seasonality = monthly_seasonality(totals.rename(columns={"value": "installs"}))
        self.assertEqual(int(seasonality["total_installs"].sum()), int(totals["value"].sum()))


if __name__ == "__main__":
    unittest.main()
