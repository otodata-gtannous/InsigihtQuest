from __future__ import annotations

import re
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path

import pandas as pd

from insightquest.config import get_config
from insightquest.data import (
    fetch_customer_size_distribution,
    fetch_data_source_diagnostics,
    fetch_flat_table_columns,
    fetch_flat_table_rows,
    fetch_install_map_data,
    fetch_install_territory_trend_data,
    fetch_install_trend_data,
    fetch_sales_data,
)


CSV = Path(__file__).resolve().parents[1] / "demo_data.csv"


class DemoDataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = replace(
            get_config(),
            demo_mode=True,
            demo_csv_path=Path(__file__).resolve().parents[1] / "demo_data.csv",
            col_device="Id",
            col_location="DeviceLocation",
            col_installed_at="TankInstallDate",
            col_customer="CompanyId",
            col_latitude="Latitude",
            col_longitude="Longitude",
            col_territory="Territory",
            col_sales_date="PurchaseDate",
            col_sales_amount="PurchaseAmount",
            col_shipped_at="ShipmentDate",
            col_customer_size="CustomerSize",
            col_product_type="DeviceType",
            col_rate_denominator="EligibleUnits",
            col_market_indicator="MarketIndex",
            col_market_indicator_at="MarketDate",
            col_inventory_on_hand="InventoryOnHand",
            col_inventory_demand="InventoryDailyDemand",
            col_inventory_as_of="InventoryAsOf",
            col_inventory_item="InventoryItem",
            col_replenishment_days="ReplenishmentDays",
            col_reorder_point="ReorderPoint",
            col_max_stock="MaxStock",
        )

    def test_demo_schema_and_mapped_rows_load_without_database_engine(self) -> None:
        columns = fetch_flat_table_columns(None, self.config)
        self.assertIn("Latitude", columns)
        self.assertIn("PurchaseAmount", columns)

        rows = fetch_flat_table_rows(
            None,
            self.config,
            {"device": "col_device", "installed_at": "col_installed_at"},
            date_field="col_installed_at",
            limit=10,
        )
        self.assertEqual(len(rows), 10)
        self.assertEqual(list(rows.columns), ["device", "installed_at"])

    def test_legacy_overview_loaders_use_demo_csv(self) -> None:
        map_data = fetch_install_map_data(
            None, self.config, self._date("2026-04-01"), self._date("2026-10-31"), 5000
        )
        trend_data = fetch_install_trend_data(
            None, self.config, self._date("2026-04-01"), self._date("2026-10-31"), 5000
        )
        territory_data = fetch_install_territory_trend_data(
            None, self.config, self._date("2026-04-01"), self._date("2026-10-31"), 5000
        )
        sales_data = fetch_sales_data(
            None, self.config, self._date("2026-04-01"), self._date("2026-10-31")
        )
        diagnostics = fetch_data_source_diagnostics(
            None, self.config, self._date("2026-04-01"), self._date("2026-10-31")
        )

        self.assertFalse(map_data.empty)
        self.assertFalse(trend_data.empty)
        self.assertFalse(territory_data.empty)
        self.assertFalse(sales_data.empty)
        self.assertEqual(int(diagnostics.loc[0, "total_devices_in_range"]), len(trend_data))
        self.assertEqual(int(diagnostics.loc[0, "with_coordinates"]), len(map_data))

    def test_demo_file_shape_and_columns(self) -> None:
        raw = self._raw()
        self.assertEqual(
            list(raw.columns[:11]),
            ["Id", "CompanyId", "DeviceType", "PurchaseDate", "PurchaseAmount",
             "DeviceInitialInstallDate", "TankInstallDate", "Latitude", "Longitude",
             "DeviceLocation", "Territory"],
        )
        self.assertLessEqual(len(raw), 20_000)
        self.assertLessEqual(CSV.stat().st_size, 2_000_000)
        self.assertTrue(raw["Id"].is_unique)
        self.assertTrue(raw["Latitude"].between(41, 60).all())
        self.assertTrue(raw["Longitude"].between(-125, -52).all())

    def test_every_device_type_has_uninstalled_devices_but_not_all(self) -> None:
        raw = self._raw()
        on_hand = raw["TankInstallDate"].isna().groupby(raw["DeviceType"]).agg(["sum", "mean"])
        self.assertGreaterEqual(len(on_hand), 4)
        self.assertTrue((on_hand["sum"] > 0).all())
        self.assertTrue((on_hand["mean"] < 0.5).all())
        self.assertTrue(0.08 <= raw["TankInstallDate"].isna().mean() <= 0.15)
        # Types differ so that some look short on stock and others overstocked.
        self.assertGreater(on_hand["mean"].max() - on_hand["mean"].min(), 0.1)
        self.assertTrue(
            (raw["DeviceInitialInstallDate"].isna() == raw["TankInstallDate"].isna()).all()
        )

    def test_recent_purchases_are_more_often_uninstalled(self) -> None:
        raw = self._raw()
        recent = raw["purchase"] >= raw["purchase"].max() - pd.DateOffset(days=90)
        self.assertGreater(
            raw.loc[recent, "TankInstallDate"].isna().mean(),
            raw.loc[~recent, "TankInstallDate"].isna().mean() + 0.1,
        )

    def test_install_lag_is_realistic_with_a_few_invalid_rows(self) -> None:
        raw = self._raw()
        lag = (raw["install"] - raw["purchase"]).dt.days.dropna()
        self.assertGreaterEqual(int((lag < 0).sum()), 3)
        self.assertLessEqual(int((lag < 0).sum()), 20)
        self.assertGreaterEqual(int((lag > 365).sum()), 1)
        normal = lag[(lag >= 0) & (lag <= 365)]
        self.assertGreater(normal.between(5, 120).mean(), 0.98)
        self.assertLess(normal.median(), 40)

    def test_customer_size_buckets_are_all_present_and_skewed(self) -> None:
        raw = self._raw()
        per_company = raw.groupby("CompanyId").size()
        self.assertEqual(raw.groupby("CompanyId")["Territory"].nunique().max(), 1)
        small, medium, large = (
            int((per_company < 10).sum()),
            int(((per_company >= 10) & (per_company < 100)).sum()),
            int((per_company >= 100).sum()),
        )
        self.assertGreater(small, medium)
        self.assertGreaterEqual(medium, 10)
        self.assertTrue(2 <= large <= 3)

        distribution = fetch_customer_size_distribution(None, self.config)
        self.assertEqual(list(distribution["segment"]), ["Small", "Medium", "Large"])
        self.assertEqual(distribution["companies"].tolist(), [small, medium, large])
        self.assertEqual(int(distribution["devices"].sum()), len(raw))
        expected = per_company.map(
            lambda n: "Small" if n < 10 else "Medium" if n < 100 else "Large"
        )
        self.assertTrue((raw["CompanyId"].map(expected) == raw["CustomerSize"]).all())

    def test_history_supports_per_market_forecasting(self) -> None:
        raw = self._raw()
        raw["month"] = raw["purchase"].dt.to_period("M")
        span_months = (raw["month"].max() - raw["month"].min()).n + 1
        self.assertGreaterEqual(span_months, 36)

        markets = raw.groupby(["Territory", "DeviceType"]).agg(
            rows=("Id", "size"),
            active_months=("month", "nunique"),
            span=("month", lambda m: (m.max() - m.min()).n + 1),
        )
        long_markets = markets[(markets["active_months"] >= 36) & (markets["rows"] >= 100)]
        self.assertGreaterEqual(len(long_markets), 6)
        self.assertGreaterEqual(long_markets.index.get_level_values("Territory").nunique(), 4)
        self.assertGreaterEqual(long_markets.index.get_level_values("DeviceType").nunique(), 3)
        self.assertTrue((markets["span"] < 12).any(), "need a market with insufficient history")
        self.assertTrue((markets["rows"] < 20).any(), "need a tiny market")
        self.assertGreaterEqual(int((markets["span"] < 12).sum()), 2)

        by_month = raw.groupby(["Territory", "DeviceType", "month"]).size()
        growing = by_month.loc[("Quebec", "Monitor Pro")]
        self.assertGreater(growing.iloc[-12:].sum(), 2 * growing.iloc[:12].sum())
        declining = by_month.loc[("Ontario", "Monitor Pro")]
        self.assertLess(declining.iloc[-12:].sum(), 0.5 * declining.iloc[:12].sum())

        def variation(key: tuple[str, str]) -> float:
            series = by_month.loc[key].reindex(
                pd.period_range(raw["month"].min(), raw["month"].max(), freq="M"), fill_value=0
            )
            return float(series.std() / series.mean())

        self.assertLess(variation(("Quebec", "Tank Sensor")), 0.35)
        self.assertGreater(variation(("Ontario", "Monitor Basic")), 0.5)

    def test_top_territories_are_quebec_then_ontario(self) -> None:
        counts = self._raw()["Territory"].value_counts()
        self.assertEqual(list(counts.index[:2]), ["Quebec", "Ontario"])

    def test_demo_data_contains_no_real_looking_personal_data(self) -> None:
        text = CSV.read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text), "email-like value")
        self.assertIsNone(
            re.search(r"\(?\b\d{3}\)?[ .-]\d{3}[ .-]\d{4}\b", text), "phone-like value"
        )
        raw = self._raw()
        self.assertTrue(raw["CompanyId"].str.startswith("Demo ").all())
        self.assertTrue(raw["Id"].str.startswith("DEMO-").all())

    @staticmethod
    def _raw() -> pd.DataFrame:
        raw = pd.read_csv(CSV)
        raw["purchase"] = pd.to_datetime(raw["PurchaseDate"])
        raw["install"] = pd.to_datetime(raw["TankInstallDate"])
        return raw

    @staticmethod
    def _date(value: str) -> date:
        return date.fromisoformat(value)


if __name__ == "__main__":
    unittest.main()
