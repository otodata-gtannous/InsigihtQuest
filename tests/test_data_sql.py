from __future__ import annotations

import re
import unittest
from dataclasses import replace
from datetime import date
from unittest.mock import MagicMock, patch

import pandas as pd
from sqlalchemy import text
from sqlalchemy.exc import NoSuchTableError

from insightquest import data
from insightquest.config import get_config
from insightquest.data import (
    _quote_name,
    build_customer_size_distribution_query,
    build_event_counts_query,
    build_inventory_summary_query,
    build_lag_by_cohort_query,
    build_purchase_install_cohorts_query,
    build_segment_counts_query,
    build_shipping_lag_query,
    fetch_event_counts,
    fetch_flat_table_columns,
    fetch_customer_size_distribution,
    fetch_segment_counts,
    fetch_inventory_summary,
    fetch_lag_by_cohort,
    fetch_monthly_purchase_counts,
    fetch_purchase_install_cohorts,
    summarize_lag_by_cohort,
)

COLUMNS = {
    "Id", "CompanyId", "DeviceType", "PurchaseDate", "TankInstallDate", "Territory",
}


def db_config(**overrides):
    values = dict(
        demo_mode=False, source_mode="flat-table", source_table="dbo.Insight Project",
        col_installed_at="TankInstallDate", col_sales_date="PurchaseDate",
        col_shipped_at="PurchaseDate", col_territory="Territory",
        col_customer="CompanyId", col_product_type="DeviceType",
        customer_size_edges=(10, 100),
    )
    values.update(overrides)
    return replace(get_config(), **values)


def squash(sql: str) -> str:
    return re.sub(r"\s+", " ", sql)


class IdentifierSafetyTests(unittest.TestCase):
    def test_quote_name_rejects_bracket_and_control_characters(self) -> None:
        for bad in ["a]b", "a]; DROP TABLE x;--", "a\nb", "a[b", "a-b", "", "  ", "a'b", "a\x00"]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                _quote_name(bad)
        self.assertEqual(_quote_name(" Tank Install_Date1 "), "[Tank Install_Date1]")

    def test_builders_reject_unsafe_column_mappings(self) -> None:
        config = db_config(col_territory="Territory]; DROP TABLE x;--")
        with self.assertRaises(ValueError):
            build_event_counts_query(config, "purchases", "month", "territory")

    def test_invalid_choices_are_rejected_before_sql_is_built(self) -> None:
        config = db_config()
        with self.assertRaises(ValueError):
            build_event_counts_query(config, "refunds")
        with self.assertRaises(ValueError):
            build_event_counts_query(config, "installs", "year")
        with self.assertRaises(ValueError):
            build_event_counts_query(config, "installs", "month", "color")
        with self.assertRaises(ValueError):
            build_event_counts_query(config, "installs", from_date=date(2024, 1, 1))


class EventCountSqlTests(unittest.TestCase):
    def test_monthly_purchases_by_territory_is_aggregated_in_sql(self) -> None:
        sql, params = build_event_counts_query(
            db_config(), "purchases", "month", "territory", date(2024, 1, 1), date(2024, 12, 31),
        )
        flat = squash(sql)
        self.assertIn("FROM [dbo].[Insight Project]", flat)
        self.assertIn("[PurchaseDate] AS event_at", flat)
        self.assertIn("COALESCE(NULLIF(LTRIM(RTRIM(CAST([Territory] AS nvarchar(255)))), ''), :unknown_label) AS segment", flat)
        self.assertIn("DATEFROMPARTS(YEAR(event_at), MONTH(event_at), 1) AS period", flat)
        self.assertIn("event_at >= :start_dt AND event_at < :end_exclusive", flat)
        self.assertIn("SELECT period, segment, COUNT(*) AS events FROM bucketed GROUP BY period, segment ORDER BY period, segment", flat)
        self.assertEqual(params["unknown_label"], "Unknown Territory")
        self.assertEqual(params["start_dt"].isoformat(), "2024-01-01T00:00:00")
        self.assertEqual(params["end_exclusive"].isoformat(), "2025-01-01T00:00:00")
        self.assertNotIn("2024", sql)
        self.assertNotIn("Unknown Territory", sql)
        self.assertNotIn("TOP", sql)

    def test_grains_and_unbounded_range(self) -> None:
        week, _ = build_event_counts_query(db_config(), "installs", "week")
        self.assertIn("DATEDIFF(day, '19000101', event_at) % 7", week)
        self.assertIn("event_at IS NOT NULL", week)
        self.assertNotIn(":start_dt", week)
        day, params = build_event_counts_query(db_config(), "installs", "day")
        self.assertIn("CAST(event_at AS date) AS period", day)
        self.assertEqual(params, {})

    def test_every_placeholder_has_a_bound_parameter_and_vice_versa(self) -> None:
        config = db_config()
        queries = [
            build_event_counts_query(config, "purchases", "month", "customer_size", date(2024, 1, 1), date(2024, 2, 1)),
            build_segment_counts_query(config, "customer_size", date(2024, 1, 1), date(2024, 2, 1), top_n=5),
            build_segment_counts_query(config, "territory"),
            build_customer_size_distribution_query(config),
            build_shipping_lag_query(config, date(2024, 1, 1), date(2024, 2, 1)),
            build_inventory_summary_query(config, date(2026, 10, 28), (30, 90)),
        ]
        for sql, params in queries:
            with self.subTest(sql=squash(sql)[:60]):
                self.assertEqual(set(re.findall(r"(?<![:\w]):(\w+)", sql)), set(params))
                text(sql)  # compiles as a SQLAlchemy textual statement

    def test_customer_size_counts_all_devices_per_company_before_date_filter(self) -> None:
        sql, params = build_event_counts_query(
            db_config(), "installs", "month", "customer_size", date(2024, 1, 1), date(2024, 3, 31),
        )
        flat = squash(sql)
        self.assertIn("company_sizes AS (SELECT company_key, COUNT(*) AS company_devices FROM base WHERE company_key IS NOT NULL GROUP BY company_key)", flat)
        self.assertIn("LEFT JOIN company_sizes s ON s.company_key = b.company_key", flat)
        self.assertIn("WHEN b.company_key IS NULL THEN :unknown_label", flat)
        self.assertIn("WHEN s.company_devices < :edge_0 THEN :size_label_0", flat)
        self.assertIn("WHEN s.company_devices < :edge_1 THEN :size_label_1 ELSE :size_label_2 END AS segment", flat)
        self.assertLess(flat.index("company_sizes AS"), flat.index("event_at >= :start_dt"))
        self.assertEqual(
            (params["edge_0"], params["edge_1"], params["size_label_0"], params["size_label_1"], params["size_label_2"]),
            (10, 100, "Small", "Medium", "Large"),
        )
        # The company-size CTEs must not be date filtered.
        cte_part = flat[: flat.index("bucketed AS")]
        self.assertNotIn(":start_dt", cte_part)

    def test_configurable_edges_flow_to_parameters(self) -> None:
        _, params = build_event_counts_query(
            db_config(customer_size_edges=(5, 50, 500)), "installs", "month", "customer_size",
        )
        self.assertEqual([params[f"edge_{i}"] for i in range(3)], [5, 50, 500])
        self.assertEqual(
            [params[f"size_label_{i}"] for i in range(4)], ["<5", "5-49", "50-499", "500+"],
        )
        with self.assertRaises(ValueError):
            build_event_counts_query(
                db_config(customer_size_edges=(100, 10)), "installs", "month", "customer_size",
            )

    def test_segment_counts_shape(self) -> None:
        sql, params = build_segment_counts_query(
            db_config(), "product_type", date(2024, 1, 1), date(2024, 12, 31), top_n=3,
        )
        flat = squash(sql)
        self.assertIn("SELECT TOP (:top_n) segment, COUNT(*) AS devices,", flat)
        self.assertIn("SUM(CASE WHEN purchased_at >= :start_dt AND purchased_at < :end_exclusive THEN 1 ELSE 0 END) AS purchased", flat)
        self.assertIn("SUM(CASE WHEN installed_at IS NULL THEN 1 ELSE 0 END) AS not_installed", flat)
        self.assertIn("GROUP BY segment ORDER BY devices DESC, segment", flat)
        self.assertEqual(params["top_n"], 3)

    def test_shipping_lag_is_a_day_histogram(self) -> None:
        sql, _ = build_shipping_lag_query(db_config(), date(2024, 1, 1), date(2024, 2, 1))
        flat = squash(sql)
        self.assertIn("DATEDIFF(day, shipped_at, installed_at) AS lag_days", flat)
        self.assertIn("shipped_at IS NOT NULL AND installed_at IS NOT NULL", flat)
        self.assertIn("GROUP BY lag_days ORDER BY lag_days", flat)
        self.assertNotIn("lag_days >=", flat)  # negative lags are kept, as in demo mode

    def test_inventory_uses_null_install_date_and_trailing_windows(self) -> None:
        sql, params = build_inventory_summary_query(db_config(), date(2026, 10, 28), (90, 30, 30))
        flat = squash(sql)
        self.assertIn("SUM(CASE WHEN installed_at IS NULL THEN 1 ELSE 0 END) AS on_hand", flat)
        self.assertIn("AS installs_30d", flat)
        self.assertIn("AS installs_90d", flat)
        self.assertEqual(params["as_of_exclusive"].isoformat(), "2026-10-29T00:00:00")
        self.assertEqual(params["win_start_0"].isoformat(), "2026-09-29T00:00:00")
        self.assertEqual(params["win_start_1"].isoformat(), "2026-07-31T00:00:00")
        with self.assertRaises(ValueError):
            build_inventory_summary_query(db_config(), date(2026, 10, 28), (0,))


class CohortSqlTests(unittest.TestCase):
    def setUp(self) -> None:
        data.clear_column_cache()

    def test_cohort_query_groups_by_purchase_and_install_month_in_sql(self) -> None:
        sql, params = build_purchase_install_cohorts_query(
            db_config(), date(2024, 1, 1), date(2024, 6, 30), "territory", as_of=date(2024, 5, 15),
        )
        flat = squash(sql)
        self.assertIn("[PurchaseDate] AS purchased_at", flat)
        self.assertIn("[TankInstallDate] AS installed_at", flat)
        self.assertIn("CASE WHEN installed_at < :as_of_exclusive THEN installed_at END AS installed_at, segment FROM base", flat)
        self.assertIn("DATEFROMPARTS(YEAR(purchased_at), MONTH(purchased_at), 1) AS purchase_month", flat)
        self.assertIn("DATEFROMPARTS(YEAR(installed_at), MONTH(installed_at), 1) AS install_month", flat)
        self.assertIn("CAST(installed_at AS date) < CAST(purchased_at AS date)", flat)
        self.assertIn("FROM censored WHERE purchased_at >= :start_dt AND purchased_at < :end_exclusive", flat)
        self.assertIn(
            "SELECT purchase_month, install_month, segment, COUNT(*) AS devices, SUM(is_negative) AS negative_lag_devices "
            "FROM bucketed GROUP BY purchase_month, install_month, segment ORDER BY purchase_month, install_month, segment",
            flat,
        )
        self.assertEqual(params["as_of_exclusive"].isoformat(), "2024-05-16T00:00:00")
        self.assertEqual(set(re.findall(r"(?<![:\w]):(\w+)", sql)), set(params))
        self.assertNotIn("2024", sql)

    def test_cohort_query_without_bounds_or_group_has_no_filter(self) -> None:
        sql, params = build_purchase_install_cohorts_query(db_config())
        flat = squash(sql)
        self.assertNotIn("WHERE", flat)
        self.assertNotIn("censored", flat)
        self.assertEqual(params, {})
        self.assertIn("GROUP BY purchase_month, install_month ORDER BY purchase_month, install_month", flat)

    def test_cohort_query_customer_size_counts_all_devices_before_filtering(self) -> None:
        sql, params = build_purchase_install_cohorts_query(
            db_config(), date(2024, 1, 1), date(2024, 2, 1), "customer_size",
        )
        flat = squash(sql)
        self.assertLess(flat.index("company_sizes AS"), flat.index("purchased_at >= :start_dt"))
        self.assertNotIn(":start_dt", flat[: flat.index("bucketed AS")])
        self.assertEqual(set(re.findall(r"(?<![:\w]):(\w+)", sql)), set(params))
        with self.assertRaises(ValueError):
            build_purchase_install_cohorts_query(db_config(), group_by="color")
        with self.assertRaises(ValueError):
            build_purchase_install_cohorts_query(db_config(), from_date=date(2024, 1, 1))

    def test_lag_by_cohort_query_shape_and_basis(self) -> None:
        sql, params = build_lag_by_cohort_query(
            db_config(col_shipped_at="ShipDate"), date(2024, 1, 1), date(2024, 3, 31), "shipped", date(2024, 4, 1),
        )
        flat = squash(sql)
        self.assertIn("[ShipDate] AS start_at", flat)
        self.assertIn("DATEDIFF(day, start_at, installed_at) AS lag_days", flat)
        self.assertIn("start_at IS NOT NULL AND installed_at IS NOT NULL AND start_at >= :start_dt AND start_at < :end_exclusive AND installed_at < :as_of_exclusive", flat)
        self.assertIn("GROUP BY cohort_month, lag_days ORDER BY cohort_month, lag_days", flat)
        self.assertNotIn("lag_days >=", flat)
        self.assertEqual(set(re.findall(r"(?<![:\w]):(\w+)", sql)), set(params))
        purchased, _ = build_lag_by_cohort_query(db_config(col_shipped_at="ShipDate"), basis="purchased")
        self.assertIn("[PurchaseDate] AS start_at", purchased)
        with self.assertRaises(ValueError):
            build_lag_by_cohort_query(db_config(), basis="ordered")

    def test_fetchers_validate_columns_then_normalise_sql_results(self) -> None:
        raw = pd.DataFrame({
            "purchase_month": [date(2024, 2, 1), date(2024, 1, 1), date(2024, 1, 1)],
            "install_month": [None, date(2024, 2, 1), date(2024, 1, 1)],
            "devices": [3, 2, 1], "negative_lag_devices": [0, 0, 1],
        })
        with patch("insightquest.data.fetch_flat_table_columns", return_value=COLUMNS), \
                patch("insightquest.data.pd.read_sql", return_value=raw):
            result = fetch_purchase_install_cohorts(MagicMock(), db_config())
            self.assertEqual(list(result["devices"]), [1, 2, 3])
            self.assertTrue(pd.isna(result["install_month"].iloc[-1]))
            with self.assertRaisesRegex(ValueError, "was not found"):
                fetch_purchase_install_cohorts(MagicMock(), db_config(col_sales_date="Nope"))
        lagged = pd.DataFrame({"cohort_month": [date(2024, 1, 1)] * 2, "lag_days": [5, -2], "devices": [4, 1]})
        with patch("insightquest.data.fetch_flat_table_columns", return_value=COLUMNS), \
                patch("insightquest.data.pd.read_sql", return_value=lagged):
            result = fetch_lag_by_cohort(MagicMock(), db_config(), basis="purchased")
        self.assertEqual(list(result["lag_days"]), [-2, 5])
        self.assertEqual(result["cohort_month"].dtype, "datetime64[ns]")
        summary = summarize_lag_by_cohort(result)
        self.assertEqual(summary.loc[0, "negative_lag_devices"], 1)
        self.assertEqual(summary.loc[0, "devices"], 4)
        self.assertEqual(summary.loc[0, "median_days"], 5.0)


class OptionalColumnSqlTests(unittest.TestCase):
    def setUp(self) -> None:
        data.clear_column_cache()

    def test_segment_query_omits_purchased_when_unmapped_or_excluded(self) -> None:
        for sql, params in (
            build_segment_counts_query(db_config(col_sales_date=""), "territory", date(2024, 1, 1), date(2024, 2, 1)),
            build_segment_counts_query(db_config(), "territory", date(2024, 1, 1), date(2024, 2, 1), include_purchased=False),
            build_segment_counts_query(db_config(), "customer_size", include_purchased=False),
        ):
            with self.subTest(sql=squash(sql)[:80]):
                self.assertNotIn("purchased_at", sql)
                self.assertNotIn("PurchaseDate", sql)
                self.assertIn("CAST(0 AS int) AS purchased", sql)
                self.assertIn("AS installed", sql)
                self.assertIn("AS not_installed", sql)
                self.assertEqual(set(re.findall(r"(?<![:\w]):(\w+)", sql)), set(params))
                text(sql)
        sql, _ = build_segment_counts_query(db_config(), "territory")
        self.assertIn("purchased_at", sql)

    def test_fetch_segment_counts_works_without_purchase_column(self) -> None:
        columns = COLUMNS - {"PurchaseDate"}
        raw = pd.DataFrame({
            "segment": ["Quebec"], "devices": [5], "purchased": [0], "installed": [4], "not_installed": [1],
        })
        with patch("insightquest.data.fetch_flat_table_columns", return_value=columns), \
                patch("insightquest.data.pd.read_sql", return_value=raw) as read:
            result = fetch_segment_counts(MagicMock(), db_config(), "territory")
        self.assertNotIn("purchased_at", str(read.call_args.args[0]))
        self.assertEqual(result.loc[0, "purchased"], 0)
        self.assertIs(result.attrs["purchased_available"], False)
        with patch("insightquest.data.fetch_flat_table_columns", return_value=columns), \
                patch("insightquest.data.pd.read_sql", return_value=raw):
            unmapped = fetch_segment_counts(MagicMock(), db_config(col_sales_date=""), "territory")
        self.assertIs(unmapped.attrs["purchased_available"], False)

    def test_purchase_column_is_used_when_present_and_install_column_still_required(self) -> None:
        raw = pd.DataFrame({
            "segment": ["Quebec"], "devices": [5], "purchased": [3], "installed": [4], "not_installed": [1],
        })
        with patch("insightquest.data.fetch_flat_table_columns", return_value=COLUMNS), \
                patch("insightquest.data.pd.read_sql", return_value=raw) as read:
            result = fetch_segment_counts(MagicMock(), db_config(), "territory")
        self.assertIn("purchased_at", str(read.call_args.args[0]))
        self.assertIs(result.attrs["purchased_available"], True)
        with patch("insightquest.data.fetch_flat_table_columns", return_value=COLUMNS - {"TankInstallDate"}), \
                patch("insightquest.data.pd.read_sql") as read:
            with self.assertRaisesRegex(ValueError, "was not found"):
                fetch_segment_counts(MagicMock(), db_config(), "territory")
        read.assert_not_called()

    def test_customer_size_distribution_needs_only_the_customer_column(self) -> None:
        sql, params = build_customer_size_distribution_query(db_config())
        self.assertNotIn("installed_at", sql)
        self.assertNotIn("TankInstallDate", sql)
        self.assertIn("SELECT b.company_key, CASE", squash(sql))
        self.assertEqual(set(re.findall(r"(?<![:\w]):(\w+)", sql)), set(params))
        text(sql)
        raw = pd.DataFrame({"segment": ["Small"], "companies": [2], "devices": [3]})
        with patch("insightquest.data.fetch_flat_table_columns", return_value={"CompanyId"}), \
                patch("insightquest.data.pd.read_sql", return_value=raw):
            result = fetch_customer_size_distribution(MagicMock(), db_config(col_installed_at=""))
        self.assertEqual(result.loc[0, "devices"], 3)


class FetchExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        data.clear_column_cache()

    def test_fetch_executes_built_sql_and_normalises_result(self) -> None:
        engine = MagicMock()
        raw = pd.DataFrame({
            "period": [date(2024, 2, 1), date(2024, 1, 1)],
            "segment": ["Quebec", "Quebec"], "events": [3, 2],
        })
        with patch("insightquest.data.fetch_flat_table_columns", return_value=COLUMNS), \
                patch("insightquest.data.pd.read_sql", return_value=raw) as read:
            result = fetch_monthly_purchase_counts(
                engine, db_config(), "territory", date(2024, 1, 1), date(2024, 12, 31),
            )
        statement = read.call_args.args[0]
        self.assertIn("COUNT(*) AS events", str(statement))
        self.assertEqual(read.call_args.kwargs["params"]["unknown_label"], "Unknown Territory")
        self.assertEqual(list(result["events"]), [2, 3])
        self.assertEqual(result["period"].dtype, "datetime64[ns]")

    def test_unmapped_or_missing_columns_fail_before_querying(self) -> None:
        engine = MagicMock()
        with patch("insightquest.data.fetch_flat_table_columns", return_value=COLUMNS), \
                patch("insightquest.data.pd.read_sql") as read:
            with self.assertRaisesRegex(ValueError, "was not found"):
                fetch_event_counts(engine, db_config(col_territory="Nope"), "purchases", group_by="territory")
            with self.assertRaisesRegex(ValueError, "No column is configured"):
                fetch_event_counts(engine, db_config(col_sales_date=""), "purchases")
        read.assert_not_called()

    def test_inventory_summary_fills_expected_columns(self) -> None:
        raw = pd.DataFrame({
            "product_type": ["B", "A"], "total_devices": [5, 7], "on_hand": [1, 2],
            "installs_30d": [0, 3],
        })
        with patch("insightquest.data.fetch_flat_table_columns", return_value=COLUMNS), \
                patch("insightquest.data.pd.read_sql", return_value=raw):
            result = fetch_inventory_summary(MagicMock(), db_config(), date(2026, 10, 28), (30,))
        self.assertEqual(list(result["product_type"]), ["A", "B"])
        self.assertEqual(list(result.columns), ["product_type", "total_devices", "on_hand", "installs_30d"])

    def test_engine_required_outside_demo_mode(self) -> None:
        with patch("insightquest.data.fetch_flat_table_columns", return_value=COLUMNS):
            with self.assertRaises(ValueError):
                fetch_event_counts(None, db_config(), "purchases")


class ColumnCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        data.clear_column_cache()

    def test_catalog_is_inspected_once_for_repeated_calls(self) -> None:
        inspector = MagicMock()
        inspector.get_columns.return_value = [{"name": name} for name in COLUMNS]
        config = db_config()
        with patch("insightquest.data.inspect", return_value=inspector) as inspect_call:
            first = fetch_flat_table_columns(object(), config)
            second = fetch_flat_table_columns(object(), config)
        self.assertEqual(first, COLUMNS)
        self.assertEqual(second, COLUMNS)
        inspect_call.assert_called_once()
        inspector.get_columns.assert_called_once_with("Insight Project", schema="dbo")
        inspector.has_table.assert_not_called()

    def test_missing_table_raises_and_is_not_cached(self) -> None:
        inspector = MagicMock()
        inspector.get_columns.side_effect = NoSuchTableError("x")
        with patch("insightquest.data.inspect", return_value=inspector):
            for _ in range(2):
                with self.assertRaisesRegex(ValueError, "does not exist"):
                    fetch_flat_table_columns(object(), db_config())
        self.assertEqual(inspector.get_columns.call_count, 2)

    def test_clear_column_cache_forces_refresh(self) -> None:
        inspector = MagicMock()
        inspector.get_columns.return_value = [{"name": "Id"}]
        with patch("insightquest.data.inspect", return_value=inspector):
            fetch_flat_table_columns(object(), db_config())
            data.clear_column_cache()
            fetch_flat_table_columns(object(), db_config())
        self.assertEqual(inspector.get_columns.call_count, 2)


if __name__ == "__main__":
    unittest.main()
