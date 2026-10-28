from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from insightquest.config import get_config
from insightquest.data import (
    bin_lag_distribution,
    fetch_customer_size_distribution,
    fetch_event_counts,
    fetch_inventory_summary,
    fetch_lag_by_cohort,
    fetch_monthly_purchase_counts,
    fetch_purchase_install_cohorts,
    fetch_segment_counts,
    fetch_shipping_lag_distribution,
    lag_percentiles,
    pivot_monthly_counts,
    summarize_lag_by_cohort,
)

CSV = Path(__file__).resolve().parents[1] / "demo_data.csv"


class DemoParityTests(unittest.TestCase):
    """Demo aggregates must equal an independent plain-pandas computation."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.raw = pd.read_csv(CSV)
        cls.raw["purchase"] = pd.to_datetime(cls.raw["PurchaseDate"])
        cls.raw["install"] = pd.to_datetime(cls.raw["TankInstallDate"])

    def setUp(self) -> None:
        self.config = replace(
            get_config(), demo_mode=True, demo_csv_path=CSV,
            col_installed_at="TankInstallDate", col_sales_date="PurchaseDate",
            col_shipped_at="PurchaseDate", col_territory="Territory",
            col_customer="CompanyId", col_product_type="DeviceType",
            customer_size_edges=(10, 100),
        )

    def test_monthly_purchases_by_territory_and_product_type(self) -> None:
        for group_by, column in (("territory", "Territory"), ("product_type", "DeviceType")):
            with self.subTest(group_by=group_by):
                expected = (
                    self.raw.assign(period=self.raw["purchase"].dt.to_period("M").dt.to_timestamp())
                    .groupby(["period", column]).size().rename("events").reset_index()
                    .rename(columns={column: "segment"})
                    .sort_values(["period", "segment"]).reset_index(drop=True)
                )
                actual = fetch_monthly_purchase_counts(None, self.config, group_by)
                self.assertEqual(list(actual.columns), ["period", "segment", "events"])
                pd.testing.assert_frame_equal(actual, expected, check_dtype=False)
                self.assertEqual(int(actual["events"].sum()), len(self.raw))

    def test_monthly_range_filter_is_inclusive_of_end_date(self) -> None:
        start, end = date(2024, 3, 1), date(2024, 5, 31)
        mask = (self.raw["purchase"] >= "2024-03-01") & (self.raw["purchase"] < "2024-06-01")
        expected = self.raw[mask].groupby(self.raw[mask]["purchase"].dt.to_period("M").dt.to_timestamp()).size()
        actual = fetch_monthly_purchase_counts(None, self.config, "territory", start, end)
        self.assertEqual(int(actual["events"].sum()), int(expected.sum()))
        self.assertTrue(actual["period"].between("2024-03-01", "2024-05-01").all())

    def test_installs_by_week_and_day_without_dimension(self) -> None:
        week = self.raw["install"].dt.normalize() - pd.to_timedelta(self.raw["install"].dt.dayofweek, unit="D")
        expected_week = week.value_counts().sort_index()
        actual = fetch_event_counts(None, self.config, "installs", "week")
        self.assertEqual(list(actual.columns), ["period", "events"])
        self.assertEqual(list(actual["events"]), list(expected_week.values))
        self.assertEqual(list(actual["period"]), list(expected_week.index))
        self.assertTrue((actual["period"].dt.dayofweek == 0).all())
        expected_day = self.raw["install"].dt.normalize().value_counts().sort_index()
        actual_day = fetch_event_counts(None, self.config, "installs", "day")
        self.assertEqual(list(actual_day["events"]), list(expected_day.values))

    def test_segment_counts_by_every_dimension(self) -> None:
        start, end = date(2024, 1, 1), date(2024, 12, 31)
        in_purchase = self.raw["purchase"].between("2024-01-01", "2024-12-31 23:59:59")
        in_install = self.raw["install"].between("2024-01-01", "2024-12-31 23:59:59")
        for dimension, column in (("territory", "Territory"), ("product_type", "DeviceType"), ("customer", "CompanyId")):
            with self.subTest(dimension=dimension):
                grouped = self.raw.assign(p=in_purchase, i=in_install, n=self.raw["install"].isna()).groupby(column)
                expected = pd.DataFrame({
                    "segment": grouped.size().index, "devices": grouped.size().values,
                    "purchased": grouped["p"].sum().values, "installed": grouped["i"].sum().values,
                    "not_installed": grouped["n"].sum().values,
                }).sort_values(["devices", "segment"], ascending=[False, True]).reset_index(drop=True)
                actual = fetch_segment_counts(None, self.config, dimension, start, end)
                pd.testing.assert_frame_equal(actual, expected, check_dtype=False)
        top = fetch_segment_counts(None, self.config, "territory", top_n=2)
        self.assertEqual(len(top), 2)
        self.assertEqual(list(top["segment"]), ["Quebec", "Ontario"])

    def test_customer_size_buckets_use_company_device_counts(self) -> None:
        per_company = self.raw.groupby("CompanyId").size()
        edges = (31, 35)
        config = replace(self.config, customer_size_edges=edges)
        names = ["Small", "Medium", "Large"]

        def bucket(size: int) -> str:
            return names[0] if size < 31 else names[1] if size < 35 else names[2]

        sizes = self.raw["CompanyId"].map(per_company).map(bucket)
        distribution = fetch_customer_size_distribution(None, config)
        expected_devices = sizes.value_counts()
        expected_companies = per_company.map(bucket).value_counts()
        for row in distribution.itertuples():
            self.assertEqual(row.devices, expected_devices[row.segment])
            self.assertEqual(row.companies, expected_companies[row.segment])
        self.assertEqual(int(distribution["devices"].sum()), len(self.raw))
        self.assertEqual(list(distribution["segment"]), [n for n in names if n in set(sizes)])

        by_month = fetch_event_counts(None, config, "installs", "month", group_by="customer_size")
        reference = (
            pd.DataFrame({
                "period": self.raw["install"].dt.to_period("M").dt.to_timestamp(),
                "segment": sizes,
            }).groupby(["period", "segment"]).size()
        )
        self.assertEqual(int(by_month["events"].sum()), int(reference.sum()))
        for row in by_month.itertuples():
            self.assertEqual(row.events, reference[(row.period, row.segment)])

    def test_segment_counts_without_purchase_column_keep_other_columns_exact(self) -> None:
        import tempfile

        full = fetch_segment_counts(None, self.config, "territory")
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            path = Path(folder) / "no_purchase.csv"
            pd.read_csv(CSV).drop(columns=["PurchaseDate"]).to_csv(path, index=False)
            for override in ({}, {"col_sales_date": ""}):
                with self.subTest(override=override):
                    config = replace(self.config, demo_csv_path=path, **override)
                    result = fetch_segment_counts(None, config, "territory", date(2024, 1, 1), date(2024, 12, 31))
                    self.assertIs(result.attrs["purchased_available"], False)
                    self.assertTrue((result["purchased"] == 0).all())
                    ranged = fetch_segment_counts(None, self.config, "territory", date(2024, 1, 1), date(2024, 12, 31))
                    pd.testing.assert_frame_equal(
                        result.drop(columns="purchased"), ranged.drop(columns="purchased"),
                    )
                    sized = fetch_segment_counts(None, config, "customer_size")
                    self.assertEqual(int(sized["devices"].sum()), len(self.raw))
            distribution = fetch_customer_size_distribution(
                None, replace(self.config, demo_csv_path=path, col_installed_at="Nope"),
            )
            pd.testing.assert_frame_equal(distribution, fetch_customer_size_distribution(None, self.config))
        self.assertIs(full.attrs["purchased_available"], True)
        self.assertGreater(int(full["purchased"].sum()), 0)

    def test_default_edges_yield_small_medium_large(self) -> None:
        per_company = self.raw.groupby("CompanyId").size()
        distribution = fetch_customer_size_distribution(None, self.config)
        self.assertEqual(list(distribution["segment"]), ["Small", "Medium", "Large"])
        expected_companies = {
            "Small": int((per_company < 10).sum()),
            "Medium": int(((per_company >= 10) & (per_company < 100)).sum()),
            "Large": int((per_company >= 100).sum()),
        }
        expected_devices = {
            "Small": int(per_company[per_company < 10].sum()),
            "Medium": int(per_company[(per_company >= 10) & (per_company < 100)].sum()),
            "Large": int(per_company[per_company >= 100].sum()),
        }
        self.assertEqual(dict(zip(distribution["segment"], distribution["companies"])), expected_companies)
        self.assertEqual(dict(zip(distribution["segment"], distribution["devices"])), expected_devices)
        self.assertEqual(int(distribution["devices"].sum()), len(self.raw))

    def test_shipping_lag_distribution_and_percentiles(self) -> None:
        lag = (self.raw["install"] - self.raw["purchase"]).dt.days.dropna()
        distribution = fetch_shipping_lag_distribution(None, self.config)
        expected = lag.value_counts().sort_index()
        self.assertEqual(list(distribution["lag_days"]), list(expected.index))
        self.assertEqual(list(distribution["devices"]), list(expected.values))
        quantiles = (0.0, 0.25, 0.5, 0.9, 1.0)
        result = lag_percentiles(distribution, quantiles)
        for quantile in quantiles:
            self.assertAlmostEqual(result[quantile], float(np.quantile(lag, quantile)))
        binned = bin_lag_distribution(distribution, 7)
        self.assertEqual(int(binned["devices"].sum()), len(lag))
        self.assertTrue(((binned["bin_end"] - binned["bin_start"]) == 7).all())

    def test_negative_lags_are_kept_in_the_distribution(self) -> None:
        lag = (self.raw["install"] - self.raw["purchase"]).dt.days.dropna()
        distribution = fetch_shipping_lag_distribution(None, self.config)
        negatives = int((lag < 0).sum())
        self.assertGreater(negatives, 0)
        self.assertEqual(int(distribution.loc[distribution["lag_days"] < 0, "devices"].sum()), negatives)
        self.assertEqual(int(distribution["devices"].sum()), len(lag))

    def test_inventory_summary_counts_null_install_dates_and_trailing_installs(self) -> None:
        as_of = date(2026, 6, 30)
        raw = self.raw.copy()
        result = fetch_inventory_summary(None, self.config, as_of, (30, 90))
        for row in result.itertuples():
            rows = raw[raw["DeviceType"] == row.product_type]
            self.assertEqual(row.total_devices, len(rows))
            self.assertEqual(row.on_hand, int(rows["install"].isna().sum()))
            for days, value in ((30, row.installs_30d), (90, row.installs_90d)):
                start = pd.Timestamp(as_of - timedelta(days=days - 1))
                expected = ((rows["install"] >= start) & (rows["install"] < pd.Timestamp(as_of + timedelta(days=1)))).sum()
                self.assertEqual(value, expected)

    def test_null_install_dates_and_blank_labels_are_handled_like_sql(self) -> None:
        import tempfile

        frame = pd.DataFrame({
            "Id": [1, 2, 3, 4], "CompanyId": ["A", " ", None, "A"],
            "DeviceType": ["X", "X", "", "Y"], "Territory": ["N", None, "N", " S "],
            "PurchaseDate": ["2024-01-05", "2024-01-20", "2024-02-01", None],
            "TankInstallDate": ["2024-01-10", None, None, "2024-02-02"],
        })
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            path = Path(folder) / "mini.csv"
            frame.to_csv(path, index=False)
            config = replace(self.config, demo_csv_path=path, customer_size_edges=(2,))
            counts = fetch_monthly_purchase_counts(None, config, "territory")
            self.assertEqual(
                [(p.strftime("%Y-%m"), s, e) for p, s, e in counts.itertuples(index=False)],
                [("2024-01", "N", 1), ("2024-01", "Unknown Territory", 1), ("2024-02", "N", 1)],
            )
            inventory = fetch_inventory_summary(None, config, date(2024, 2, 28), (30,))
            self.assertEqual(
                inventory.to_dict("records"),
                [
                    {"product_type": "Unknown Product", "total_devices": 1, "on_hand": 1, "installs_30d": 0},
                    {"product_type": "X", "total_devices": 2, "on_hand": 1, "installs_30d": 0},
                    {"product_type": "Y", "total_devices": 1, "on_hand": 0, "installs_30d": 1},
                ],
            )
            sizes = fetch_customer_size_distribution(None, config)
            self.assertEqual(
                sizes.to_dict("records"),
                [
                    {"segment": "Large", "companies": 1, "devices": 2},
                    {"segment": "Unknown", "companies": 0, "devices": 2},
                ],
            )

    def test_purchase_install_cohorts_match_plain_pandas(self) -> None:
        raw = self.raw
        for group_by, column in ((None, None), ("territory", "Territory"), ("product_type", "DeviceType")):
            with self.subTest(group_by=group_by):
                frame = pd.DataFrame({
                    "purchase_month": raw["purchase"].dt.to_period("M").dt.to_timestamp(),
                    "install_month": raw["install"].dt.to_period("M").dt.to_timestamp(),
                    "neg": (raw["install"] < raw["purchase"]).astype(int),
                })
                keys = ["purchase_month", "install_month"]
                if column:
                    frame["segment"] = raw[column]
                    keys.append("segment")
                expected = (
                    frame.groupby(keys, dropna=False).agg(devices=("neg", "size"), negative_lag_devices=("neg", "sum"))
                    .reset_index().sort_values(keys, na_position="last").reset_index(drop=True)
                )
                actual = fetch_purchase_install_cohorts(None, self.config, group_by=group_by)
                pd.testing.assert_frame_equal(actual, expected, check_dtype=False)
                self.assertEqual(int(actual["devices"].sum()), len(raw))
        not_installed = fetch_purchase_install_cohorts(None, self.config)
        self.assertEqual(int(not_installed.loc[not_installed["install_month"].isna(), "devices"].sum()), int(raw["install"].isna().sum()))
        self.assertEqual(int(not_installed["negative_lag_devices"].sum()), int((raw["install"] < raw["purchase"]).sum()))

    def test_cohort_bounds_filter_purchase_date_and_as_of_censors_installs(self) -> None:
        start, end, as_of = date(2024, 3, 10), date(2024, 5, 20), date(2024, 4, 15)
        subset = self.raw[self.raw["purchase"].between("2024-03-10", "2024-05-20 23:59:59")]
        actual = fetch_purchase_install_cohorts(None, self.config, start, end)
        self.assertEqual(int(actual["devices"].sum()), len(subset))
        censored = fetch_purchase_install_cohorts(None, self.config, start, end, as_of=as_of)
        late = subset["install"] >= pd.Timestamp("2024-04-16")
        self.assertEqual(int(censored.loc[censored["install_month"].isna(), "devices"].sum()), int(late.sum() + subset["install"].isna().sum()))
        self.assertEqual(int(censored["devices"].sum()), len(subset))
        self.assertEqual(fetch_purchase_install_cohorts(None, self.config, as_of=pd.Timestamp(as_of)).shape,
                         fetch_purchase_install_cohorts(None, self.config, as_of=as_of).shape)

    def test_cohorts_by_customer_size_use_company_device_counts(self) -> None:
        per_company = self.raw.groupby("CompanyId").size()
        size = self.raw["CompanyId"].map(per_company).map(lambda n: "Small" if n < 10 else "Medium" if n < 100 else "Large")
        actual = fetch_purchase_install_cohorts(None, self.config, group_by="customer_size")
        expected = size.value_counts()
        self.assertEqual({k: int(v) for k, v in actual.groupby("segment")["devices"].sum().items()}, expected.to_dict())

    def test_lag_by_cohort_matches_plain_pandas_and_keeps_negatives_separate(self) -> None:
        for basis, column in (("purchased", "purchase"), ("shipped", "purchase")):
            lag = (self.raw["install"] - self.raw[column]).dt.days
            valid = lag.notna()
            expected = (
                pd.DataFrame({
                    "cohort_month": self.raw.loc[valid, column].dt.to_period("M").dt.to_timestamp(),
                    "lag_days": lag[valid],
                }).groupby(["cohort_month", "lag_days"]).size().rename("devices").reset_index()
            )
            actual = fetch_lag_by_cohort(None, self.config, basis=basis)
            pd.testing.assert_frame_equal(actual, expected, check_dtype=False)
        summary = summarize_lag_by_cohort(actual)
        self.assertEqual(int(summary["negative_lag_devices"].sum()), int((lag < 0).sum()))
        self.assertEqual(int(summary["devices"].sum()), int((lag >= 0).sum()))
        bounded = fetch_lag_by_cohort(None, self.config, date(2024, 1, 1), date(2024, 3, 31))
        self.assertTrue(bounded["cohort_month"].between("2024-01-01", "2024-03-01").all())
        with self.assertRaises(ValueError):
            fetch_lag_by_cohort(None, self.config, basis="ordered")

    def test_cohort_edge_cases_null_dates_and_empty_ranges(self) -> None:
        import tempfile

        frame = pd.DataFrame({
            "Id": [1, 2, 3, 4], "CompanyId": ["A"] * 4, "DeviceType": ["X"] * 4, "Territory": ["N"] * 4,
            "PurchaseDate": ["2024-01-20", "2024-01-05", None, None],
            "TankInstallDate": ["2024-01-10", "2024-02-01", "2024-03-01", None],
        })
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            path = Path(folder) / "mini.csv"
            frame.to_csv(path, index=False)
            config = replace(self.config, demo_csv_path=path)
            cohorts = fetch_purchase_install_cohorts(None, config)
            self.assertEqual(
                [(None if pd.isna(p) else p.strftime("%Y-%m"), None if pd.isna(i) else i.strftime("%Y-%m"), d, n)
                 for p, i, d, n in cohorts.itertuples(index=False)],
                [("2024-01", "2024-01", 1, 1), ("2024-01", "2024-02", 1, 0), (None, "2024-03", 1, 0), (None, None, 1, 0)],
            )
            lag = fetch_lag_by_cohort(None, config, basis="purchased")
            self.assertEqual([(m.strftime("%Y-%m"), l, d) for m, l, d in lag.itertuples(index=False)],
                             [("2024-01", -10, 1), ("2024-01", 27, 1)])
            bounded = fetch_purchase_install_cohorts(None, config, date(2024, 1, 1), date(2024, 1, 31))
            self.assertEqual(int(bounded["devices"].sum()), 2)
        empty = fetch_purchase_install_cohorts(None, self.config, date(1999, 1, 1), date(1999, 2, 1), "territory")
        self.assertTrue(empty.empty)
        self.assertEqual(list(empty.columns), ["purchase_month", "install_month", "segment", "devices", "negative_lag_devices"])
        self.assertTrue(fetch_lag_by_cohort(None, self.config, date(1999, 1, 1), date(1999, 2, 1)).empty)
        self.assertTrue(summarize_lag_by_cohort(fetch_lag_by_cohort(None, self.config, date(1999, 1, 1), date(1999, 2, 1))).empty)

    def test_pivot_fills_missing_months_with_zero(self) -> None:
        counts = pd.DataFrame({
            "period": pd.to_datetime(["2024-01-01", "2024-03-01", "2024-03-01"]),
            "segment": ["A", "A", "B"], "events": [2, 1, 4],
        })
        wide = pivot_monthly_counts(counts)
        self.assertEqual(list(wide.index.strftime("%Y-%m")), ["2024-01", "2024-02", "2024-03"])
        self.assertEqual(wide["A"].tolist(), [2, 0, 1])
        self.assertEqual(wide["B"].tolist(), [0, 0, 4])
        self.assertEqual(len(pivot_monthly_counts(counts, fill_missing=False)), 2)
        self.assertTrue(pivot_monthly_counts(counts.iloc[0:0]).empty)

    def test_empty_ranges_return_typed_empty_frames(self) -> None:
        empty = fetch_event_counts(
            None, self.config, "purchases", "month", date(1999, 1, 1), date(1999, 12, 31), "territory",
        )
        self.assertEqual(list(empty.columns), ["period", "segment", "events"])
        self.assertTrue(empty.empty)
        self.assertTrue(fetch_shipping_lag_distribution(None, self.config, date(1999, 1, 1), date(1999, 2, 1)).empty)

    def test_demo_missing_column_reports_clear_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "was not found in 'demo_data.csv'"):
            fetch_monthly_purchase_counts(None, replace(self.config, col_territory="Nope"), "territory")


if __name__ == "__main__":
    unittest.main()
