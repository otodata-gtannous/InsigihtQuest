from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from insightquest.analytics import (
    PROXY_LABEL,
    analyze_install_lag,
    lag_histogram,
    monthly_installation_rate,
    summarize_lags,
)


class LagTests(unittest.TestCase):
    def _data(self) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "p": ["2025-01-01", "2025-01-05", "2025-01-10", None, "2025-02-01", "2025-02-02", "bad"],
                "i": ["2025-01-01", "2025-01-15", "2025-01-05", "2025-01-09", None, "2025-03-03", "2025-03-01"],
            }
        )

    def test_excluded_counts_add_up_and_zero_lag_is_valid(self) -> None:
        result = analyze_install_lag(self._data(), "p", "i", is_proxy=True)

        self.assertEqual(
            result.excluded,
            {
                "total_rows": 7, "valid": 3, "missing_purchase_date": 2,
                "missing_install_date": 1, "missing_any_date": 3, "negative_lag": 1,
                "invalid_date_values": 1,
            },
        )
        self.assertEqual(
            result.excluded["valid"] + result.excluded["missing_any_date"] + result.excluded["negative_lag"],
            result.excluded["total_rows"],
        )
        self.assertEqual(sorted(result.lags["lag_days"].tolist()), [0.0, 10.0, 29.0])

    def test_summary_statistics_and_proxy_label(self) -> None:
        result = analyze_install_lag(self._data(), "p", "i", is_proxy=True)

        self.assertEqual(result.summary["count"], 3)
        self.assertEqual(result.summary["median_days"], 10.0)
        self.assertEqual(result.summary["p50_days"], 10.0)
        self.assertAlmostEqual(result.summary["mean_days"], 13.0)
        self.assertAlmostEqual(result.summary["p90_days"], 25.2)
        self.assertEqual(result.summary["min_days"], 0.0)
        self.assertEqual(result.summary["max_days"], 29.0)
        self.assertTrue(result.metadata["is_proxy"])
        self.assertEqual(result.metadata["date_basis_label"], PROXY_LABEL)
        self.assertIn("censoring_note", result.metadata)

    def test_histogram_boundaries_are_left_closed(self) -> None:
        lags = pd.Series([0, 6.9, 7, 13, 14, 364, 365, 1000])

        hist = lag_histogram(lags)
        counts = hist.set_index("bin_label")["count"]

        self.assertEqual(hist["bin_label"].tolist()[:3], ["0-6", "7-13", "14-29"])
        self.assertEqual(hist["bin_label"].iloc[-1], "365+")
        self.assertEqual(counts["0-6"], 2)
        self.assertEqual(counts["7-13"], 2)
        self.assertEqual(counts["14-29"], 1)
        self.assertEqual(counts["180-364"], 1)
        self.assertEqual(counts["365+"], 2)
        self.assertEqual(hist["count"].sum(), 8)
        self.assertAlmostEqual(hist["share_pct"].sum(), 100.0, places=1)
        self.assertTrue(pd.isna(hist["upper_days"].iloc[-1]))

    def test_histogram_custom_edges_empty_and_invalid(self) -> None:
        hist = lag_histogram(pd.Series(dtype=float), bin_edges=(0, 10))
        self.assertEqual(hist["count"].tolist(), [0, 0])
        self.assertEqual(hist["share_pct"].tolist(), [0.0, 0.0])
        with self.assertRaises(ValueError):
            lag_histogram(pd.Series([1]), bin_edges=(10, 5))
        with self.assertRaises(ValueError):
            lag_histogram(pd.Series([1]), bin_edges=())

    def test_cohort_trend_by_purchase_month(self) -> None:
        result = analyze_install_lag(self._data(), "p", "i")
        trend = result.cohort_trend.set_index("cohort_month")

        jan, feb = pd.Timestamp("2025-01-01"), pd.Timestamp("2025-02-01")
        self.assertEqual(trend.loc[jan, "purchased_devices"], 3)
        self.assertEqual(trend.loc[jan, "installed_devices"], 2)
        self.assertEqual(trend.loc[jan, "median_days"], 5.0)
        self.assertEqual(trend.loc[feb, "purchased_devices"], 2)
        self.assertEqual(trend.loc[feb, "installed_devices"], 1)
        self.assertEqual(trend.loc[feb, "p90_days"], 29.0)

    def test_cohort_with_no_valid_installs_has_nan_stats(self) -> None:
        data = pd.DataFrame({"p": ["2025-01-01"], "i": [None]})
        result = analyze_install_lag(data, "p", "i")
        self.assertEqual(result.cohort_trend["installed_devices"].tolist(), [0])
        self.assertTrue(result.cohort_trend["median_days"].isna().all())
        self.assertIsNone(result.summary["median_days"])
        self.assertEqual(result.summary["count"], 0)

    def test_empty_and_all_negative_inputs(self) -> None:
        empty = analyze_install_lag(pd.DataFrame(columns=["p", "i"]), "p", "i")
        self.assertTrue(empty.lags.empty)
        self.assertEqual(empty.excluded["total_rows"], 0)
        self.assertTrue(empty.cohort_trend.empty)

        negative = analyze_install_lag(pd.DataFrame({"p": ["2025-02-01"], "i": ["2025-01-01"]}), "p", "i")
        self.assertEqual(negative.excluded["negative_lag"], 1)
        self.assertTrue(negative.lags.empty)
        self.assertIsNone(negative.summary["p90_days"])

    def test_summarize_ignores_nan(self) -> None:
        self.assertEqual(summarize_lags(pd.Series([np.nan, 4.0]))["median_days"], 4.0)

    def test_missing_column_raises(self) -> None:
        with self.assertRaises(KeyError):
            analyze_install_lag(pd.DataFrame({"p": ["2025-01-01"]}), "p", "i")


class MonthlyRateTests(unittest.TestCase):
    def _data(self) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "p": ["2025-01-05", "2025-01-10", "2025-01-20", "2025-03-02", "2025-03-03"],
                "i": ["2025-01-31", "2025-02-15", None, "2025-03-10", "2025-03-01"],
            }
        )

    def test_cohort_rate_and_rate_vs_population(self) -> None:
        result = monthly_installation_rate(self._data(), "p", "i", is_proxy=True)
        table = result.data.set_index("month")
        jan, feb, mar = (pd.Timestamp(f"2025-0{m}-01") for m in (1, 2, 3))

        self.assertEqual(table.loc[jan, "purchased"], 3)
        self.assertEqual(table.loc[jan, "installed_by_month_end"], 1)
        self.assertAlmostEqual(table.loc[jan, "cohort_rate_pct"], 33.33)
        self.assertEqual(table.loc[jan, "installed_cohort_to_date"], 2)
        self.assertAlmostEqual(table.loc[jan, "cohort_rate_to_date_pct"], 66.67)
        self.assertEqual(table.loc[mar, "purchased"], 2)
        self.assertEqual(table.loc[mar, "installed_by_month_end"], 1)
        self.assertEqual(table.loc[mar, "negative_lag_devices"], 1)
        self.assertEqual(table.loc[feb, "installs_in_month"], 1)
        self.assertEqual(table.loc[feb, "purchased_to_date"], 3)
        self.assertAlmostEqual(table.loc[feb, "rate_vs_population_pct"], 33.33)
        self.assertEqual(table.loc[mar, "purchased_to_date"], 5)
        self.assertEqual(table.loc[mar, "installs_in_month"], 2)
        self.assertEqual(table.loc[mar, "installed_to_date"], 4)
        self.assertEqual(result.metadata["negative_lag_devices"], 1)
        self.assertTrue(result.metadata["is_proxy"])
        self.assertIn("cohort_rate_pct", result.metadata["denominators"])
        self.assertIn("rate_vs_population_pct", result.metadata["denominators"])

    def test_zero_denominators_give_nan_not_errors(self) -> None:
        result = monthly_installation_rate(self._data(), "p", "i")
        feb = result.data.set_index("month").loc[pd.Timestamp("2025-02-01")]
        self.assertEqual(feb["purchased"], 0)
        self.assertTrue(np.isnan(feb["cohort_rate_pct"]))
        self.assertFalse(np.isinf(result.data[["cohort_rate_pct", "rate_vs_population_pct"]].to_numpy(dtype=float)).any())

        installs_first = pd.DataFrame({"p": ["2025-02-10"], "i": ["2025-01-05"]})
        early = monthly_installation_rate(installs_first, "p", "i").data.set_index("month")
        self.assertTrue(np.isnan(early.loc[pd.Timestamp("2025-01-01"), "rate_vs_population_pct"]))

    def test_partial_last_month_and_as_of_cut(self) -> None:
        result = monthly_installation_rate(self._data(), "p", "i", as_of="2025-03-05")
        table = result.data.set_index("month")
        mar = table.loc[pd.Timestamp("2025-03-01")]

        self.assertTrue(mar["is_partial_month"])
        self.assertFalse(table.loc[pd.Timestamp("2025-01-01"), "is_partial_month"])
        self.assertEqual(mar["installed_by_month_end"], 0)
        self.assertEqual(mar["installs_in_month"], 1)
        self.assertEqual(result.metadata["as_of"], pd.Timestamp("2025-03-05"))

    def test_missing_dates_empty_and_as_of_before_data(self) -> None:
        sparse = pd.DataFrame({"p": [None, "2025-01-05"], "i": ["2025-01-20", None]})
        result = monthly_installation_rate(sparse, "p", "i")
        self.assertEqual(result.metadata["rows_missing_purchase_date"], 1)
        self.assertEqual(result.metadata["rows_missing_install_date"], 1)
        self.assertEqual(result.data["installs_in_month"].tolist(), [1])

        self.assertTrue(monthly_installation_rate(pd.DataFrame(columns=["p", "i"]), "p", "i").data.empty)
        self.assertTrue(monthly_installation_rate(pd.DataFrame({"p": [None], "i": [None]}), "p", "i").data.empty)
        self.assertTrue(monthly_installation_rate(self._data(), "p", "i", as_of="2024-01-01").data.empty)


if __name__ == "__main__":
    unittest.main()
