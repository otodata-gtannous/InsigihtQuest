from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from insightquest.analytics import (
    PROXY_LABEL,
    assess_all_markets,
    assess_market_predictability,
    assess_purchase_predictability,
    compare_with_market,
    forecast_series,
    trend_summary,
)

SEASONAL = [20, 22, 30, 35, 30, 24, 18, 16, 22, 28, 32, 26]
NORTH = [v + 5 + (i % 5) for i, v in enumerate(SEASONAL * 3)][:30]
WOBBLY = [v + (i % 5) for i, v in enumerate(SEASONAL * 3)]


def monthly_frame(values: list[float], start: str = "2023-01-01", amount: bool = False) -> pd.DataFrame:
    months = pd.date_range(start, periods=len(values), freq="MS")
    return pd.DataFrame({"period_start": months, "value": values})


def purchase_rows(market: str | None, counts: list[int], start: str) -> pd.DataFrame:
    rows = []
    for month, count in zip(pd.date_range(start, periods=len(counts), freq="MS"), counts):
        rows += [{"purchased": month + pd.offsets.Day(14), "market": market}] * count
    return pd.DataFrame(rows)


class TrendAndForecastTests(unittest.TestCase):
    def test_trend_direction_and_percentage(self) -> None:
        up = trend_summary([10, 11, 12, 13, 14, 15])
        self.assertEqual(up["direction"], "up")
        self.assertAlmostEqual(up["slope_per_month"], 1.0)
        self.assertAlmostEqual(up["slope_pct_per_month"], 1 / 12.5 * 100)
        self.assertEqual(trend_summary([10, 9, 8, 7, 6])["direction"], "down")
        flat = trend_summary([100, 101, 100, 99, 100, 100])
        self.assertEqual(flat["direction"], "flat")

    def test_trend_threshold_period_conversion_and_degenerate_inputs(self) -> None:
        self.assertEqual(trend_summary([100, 101, 102, 103], flat_threshold_pct=5)["direction"], "flat")
        weekly = trend_summary([1, 2, 3, 4], period="Week")
        self.assertAlmostEqual(weekly["slope_per_month"], 52 / 12)
        short = trend_summary([1, 2])
        self.assertIsNone(short["slope_per_month"])
        self.assertIsNone(short["direction"])
        zero = trend_summary([0, 0, 0, 0])
        self.assertEqual(zero["slope_per_month"], 0.0)
        self.assertIsNone(zero["direction"])
        self.assertIsNone(trend_summary([np.nan, np.nan])["direction"])
        with self.assertRaises(ValueError):
            trend_summary([1, 2, 3], period="Year")

    def test_forecast_uses_holt_winters_or_seasonal_naive(self) -> None:
        long = forecast_series(monthly_frame(SEASONAL * 3), horizon=3)
        short = forecast_series(monthly_frame(SEASONAL[:8]), horizon=2)

        self.assertEqual(len(long), 3)
        self.assertEqual(long["period_start"].iloc[0], pd.Timestamp("2026-01-01"))
        self.assertEqual(set(long["method"]), {"Holt-Winters"})
        self.assertTrue((long["forecast"] >= 0).all())
        self.assertEqual(set(short["method"]), {"Seasonal Naive"})
        self.assertEqual(short["forecast"].tolist(), [SEASONAL[7]] * 2)

    def test_forecast_degenerate_inputs_and_array_input(self) -> None:
        self.assertTrue(forecast_series(monthly_frame([5]), horizon=3).empty)
        self.assertTrue(forecast_series(monthly_frame(SEASONAL), horizon=0).empty)
        self.assertTrue(forecast_series(pd.DataFrame(columns=["period_start", "value"]), 3).empty)
        from_array = forecast_series(np.array([1.0, 2.0, 3.0]), 2, last_period_start=pd.Timestamp("2025-03-01"))
        self.assertEqual(from_array["period_start"].tolist(), [pd.Timestamp("2025-04-01"), pd.Timestamp("2025-05-01")])


class OverallPredictabilityTests(unittest.TestCase):
    def test_steady_seasonal_series_scores_high_with_full_metrics(self) -> None:
        result = assess_purchase_predictability(monthly_frame(WOBBLY), horizon=4, is_proxy=True)
        m = result.metrics

        self.assertEqual(result.status, "ok")
        self.assertEqual(m["n_points"], 36)
        self.assertGreater(m["folds"], 0)
        for key in ("mae", "rmse", "mape_pct", "wape_pct", "mase", "cv", "seasonality_strength"):
            self.assertIsNotNone(m[key], key)
        self.assertGreaterEqual(m["rmse"], m["mae"])
        self.assertGreater(m["seasonality_strength"], 0.9)
        self.assertEqual(m["predictability_label"], "High")
        self.assertEqual(m["trend_direction"], "flat")
        self.assertEqual(len(result.forecast), 4)
        self.assertEqual(list(result.backtest.columns), ["period_start", "actual", "predicted", "method"])
        self.assertTrue(result.metadata["is_proxy"])
        self.assertEqual(result.metadata["date_basis_label"], PROXY_LABEL)

    def test_erratic_series_scores_lower_than_steady(self) -> None:
        rng = np.random.default_rng(7)
        erratic = rng.choice([0, 2, 50, 4, 80, 1], size=36)
        steady = assess_purchase_predictability(monthly_frame(SEASONAL * 3)).metrics
        noisy = assess_purchase_predictability(monthly_frame(list(erratic))).metrics

        self.assertLess(noisy["predictability_score"], steady["predictability_score"])
        self.assertEqual(noisy["predictability_label"], "Low")

    def test_growth_series_reports_upward_trend(self) -> None:
        result = assess_purchase_predictability(monthly_frame([10 + 2 * i for i in range(30)]))
        self.assertEqual(result.metrics["trend_direction"], "up")
        self.assertGreater(result.metrics["trend_pct_per_month"], 1)

    def test_insufficient_history_is_explicit_but_keeps_descriptives(self) -> None:
        result = assess_purchase_predictability(monthly_frame([3, 5, 4, 6, 8, 7, 9, 10]))

        self.assertEqual(result.status, "insufficient_data")
        self.assertIn("Insufficient", result.message)
        self.assertIsNone(result.metrics["mae"])
        self.assertIsNone(result.metrics["predictability_label"])
        self.assertTrue(result.backtest.empty)
        self.assertTrue(result.forecast.empty)
        self.assertIsNotNone(result.metrics["cv"])
        self.assertEqual(result.metrics["trend_direction"], "up")

    def test_all_zero_series_does_not_divide_by_zero(self) -> None:
        result = assess_purchase_predictability(monthly_frame([0] * 24))
        m = result.metrics
        self.assertEqual(result.status, "ok")
        self.assertEqual(m["mae"], 0.0)
        for key in ("mape_pct", "wape_pct", "mase", "cv", "predictability_score"):
            self.assertIsNone(m[key], key)

    def test_mape_excludes_zero_actuals_and_gaps_are_filled(self) -> None:
        values = SEASONAL * 2 + [0, 12, 0, 14, 0, 16]
        frame = monthly_frame(values)
        gapped = frame.drop(index=[5]).reset_index(drop=True)

        result = assess_purchase_predictability(frame)
        gap_result = assess_purchase_predictability(gapped)

        self.assertGreater(result.metrics["mape_zero_actuals_excluded"], 0)
        self.assertTrue(np.isfinite(result.metrics["mape_pct"]))
        self.assertEqual(gap_result.metrics["n_points"], 30)

    def test_empty_none_and_invalid_parameters(self) -> None:
        for empty in (pd.DataFrame(columns=["period_start", "value"]), None):
            result = assess_purchase_predictability(empty)
            self.assertEqual(result.status, "insufficient_data")
            self.assertEqual(result.metrics["n_points"], 0)
        with self.assertRaises(ValueError):
            assess_purchase_predictability(monthly_frame(SEASONAL), period="Year")
        with self.assertRaises(ValueError):
            assess_purchase_predictability(monthly_frame(SEASONAL), max_folds=0)
        with self.assertRaises(ValueError):
            assess_purchase_predictability(monthly_frame(SEASONAL), min_train_points=1)

    def test_custom_min_train_changes_threshold(self) -> None:
        series = monthly_frame(SEASONAL[:10])
        self.assertEqual(assess_purchase_predictability(series).status, "insufficient_data")
        self.assertEqual(assess_purchase_predictability(series, min_train_points=6).status, "ok")


class MarketPredictabilityTests(unittest.TestCase):
    def _data(self) -> pd.DataFrame:
        north = purchase_rows("North", NORTH, "2023-01-01")
        south = purchase_rows("South", [5 + i for i in range(30)], "2023-01-01")
        tiny = purchase_rows("Tiny", [1] * 6, "2025-01-01")
        unknown = purchase_rows(None, [2, 0, 3], "2023-01-01")
        return pd.concat([north, south, tiny, unknown], ignore_index=True)

    def setUp(self) -> None:
        self.result = assess_market_predictability(
            self._data(), "purchased", "market", horizon=3, is_proxy=True
        )
        self.table = self.result.table.set_index("market")

    def test_one_row_per_market_with_status_label_and_small_flag(self) -> None:
        self.assertEqual(set(self.table.index), {"North", "South", "Tiny", "Unknown"})
        self.assertEqual(self.table.loc["North", "status"], "ok")
        self.assertIn(self.table.loc["North", "predictability_label"], {"High", "Medium", "Low"})
        self.assertEqual(self.table.loc["Tiny", "status"], "insufficient_history")
        self.assertEqual(self.table.loc["Tiny", "predictability_label"], "Insufficient history")
        self.assertEqual(self.table.loc["Tiny", "n_months"], 6)
        self.assertTrue(pd.isna(self.table.loc["Tiny", "mae"]))
        self.assertTrue(self.table.loc["Tiny", "small_market"])
        self.assertTrue(self.table.loc["Unknown", "small_market"])
        self.assertFalse(self.table.loc["South", "small_market"])
        self.assertEqual(self.table.loc["Unknown", "n_months"], 30)
        self.assertEqual(self.result.table["market"].iloc[0], "North")

    def test_trend_cv_and_backtest_metrics(self) -> None:
        self.assertEqual(self.table.loc["South", "trend_direction"], "up")
        self.assertGreater(self.table.loc["South", "trend_pct_per_month"], 1)
        for key in ("cv", "mae", "rmse", "mase", "wape_pct", "predictability_score", "seasonality_strength"):
            self.assertTrue(pd.notna(self.table.loc["North", key]), key)
        self.assertTrue(pd.notna(self.table.loc["Tiny", "cv"]))

    def test_share_now_vs_year_ago_and_growth_labels(self) -> None:
        self.assertEqual(self.result.metadata["comparison_status"], "ok")
        self.assertAlmostEqual(self.table["share_recent_pct"].sum(), 100.0, places=6)
        self.assertAlmostEqual(self.table["share_prior_pct"].sum(), 100.0, places=6)
        self.assertAlmostEqual(self.table["share_change_pp"].sum(), 0.0, places=6)
        self.assertEqual(self.table.loc["South", "growth_label"], "growing")
        self.assertEqual(self.table.loc["Tiny", "growth_label"], "new")
        self.assertTrue(np.isnan(self.table.loc["Tiny", "growth_pct"]))
        self.assertEqual(self.table.loc["North", "volume_recent"], sum(NORTH[18:30]))
        self.assertEqual(self.result.metadata["recent_window"][1], pd.Timestamp("2025-06-01"))

    def test_volatility_contribution_sums_to_100(self) -> None:
        self.assertAlmostEqual(self.table["volatility_contribution_pct"].sum(), 100.0, places=6)
        self.assertTrue(self.table["volatility_vs_share"].notna().all())

    def test_forecasts_only_for_markets_with_enough_history(self) -> None:
        forecasts = self.result.forecasts
        self.assertEqual(set(forecasts["market"]), {"North", "South", "Unknown"})
        self.assertEqual(len(forecasts), 9)
        self.assertEqual(forecasts["period_start"].min(), pd.Timestamp("2025-07-01"))
        no_forecast = assess_market_predictability(self._data(), "purchased", "market")
        self.assertTrue(no_forecast.forecasts.empty)

    def test_overall_assessment_and_proxy_metadata(self) -> None:
        self.assertEqual(self.result.overall.status, "ok")
        self.assertEqual(self.result.overall.metrics["n_points"], 30)
        self.assertTrue(self.result.metadata["is_proxy"])
        self.assertEqual(self.result.metadata["date_basis_label"], PROXY_LABEL)
        self.assertIn("predictability_score", self.result.metadata["definitions"])

    def test_short_history_skips_year_over_year_comparison(self) -> None:
        data = self._data()
        data = data[data["purchased"] < "2024-07-01"]

        result = assess_market_predictability(data, "purchased", "market")

        self.assertEqual(result.metadata["comparison_status"], "insufficient_history")
        self.assertTrue(result.table["share_recent_pct"].isna().all())
        self.assertTrue(result.table["growth_label"].isna().all())

    def test_declining_and_inactive_markets(self) -> None:
        falling = purchase_rows("Falling", [20] * 12 + [10] * 12, "2023-01-01")
        gone = purchase_rows("Gone", [5] * 12, "2023-01-01")
        steady = purchase_rows("Steady", [10] * 24, "2023-01-01")

        table = assess_market_predictability(
            pd.concat([falling, gone, steady], ignore_index=True), "purchased", "market"
        ).table.set_index("market")

        self.assertEqual(table.loc["Falling", "growth_label"], "declining")
        self.assertEqual(table.loc["Gone", "growth_label"], "inactive")
        self.assertEqual(table.loc["Steady", "growth_label"], "stable")

    def test_as_of_and_partial_last_month_options(self) -> None:
        data = self._data()
        capped = assess_market_predictability(data, "purchased", "market", as_of="2024-12-15")
        self.assertEqual(capped.metadata["last_month"], pd.Timestamp("2024-12-01"))
        self.assertNotIn("Tiny", capped.table["market"].tolist())

        trimmed = assess_market_predictability(data, "purchased", "market", include_last_month=False)
        self.assertEqual(trimmed.metadata["last_month"], pd.Timestamp("2025-05-01"))

    def test_amount_measure_and_missing_values(self) -> None:
        data = pd.DataFrame(
            {
                "d": ["2024-01-05", "2024-02-05", None, "2024-03-05"],
                "m": ["A", "A", "A", None],
                "amt": [10.0, np.nan, 5.0, 7.0],
            }
        )
        result = assess_market_predictability(data, "d", "m", amount_column="amt")
        table = result.table.set_index("market")

        self.assertEqual(result.metadata["measure"], "amount")
        self.assertEqual(result.metadata["rows_missing_date"], 1)
        self.assertEqual(table.loc["A", "total_value"], 10.0)
        self.assertEqual(table.loc["Unknown", "total_value"], 7.0)
        self.assertEqual(table.loc["A", "status"], "insufficient_history")

    def test_empty_all_nat_and_invalid_arguments(self) -> None:
        empty = assess_market_predictability(pd.DataFrame(columns=["d", "m"]), "d", "m")
        self.assertTrue(empty.table.empty)
        self.assertIsNone(empty.overall)
        all_nat = assess_market_predictability(pd.DataFrame({"d": [None], "m": ["A"]}), "d", "m")
        self.assertTrue(all_nat.table.empty)
        data = self._data()
        with self.assertRaises(ValueError):
            assess_market_predictability(data, "purchased", "market", measure="amount")
        with self.assertRaises(ValueError):
            assess_market_predictability(data, "purchased", "market", min_months=3, min_folds=3)
        with self.assertRaises(ValueError):
            assess_market_predictability(data, "purchased", "market", horizon=-1)
        with self.assertRaises(KeyError):
            assess_market_predictability(data, "purchased", "nope")

    def test_single_market_and_single_month(self) -> None:
        one = assess_market_predictability(pd.DataFrame({"d": ["2025-01-05"], "m": ["A"]}), "d", "m")
        row = one.table.iloc[0]
        self.assertEqual(row["n_months"], 1)
        self.assertEqual(row["status"], "insufficient_history")
        self.assertEqual(row["share_pct"], 100.0)
        self.assertTrue(pd.isna(row["volatility_contribution_pct"]))

    def test_assess_all_markets_runs_each_dimension(self) -> None:
        data = self._data().assign(product=lambda d: np.where(d["market"] == "North", "X", "Y"))
        results = assess_all_markets(
            data, "purchased", {"territory": "market", "product_type": "product"}
        )
        self.assertEqual(set(results), {"territory", "product_type"})
        self.assertEqual(set(results["product_type"].table["market"]), {"X", "Y"})


class OptionalMarketSeriesTests(unittest.TestCase):
    def _purchases(self) -> pd.DataFrame:
        rng = np.random.default_rng(3)
        return monthly_frame(list(rng.integers(10, 60, size=30)))

    def test_detects_lag_between_external_series_and_purchases(self) -> None:
        purchases = self._purchases()
        values = purchases["value"].to_numpy()
        market = pd.Series(values[2:], index=purchases["period_start"][:-2])

        result = compare_with_market(purchases, market)

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.best_lag_months, 2)
        self.assertAlmostEqual(result.best_correlation, 1.0, places=6)
        self.assertEqual(result.table["lag_months"].tolist(), list(range(7)))
        self.assertEqual(result.table["n_obs"].iloc[0], 28)
        self.assertEqual(result.table["n_obs"].iloc[2], 28)
        self.assertIn("not show that the market causes", result.metadata["caveat"])

    def test_dataframe_inputs_and_flat_market(self) -> None:
        purchases = self._purchases()
        market = pd.DataFrame({"month": purchases["period_start"], "value": np.arange(30.0)})
        self.assertEqual(compare_with_market(purchases, market, max_lag=3).status, "ok")
        flat = pd.DataFrame({"month": purchases["period_start"], "value": 5.0})
        flat_result = compare_with_market(purchases, flat)
        self.assertEqual(flat_result.status, "insufficient_data")
        self.assertTrue(flat_result.table["correlation"].isna().all())

    def test_fewer_than_12_aligned_months_is_insufficient(self) -> None:
        purchases = self._purchases()
        market = pd.Series(np.arange(11.0), index=purchases["period_start"][:11])

        result = compare_with_market(purchases, market)

        self.assertEqual(result.status, "insufficient_data")
        self.assertEqual(result.aligned_months, 11)
        self.assertIn("11 aligned months", result.message)
        self.assertTrue(result.table["correlation"].isna().all())
        self.assertIsNone(result.best_lag_months)
        self.assertEqual(result.table["n_obs"].iloc[0], 11)

    def test_empty_inputs_and_invalid_arguments(self) -> None:
        empty = compare_with_market(pd.DataFrame(columns=["period_start", "value"]), pd.Series(dtype=float))
        self.assertEqual(empty.status, "insufficient_data")
        with self.assertRaises(ValueError):
            compare_with_market(self._purchases(), pd.Series([1.0]), max_lag=-1)
        with self.assertRaises(KeyError):
            compare_with_market(self._purchases(), pd.DataFrame({"x": [1]}))


if __name__ == "__main__":
    unittest.main()
