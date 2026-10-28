from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from insightquest.analytics import (
    PROXY_LABEL,
    add_customer_size,
    breakdown_by_group,
    build_activity_series,
    build_gap_breakdowns,
    compare_purchases_installs,
    customer_size_definitions,
    default_size_labels,
    rank_peaks_and_lows,
)


class ActivitySeriesTests(unittest.TestCase):
    def test_counts_rows_when_no_amount_column(self) -> None:
        data = pd.DataFrame({"d": ["2025-01-05", "2025-01-20", "2025-03-02"]})

        result = build_activity_series(data, "d", "Month")

        self.assertEqual(result.metadata["measure"], "count")
        self.assertEqual(result.data["value"].tolist(), [2, 0, 1])
        self.assertEqual(
            result.data["period_start"].tolist(),
            [pd.Timestamp("2025-01-01"), pd.Timestamp("2025-02-01"), pd.Timestamp("2025-03-01")],
        )

    def test_sums_amount_when_column_given_and_reports_missing_amounts(self) -> None:
        data = pd.DataFrame(
            {"d": ["2025-01-05", "2025-01-06", "2025-01-07"], "amt": [10.0, None, 5.5]}
        )

        result = build_activity_series(data, "d", "Month", amount_column="amt")

        self.assertEqual(result.metadata["measure"], "amount")
        self.assertEqual(result.data["value"].tolist(), [15.5])
        self.assertEqual(result.metadata["rows_missing_amount"], 1)

    def test_explicit_count_ignores_amount_column(self) -> None:
        data = pd.DataFrame({"d": ["2025-01-05"], "amt": [99.0]})

        result = build_activity_series(data, "d", "Month", amount_column="amt", measure="count")

        self.assertEqual(result.data["value"].tolist(), [1])
        self.assertIsNone(result.metadata["amount_column"])

    def test_amount_measure_requires_column_and_bad_arguments_raise(self) -> None:
        data = pd.DataFrame({"d": ["2025-01-05"]})
        with self.assertRaises(ValueError):
            build_activity_series(data, "d", "Month", measure="amount")
        with self.assertRaises(ValueError):
            build_activity_series(data, "d", "Quarter")
        with self.assertRaises(ValueError):
            build_activity_series(data, "d", "Day", "2025-02-01", "2025-01-01")
        with self.assertRaises(ValueError):
            build_activity_series(data, "d", "Day", measure="median")
        with self.assertRaises(KeyError):
            build_activity_series(data, "missing", "Day")

    def test_proxy_flag_adds_label_and_default_has_none(self) -> None:
        data = pd.DataFrame({"d": ["2025-01-05"]})

        proxy = build_activity_series(data, "d", "Day", is_proxy=True).metadata
        plain = build_activity_series(data, "d", "Day").metadata

        self.assertTrue(proxy["is_proxy"])
        self.assertEqual(proxy["date_basis_label"], PROXY_LABEL)
        self.assertFalse(plain["is_proxy"])
        self.assertNotIn("proxy", plain["date_basis_label"].lower())

    def test_empty_all_nat_and_sparse_inputs(self) -> None:
        empty = build_activity_series(pd.DataFrame(), "d", "Month")
        self.assertTrue(empty.data.empty)
        self.assertEqual(list(empty.data.columns), ["period_start", "value"])

        all_nat = build_activity_series(pd.DataFrame({"d": [None, "garbage"]}), "d", "Month")
        self.assertTrue(all_nat.data.empty)
        self.assertEqual(all_nat.metadata["rows_missing_date"], 2)

        bounded = build_activity_series(
            pd.DataFrame({"d": [None]}), "d", "Month", "2025-01-01", "2025-03-31"
        )
        self.assertEqual(bounded.data["value"].tolist(), [0, 0, 0])

    def test_week_starts_monday_and_range_bounds_are_inclusive(self) -> None:
        data = pd.DataFrame({"d": ["2024-12-31", "2025-01-01", "2025-01-06", "2025-01-12", "2025-01-13"]})

        result = build_activity_series(data, "d", "Week", "2025-01-01", "2025-01-12")

        self.assertEqual(
            result.data["period_start"].tolist(),
            [pd.Timestamp("2024-12-30"), pd.Timestamp("2025-01-06")],
        )
        self.assertEqual(result.data["value"].tolist(), [1, 2])
        self.assertEqual(result.metadata["rows_used"], 3)

    def test_day_series_fills_gaps(self) -> None:
        data = pd.DataFrame({"d": ["2025-01-01", "2025-01-03"]})
        result = build_activity_series(data, "d", "Day")
        self.assertEqual(result.data["value"].tolist(), [1, 0, 1])


class PeaksAndLowsTests(unittest.TestCase):
    def _series(self, values: list[float]) -> pd.DataFrame:
        return pd.DataFrame(
            {"period_start": pd.date_range("2025-01-01", periods=len(values), freq="MS"), "value": values}
        )

    def test_ranks_peaks_and_lows_without_overlap(self) -> None:
        result = rank_peaks_and_lows(self._series([5, 9, 1, 7, 3, 8]), n=2)

        peaks = result[result["kind"] == "peak"]
        lows = result[result["kind"] == "low"]
        self.assertEqual(peaks["value"].tolist(), [9, 8])
        self.assertEqual(peaks["rank"].tolist(), [1, 2])
        self.assertEqual(lows["value"].tolist(), [1, 3])
        self.assertEqual(result.iloc[0]["vs_mean_pct"], round((9 - 5.5) / 5.5 * 100, 2))

    def test_n_is_capped_so_sets_never_overlap(self) -> None:
        result = rank_peaks_and_lows(self._series([1, 2, 3]), n=10)
        self.assertEqual(len(result), 2)
        self.assertEqual(result["period_start"].nunique(), 2)

    def test_ties_prefer_earliest_period(self) -> None:
        result = rank_peaks_and_lows(self._series([5, 5, 1, 1]), n=1)
        self.assertEqual(result.loc[result["kind"] == "peak", "period_start"].iloc[0], pd.Timestamp("2025-01-01"))
        self.assertEqual(result.loc[result["kind"] == "low", "period_start"].iloc[0], pd.Timestamp("2025-03-01"))

    def test_flat_single_empty_and_nan_series_have_no_peaks(self) -> None:
        self.assertTrue(rank_peaks_and_lows(self._series([3, 3, 3])).empty)
        self.assertTrue(rank_peaks_and_lows(self._series([3])).empty)
        self.assertTrue(rank_peaks_and_lows(pd.DataFrame(columns=["period_start", "value"])).empty)
        self.assertTrue(rank_peaks_and_lows(self._series([np.nan, np.nan])).empty)

    def test_zero_mean_has_no_percentage_and_bad_n_raises(self) -> None:
        result = rank_peaks_and_lows(self._series([-1, 1]), n=1)
        self.assertTrue(result["vs_mean_pct"].isna().all())
        with self.assertRaises(ValueError):
            rank_peaks_and_lows(self._series([1, 2]), n=0)


class CompareTests(unittest.TestCase):
    def test_purchases_and_installs_share_one_calendar(self) -> None:
        data = pd.DataFrame(
            {
                "p": ["2025-01-10", "2025-01-20", "2025-02-05", "2025-02-06"],
                "i": ["2025-02-01", None, "2025-03-15", "invalid"],
            }
        )

        result = compare_purchases_installs(data, "p", "i", "Month", is_proxy=True)

        self.assertEqual(result.data["purchases"].tolist(), [2, 2, 0])
        self.assertEqual(result.data["installs"].tolist(), [0, 1, 1])
        self.assertEqual(result.data["net_change"].tolist(), [2, 1, -1])
        self.assertEqual(result.data["cumulative_gap"].tolist(), [2, 3, 2])
        self.assertTrue(result.metadata["is_proxy"])

    def test_empty_and_one_sided_inputs(self) -> None:
        self.assertTrue(compare_purchases_installs(pd.DataFrame(), "p", "i").data.empty)
        no_installs = pd.DataFrame({"p": ["2025-01-10", "2025-02-10"], "i": [None, None]})
        result = compare_purchases_installs(no_installs, "p", "i")
        self.assertEqual(result.data["installs"].tolist(), [0, 0])
        self.assertEqual(result.data["purchases"].tolist(), [1, 1])
        all_nat = pd.DataFrame({"p": [None], "i": [None]})
        self.assertTrue(compare_purchases_installs(all_nat, "p", "i").data.empty)


class CustomerSizeTests(unittest.TestCase):
    def _fleet(self) -> pd.DataFrame:
        rows = []
        for company, count in (("A", 9), ("B", 10), ("C", 99), ("D", 100)):
            rows += [{"company": company}] * count
        rows += [{"company": None}, {"company": "  "}, {"company": np.nan}]
        return pd.DataFrame(rows)

    def test_default_buckets_use_left_closed_edges_and_unassigned(self) -> None:
        result = add_customer_size(self._fleet(), "company")
        by_company = result.groupby(result["company"].fillna("none"))["customer_size"].first()

        self.assertEqual(by_company["A"], "Small")
        self.assertEqual(by_company["B"], "Medium")
        self.assertEqual(by_company["C"], "Medium")
        self.assertEqual(by_company["D"], "Large")
        self.assertEqual(result["customer_size"].iloc[-3:].tolist(), ["Unassigned"] * 3)
        self.assertTrue(result["company_device_count"].iloc[-3:].isna().all())
        self.assertEqual(result.loc[result["company"] == "D", "company_device_count"].iloc[0], 100)

    def test_does_not_mutate_input_and_handles_numeric_ids(self) -> None:
        data = pd.DataFrame({"company": [1, 1, 2, np.nan]})
        result = add_customer_size(data, "company", edges=(2,), labels=("Tiny", "Big"))
        self.assertNotIn("customer_size", data.columns)
        self.assertEqual(result["customer_size"].tolist(), ["Big", "Big", "Tiny", "Unassigned"])

    def test_custom_edges_default_labels_and_definitions(self) -> None:
        self.assertEqual(default_size_labels((10, 100)), ["Small", "Medium", "Large"])
        self.assertEqual(default_size_labels((5,)), ["<5", "5+"])
        self.assertEqual(default_size_labels((5, 20, 50)), ["<5", "5-19", "20-49", "50+"])
        legend = customer_size_definitions()
        self.assertEqual(legend["label"].tolist(), ["Small", "Medium", "Large", "Unassigned"])
        self.assertEqual(legend.loc[1, "description"], "10 to fewer than 100 devices")

    def test_invalid_edges_and_labels_raise(self) -> None:
        data = pd.DataFrame({"company": ["A"]})
        for bad in ((), (100, 10), (10, 10), (0, 5), (-1, 5), ("a",)):
            with self.assertRaises(ValueError, msg=str(bad)):
                add_customer_size(data, "company", bad)
        with self.assertRaises(ValueError):
            add_customer_size(data, "company", (10,), labels=("only one",))
        with self.assertRaises(KeyError):
            add_customer_size(data, "nope")

    def test_empty_frame_gets_columns(self) -> None:
        result = add_customer_size(pd.DataFrame({"company": []}), "company")
        self.assertIn("customer_size", result.columns)
        self.assertTrue(result.empty)


class BreakdownTests(unittest.TestCase):
    def _devices(self) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "id": [1, 2, 3, 4, 5, 6],
                "territory": ["N", "N", "N", "S", "S", None],
                "company": ["A", "A", "B", "C", "C", None],
                "installed": ["2025-01-10", None, "2025-02-10", "2025-03-01", "bad", None],
            }
        )

    def test_rates_gaps_and_distinct_companies(self) -> None:
        result = breakdown_by_group(
            self._devices(), "territory", "installed", company_column="company",
            device_column="id", small_group_threshold=3,
        )
        table = result.data.set_index("group")

        self.assertEqual(table.loc["N", "devices"], 3)
        self.assertEqual(table.loc["N", "installed_devices"], 2)
        self.assertAlmostEqual(table.loc["N", "installed_rate_pct"], 66.67)
        self.assertEqual(table.loc["N", "companies"], 2)
        self.assertEqual(table.loc["S", "companies"], 1)
        self.assertEqual(table.loc["S", "installed_devices"], 1)
        self.assertEqual(table.loc["Unknown", "companies"], 0)
        self.assertEqual(table.loc["Unknown", "installed_rate_pct"], 0)
        self.assertEqual(result.metadata["total_companies"], 3)
        self.assertEqual(result.metadata["overall_devices"], 6)
        self.assertAlmostEqual(result.metadata["overall_installed_rate_pct"], 50.0)
        self.assertAlmostEqual(table.loc["N", "gap_vs_overall_pp"], 16.67)
        self.assertAlmostEqual(table["share_of_devices_pct"].sum(), 100.0, places=1)
        self.assertEqual(result.data["group"].iloc[0], "N")

    def test_small_groups_are_flagged_not_suppressed(self) -> None:
        result = breakdown_by_group(self._devices(), "territory", "installed", small_group_threshold=3)
        flags = result.data.set_index("group")["small_group"]
        self.assertEqual(len(result.data), 3)
        self.assertFalse(flags["N"])
        self.assertTrue(flags["S"])
        self.assertTrue(flags["Unknown"])
        self.assertTrue(result.data["companies"].isna().all())

    def test_company_value_is_never_summed_across_devices(self) -> None:
        data = pd.DataFrame(
            {"territory": ["N"] * 4, "company": ["A"] * 4, "installed": [None] * 4}
        )
        result = breakdown_by_group(data, "territory", "installed", company_column="company")
        self.assertEqual(result.data["companies"].iloc[0], 1)

    def test_installs_in_period_window_and_duplicates(self) -> None:
        data = self._devices()
        data = pd.concat([data, data.iloc[[0]]], ignore_index=True)

        result = breakdown_by_group(
            data, "territory", "installed", device_column="id",
            from_date="2025-02-01", to_date="2025-02-10",
        )
        table = result.data.set_index("group")

        self.assertEqual(table["devices"].sum(), 6)
        self.assertEqual(table.loc["N", "installs_in_period"], 1)
        self.assertEqual(table.loc["N", "installed_devices"], 2)

    def test_empty_and_no_installs(self) -> None:
        empty = breakdown_by_group(pd.DataFrame(columns=["t", "i"]), "t", "i")
        self.assertTrue(empty.data.empty)
        self.assertIsNone(empty.metadata["overall_installed_rate_pct"])

        none = breakdown_by_group(pd.DataFrame({"t": ["a"], "i": [None]}), "t", "i")
        self.assertEqual(none.data["installed_rate_pct"].iloc[0], 0)
        self.assertTrue(none.data["share_of_installs_pct"].isna().all())

    def test_gap_breakdowns_cover_three_dimensions(self) -> None:
        data = self._devices().assign(product=["X", "X", "Y", "Y", "Y", "Y"])

        results = build_gap_breakdowns(
            data, "installed", territory_column="territory", product_column="product",
            company_column="company", device_column="id",
        )

        self.assertEqual(set(results), {"territory", "customer_size", "product_type"})
        self.assertIn("Unassigned", results["customer_size"].data["group"].tolist())
        self.assertEqual(len(results["customer_size"].metadata["size_definitions"]), 4)
        only_territory = build_gap_breakdowns(data, "installed", territory_column="territory")
        self.assertEqual(set(only_territory), {"territory"})


if __name__ == "__main__":
    unittest.main()
