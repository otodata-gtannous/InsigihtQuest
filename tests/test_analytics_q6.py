from __future__ import annotations

import unittest

import pandas as pd

from insightquest.analytics import (
    classify_stock,
    simulate_inventory_risk,
    validate_inventory_params,
)

AS_OF = pd.Timestamp("2025-06-30")


def device_rows(device_type: str | None, on_hand: int, installs_in_window: int = 0,
                old_installs: int = 0, spread_days: int = 90) -> list[dict]:
    """on_hand devices without install date; installs spread over the trailing window."""
    rows = [{"type": device_type, "installed": None} for _ in range(on_hand)]
    for i in range(installs_in_window):
        rows.append({"type": device_type, "installed": AS_OF - pd.offsets.Day(i % spread_days)})
    for _ in range(old_installs):
        rows.append({"type": device_type, "installed": AS_OF - pd.offsets.Day(200)})
    return rows


def run(rows: list[dict], **kwargs):
    return simulate_inventory_risk(pd.DataFrame(rows), "type", "installed", as_of=AS_OF, **kwargs)


class ParameterTests(unittest.TestCase):
    def test_defaults_are_returned(self) -> None:
        self.assertEqual(
            validate_inventory_params(),
            {"lead_time_days": 30.0, "safety_days": 7.0, "max_cover_days": 120.0, "demand_window_days": 90.0},
        )

    def test_invalid_values_raise_clear_messages(self) -> None:
        cases = [
            ({"lead_time_days": 0}, "lead_time_days must be greater than zero"),
            ({"lead_time_days": -5}, "lead_time_days"),
            ({"safety_days": -1}, "safety_days must be zero or greater"),
            ({"max_cover_days": 0}, "max_cover_days"),
            ({"demand_window_days": 0}, "demand_window_days"),
            ({"lead_time_days": float("nan")}, "finite"),
            ({"lead_time_days": float("inf")}, "finite"),
            ({"lead_time_days": "30"}, "finite number"),
            ({"lead_time_days": True}, "finite number"),
            ({"lead_time_days": 60, "max_cover_days": 60}, "max_cover_days must be greater than lead_time_days"),
            ({"lead_time_days": 60, "max_cover_days": 30}, "max_cover_days must be greater than lead_time_days"),
        ]
        for kwargs, fragment in cases:
            with self.assertRaises(ValueError, msg=str(kwargs)) as ctx:
                validate_inventory_params(**kwargs)
            self.assertIn(fragment, str(ctx.exception))

    def test_zero_safety_days_is_allowed(self) -> None:
        self.assertEqual(validate_inventory_params(safety_days=0)["safety_days"], 0.0)


class ClassifyTests(unittest.TestCase):
    def test_boundaries_with_demand_of_one_per_day(self) -> None:
        def status(on_hand: float) -> str:
            return classify_stock(on_hand, 1.0, 30, 7, 120)

        self.assertEqual(status(0), "stockout_risk")
        self.assertEqual(status(29.99), "stockout_risk")
        self.assertEqual(status(30), "reorder")
        self.assertEqual(status(37), "reorder")
        self.assertEqual(status(37.01), "healthy")
        self.assertEqual(status(120), "healthy")
        self.assertEqual(status(120.01), "overstock")

    def test_no_demand_wins(self) -> None:
        self.assertEqual(classify_stock(0, 0, 30, 7, 120), "no_demand")
        self.assertEqual(classify_stock(500, 0, 30, 7, 120), "no_demand")


class SimulationTests(unittest.TestCase):
    def test_on_hand_demand_cover_and_thresholds(self) -> None:
        rows = device_rows("Tank", on_hand=45, installs_in_window=90, old_installs=10)

        result = run(rows)
        row = result.data.iloc[0]

        self.assertEqual(row["device_type"], "Tank")
        self.assertEqual(row["total_devices"], 145)
        self.assertEqual(row["on_hand"], 45)
        self.assertEqual(row["installs_in_window"], 90)
        self.assertAlmostEqual(row["demand_per_day"], 1.0)
        self.assertAlmostEqual(row["days_of_cover"], 45.0)
        self.assertAlmostEqual(row["reorder_point"], 30.0)
        self.assertAlmostEqual(row["safety_stock"], 7.0)
        self.assertAlmostEqual(row["reorder_level"], 37.0)
        self.assertAlmostEqual(row["max_stock"], 120.0)
        self.assertEqual(row["status"], "healthy")
        self.assertEqual(row["projected_stockout_date"], AS_OF + pd.offsets.Day(45))
        self.assertEqual(result.metadata["total_on_hand"], 45)
        self.assertEqual(result.metadata["status_counts"]["healthy"], 1)
        self.assertEqual(result.metadata["as_of"], AS_OF)

    def test_statuses_at_each_boundary(self) -> None:
        expected = {29: "stockout_risk", 30: "reorder", 37: "reorder", 38: "healthy",
                    120: "healthy", 121: "overstock"}
        rows = []
        for on_hand in expected:
            rows += device_rows(f"T{on_hand}", on_hand, installs_in_window=90)

        table = run(rows).data.set_index("device_type")

        for on_hand, status in expected.items():
            self.assertEqual(table.loc[f"T{on_hand}", "status"], status, on_hand)
        self.assertGreater(table.loc["T121", "excess_units"], 0)
        self.assertEqual(table.loc["T120", "excess_units"], 0)

    def test_demand_window_boundaries(self) -> None:
        on_window_start = {"type": "A", "installed": AS_OF - pd.offsets.Day(90)}
        inside = {"type": "A", "installed": AS_OF - pd.offsets.Day(89)}
        after = {"type": "A", "installed": AS_OF + pd.offsets.Day(1)}
        on_as_of = {"type": "A", "installed": AS_OF}

        row = run([on_window_start, inside, after, on_as_of]).data.iloc[0]

        self.assertEqual(row["installs_in_window"], 2)
        self.assertEqual(row["on_hand"], 0)

    def test_zero_on_hand_with_demand_is_stockout_today(self) -> None:
        row = run(device_rows("Z", 0, installs_in_window=9)).data.iloc[0]
        self.assertEqual(row["status"], "stockout_risk")
        self.assertEqual(row["days_of_cover"], 0)
        self.assertEqual(row["projected_stockout_date"], AS_OF)

    def test_no_demand_has_no_projection(self) -> None:
        row = run(device_rows("Idle", 10, old_installs=3)).data.iloc[0]
        self.assertEqual(row["status"], "no_demand")
        self.assertTrue(pd.isna(row["days_of_cover"]))
        self.assertTrue(pd.isna(row["projected_stockout_date"]))
        self.assertEqual(row["excess_units"], 0)

    def test_custom_parameters_and_window(self) -> None:
        rows = device_rows("A", 20, installs_in_window=30, spread_days=30)

        row = run(rows, lead_time_days=10, safety_days=2, max_cover_days=40, demand_window_days=30).data.iloc[0]

        self.assertAlmostEqual(row["demand_per_day"], 1.0)
        self.assertAlmostEqual(row["reorder_point"], 10.0)
        self.assertAlmostEqual(row["max_stock"], 40.0)
        self.assertEqual(row["status"], "healthy")
        self.assertEqual(run(rows, lead_time_days=10, safety_days=2, max_cover_days=40, demand_window_days=30).metadata["params"]["demand_window_days"], 30.0)

    def test_per_type_overrides_and_unused_overrides(self) -> None:
        rows = device_rows("Fast", 40, installs_in_window=90) + device_rows("Slow", 40, installs_in_window=90)

        result = run(rows, overrides={"Slow": {"lead_time_days": 50}, "Ghost": {"safety_days": 1}})
        table = result.data.set_index("device_type")

        self.assertEqual(table.loc["Fast", "status"], "healthy")
        self.assertEqual(table.loc["Slow", "status"], "stockout_risk")
        self.assertEqual(table.loc["Slow", "lead_time_days"], 50)
        self.assertEqual(table.loc["Fast", "lead_time_days"], 30)
        self.assertEqual(result.metadata["unused_overrides"], ["Ghost"])

    def test_invalid_overrides_raise(self) -> None:
        rows = device_rows("A", 5, installs_in_window=5)
        with self.assertRaises(ValueError) as ctx:
            run(rows, overrides={"A": {"max_cover_days": 20}})
        self.assertIn("Invalid override for 'A'", str(ctx.exception))
        with self.assertRaises(ValueError):
            run(rows, overrides={"A": {"demand_window_days": 30}})
        with self.assertRaises(ValueError):
            run(rows, overrides={"A": {"lead_time_days": -1}})
        with self.assertRaises(ValueError):
            run(rows, lead_time_days=0)

    def test_sorted_by_severity_then_name_and_unknown_type(self) -> None:
        rows = (
            device_rows("Healthy", 60, installs_in_window=90)
            + device_rows("Over", 500, installs_in_window=90)
            + device_rows("Low", 1, installs_in_window=90)
            + device_rows(None, 0, old_installs=1)
        )

        result = run(rows)

        self.assertEqual(result.data["status"].tolist(), ["stockout_risk", "healthy", "overstock", "no_demand"])
        self.assertEqual(result.data["device_type"].iloc[-1], "Unknown")
        self.assertEqual(sum(result.metadata["status_counts"].values()), 4)

    def test_as_of_defaults_to_latest_install_and_invalid_dates_are_on_hand(self) -> None:
        data = pd.DataFrame(
            {"type": ["A", "A", "A"], "installed": ["2025-03-01", "not a date", None]}
        )

        result = simulate_inventory_risk(data, "type", "installed")

        self.assertEqual(result.metadata["as_of"], pd.Timestamp("2025-03-01"))
        self.assertEqual(result.metadata["invalid_install_dates"], 1)
        self.assertEqual(result.data["on_hand"].iloc[0], 2)
        self.assertEqual(result.data["installs_in_window"].iloc[0], 1)

    def test_empty_and_missing_columns(self) -> None:
        empty = simulate_inventory_risk(pd.DataFrame(columns=["type", "installed"]), "type", "installed",
                                        overrides={"X": {"lead_time_days": 5}})
        self.assertTrue(empty.data.empty)
        self.assertEqual(empty.metadata["unused_overrides"], ["X"])
        self.assertEqual(sum(empty.metadata["status_counts"].values()), 0)
        with self.assertRaises(KeyError):
            simulate_inventory_risk(pd.DataFrame({"type": ["A"]}), "type", "installed")
        with self.assertRaises(ValueError):
            simulate_inventory_risk(pd.DataFrame(columns=["type", "installed"]), "type", "installed", lead_time_days=-1)

    def test_metadata_documents_rules_and_caveat(self) -> None:
        result = run(device_rows("A", 5, installs_in_window=5))
        self.assertEqual(set(result.metadata["status_rules"]),
                         {"no_demand", "stockout_risk", "overstock", "reorder", "healthy"})
        self.assertIn("no install date", result.metadata["caveat"])


if __name__ == "__main__":
    unittest.main()
