from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path

from insightquest.config import get_config
from insightquest.data import (
    fetch_data_source_diagnostics,
    fetch_flat_table_columns,
    fetch_flat_table_rows,
    fetch_install_map_data,
    fetch_install_territory_trend_data,
    fetch_install_trend_data,
    fetch_sales_data,
)


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
            None, self.config, self._date("2026-04-01"), self._date("2026-10-31"), 500
        )
        trend_data = fetch_install_trend_data(
            None, self.config, self._date("2026-04-01"), self._date("2026-10-31"), 500
        )
        territory_data = fetch_install_territory_trend_data(
            None, self.config, self._date("2026-04-01"), self._date("2026-10-31"), 500
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

    @staticmethod
    def _date(value: str) -> date:
        return date.fromisoformat(value)


if __name__ == "__main__":
    unittest.main()
