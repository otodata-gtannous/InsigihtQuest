"""Charts keep a legend and never rely on colour alone."""

from __future__ import annotations

import json
import unittest

import pandas as pd

from insightquest import charts


class ChartSpecTests(unittest.TestCase):
    def setUp(self) -> None:
        self.frame = pd.DataFrame({
            "x": pd.to_datetime(["2024-01-01", "2024-02-01"] * 2),
            "y": [1, 2, 3, 4],
            "s": ["a", "a", "b", "b"],
        })

    def test_series_charts_have_colour_and_dash_encodings_with_a_visible_legend(self) -> None:
        spec = charts.line_chart(self.frame, "x", "y", "X", "Y", series="s", series_title="Series").to_dict()
        encoding = spec["encoding"]
        self.assertIn("strokeDash", encoding)
        self.assertIsNotNone(encoding["strokeDash"].get("legend"))
        self.assertIsNotNone(encoding["color"].get("legend"))
        self.assertEqual(encoding["color"]["title"], "Series")

    def test_palette_has_enough_contrast_on_the_dark_background(self) -> None:
        def luminance(hex_colour: str) -> float:
            channels = [int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
            linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
            return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

        def contrast(colour: str) -> float:
            return (luminance(colour) + 0.05) / (luminance(charts.THEME["background"]) + 0.05)

        for colour in charts.PALETTE + [style[0] for style in charts.STATUS_STYLE.values()]:
            self.assertGreaterEqual(contrast(colour), 3.0, colour)
        for key in ("text", "muted_text", "reference"):
            self.assertGreaterEqual(contrast(charts.THEME.get(key, charts.REFERENCE_COLOR)), 7.0, key)
        self.assertNotEqual(charts.ACTUAL_COLOR, charts.FORECAST_COLOR)

    def test_charts_config_uses_light_text_on_a_transparent_background(self) -> None:
        config = charts.style(charts.line_chart(self.frame, "x", "y", "X", "Y")).to_dict()["config"]
        self.assertEqual(config["axis"]["labelColor"], charts.THEME["muted_text"])
        self.assertEqual(config["axis"]["titleColor"], charts.THEME["text"])
        self.assertEqual(config["legend"]["labelColor"], charts.THEME["muted_text"])
        self.assertEqual(config["background"], "rgba(0,0,0,0)")

    def test_streamlit_theme_matches_chart_theme(self) -> None:
        import tomllib
        from pathlib import Path

        theme = tomllib.loads((Path(__file__).resolve().parents[1] / ".streamlit" / "config.toml").read_text())["theme"]
        self.assertEqual(theme["base"], "dark")
        self.assertEqual(theme["backgroundColor"].lower(), charts.THEME["background"].lower())
        self.assertEqual(theme["textColor"].lower(), charts.THEME["text"].lower())

    def test_palette_is_okabe_ito(self) -> None:
        okabe_ito = {"#56B4E9", "#E69F00", "#CC79A7", "#F0E442", "#D55E00", "#0072B2"}
        self.assertTrue(okabe_ito <= set(charts.PALETTE))
        self.assertEqual(len(set(charts.PALETTE)), len(charts.PALETTE))

    def test_forecast_chart_labels_actual_and_forecast_in_a_legend_with_dash_and_shape(self) -> None:
        actual = self.frame[self.frame["s"] == "a"].rename(columns={"y": "value"})
        forecast = pd.DataFrame({
            "x": pd.to_datetime(["2024-03-01"]), "forecast": [5.0], "low": [4.0], "high": [6.0],
        })
        chart = charts.forecast_chart(actual, forecast, "x", "value", "forecast", "X", "Y", "Actual", "Forecast", "low", "high")
        text = json.dumps(chart.to_dict())
        self.assertIn('"Actual"', text)
        self.assertIn('"Forecast"', text)
        self.assertIn("strokeDash", text)
        self.assertIn("diamond", text)
        self.assertIn("area", text)

    def test_forecast_chart_without_forecast_still_builds(self) -> None:
        actual = self.frame.rename(columns={"y": "value"})
        chart = charts.forecast_chart(actual, forecast=actual.iloc[0:0], x="x", y="value", forecast_y="value",
                                      x_title="X", y_title="Y", actual_label="Actual", forecast_label="Forecast")
        self.assertIn("Actual", json.dumps(chart.to_dict()))


if __name__ == "__main__":
    unittest.main()
