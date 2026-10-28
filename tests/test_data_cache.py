from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from insightquest import cache
from insightquest.config import get_config

CSV = Path(__file__).resolve().parents[1] / "demo_data.csv"


def demo_config(**overrides):
    values = dict(
        demo_mode=True, demo_csv_path=CSV, col_installed_at="TankInstallDate",
        col_sales_date="PurchaseDate", col_territory="Territory", col_customer="CompanyId",
        col_product_type="DeviceType", col_shipped_at="PurchaseDate",
    )
    values.update(overrides)
    return replace(get_config(), **values)


class CacheTests(unittest.TestCase):
    def setUp(self) -> None:
        cache.clear_data_cache()

    def tearDown(self) -> None:
        cache.clear_data_cache()

    def test_repeated_calls_hit_the_cache_and_clear_resets_it(self) -> None:
        config = demo_config()
        real = cache.data.fetch_event_counts
        with patch("insightquest.cache.data.fetch_event_counts", side_effect=real) as fetch:
            first = cache.cached_monthly_purchase_counts(config, "territory")
            second = cache.cached_monthly_purchase_counts(config, "territory")
            pd.testing.assert_frame_equal(first, second)
            self.assertEqual(fetch.call_count, 1)
            cache.cached_monthly_purchase_counts(config, "product_type")
            self.assertEqual(fetch.call_count, 2)
            cache.clear_data_cache()
            cache.cached_monthly_purchase_counts(config, "territory")
            self.assertEqual(fetch.call_count, 3)

    def test_config_change_invalidates_but_secret_change_does_not(self) -> None:
        with patch("insightquest.cache.data.fetch_segment_counts", return_value=pd.DataFrame()) as fetch:
            cache.cached_segment_counts(demo_config(), "territory")
            cache.cached_segment_counts(demo_config(db_password="rotated", openai_api_key="k"), "territory")
            self.assertEqual(fetch.call_count, 1)
            cache.cached_segment_counts(demo_config(col_territory="DeviceType"), "territory")
            self.assertEqual(fetch.call_count, 2)

    def test_all_wrappers_return_frames_in_demo_mode(self) -> None:
        config = demo_config()
        self.assertFalse(cache.cached_event_counts(config, "installs", "week").empty)
        self.assertFalse(cache.cached_segment_counts(config, "customer", date(2024, 1, 1), date(2024, 12, 31), 3).empty)
        self.assertFalse(cache.cached_customer_size_distribution(config).empty)
        self.assertFalse(cache.cached_shipping_lag_distribution(config).empty)
        self.assertEqual(
            list(cache.cached_inventory_summary(config, date(2026, 6, 30), [30, 90]).columns),
            ["product_type", "total_devices", "on_hand", "installs_30d", "installs_90d"],
        )

    def test_cohort_wrappers_are_cached_and_return_frames(self) -> None:
        config = demo_config()
        real = cache.data.fetch_purchase_install_cohorts
        with patch("insightquest.cache.data.fetch_purchase_install_cohorts", side_effect=real) as fetch:
            first = cache.cached_purchase_install_cohorts(config, group_by="territory", as_of=date(2025, 1, 31))
            second = cache.cached_purchase_install_cohorts(config, group_by="territory", as_of=date(2025, 1, 31))
            pd.testing.assert_frame_equal(first, second)
            self.assertEqual(fetch.call_count, 1)
            cache.cached_purchase_install_cohorts(config, group_by="territory")
            self.assertEqual(fetch.call_count, 2)
        self.assertEqual(
            list(cache.cached_lag_by_cohort(config, date(2024, 1, 1), date(2024, 12, 31)).columns),
            ["cohort_month", "lag_days", "devices"],
        )

    def test_demo_mode_needs_no_engine_and_database_engine_is_shared(self) -> None:
        self.assertIsNone(cache.get_cached_engine(demo_config()))
        config = demo_config(demo_mode=False, db_password="x")
        cache.clear_engine_cache()
        with patch("insightquest.cache.create_db_engine", return_value=object()) as create:
            first = cache.get_cached_engine(config)
            second = cache.get_cached_engine(replace(config, db_password="rotated"))
        self.assertIs(first, second)
        create.assert_called_once()
        cache.clear_engine_cache()


if __name__ == "__main__":
    unittest.main()
