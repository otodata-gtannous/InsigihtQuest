"""AppTest smoke tests: every view script renders in demo mode without exceptions."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import insightquest.config  # noqa: F401  (loads .env before the environment is isolated below)

ROOT = Path(__file__).resolve().parents[1]
VIEWS = sorted((ROOT / "views").glob("*.py"))
TIMEOUT = 120


def setUpModule() -> None:
    global _env
    isolated = {key: value for key, value in os.environ.items() if not key.startswith("IQ_")}
    isolated["IQ_DEMO_MODE"] = "true"
    _env = patch.dict(os.environ, isolated, clear=True)
    _env.start()


def tearDownModule() -> None:
    _env.stop()


def run_view(name: str, language: str = "en") -> AppTest:
    app = AppTest.from_file(str(ROOT / "views" / name), default_timeout=TIMEOUT)
    app.session_state["iq_lang"] = language
    return app.run()


class ViewSmokeTests(unittest.TestCase):
    def assert_clean(self, app: AppTest, name: str) -> None:
        self.assertFalse(app.exception, f"{name}: {[e.value for e in app.exception]}")
        self.assertFalse(app.error, f"{name}: {[e.value for e in app.error]}")

    def test_every_view_file_is_covered(self) -> None:
        names = {path.name for path in VIEWS}
        self.assertEqual(
            names,
            {"home.py", "overview.py", "q1_peaks_lows.py", "q2_gaps.py", "q3_shipping_delay.py",
             "q4_installation_rate.py", "q5_predictability.py", "q6_stock_thresholds.py", "ask_the_data.py"},
        )

    def test_every_view_renders_in_english_and_french(self) -> None:
        for path in VIEWS:
            for language in ("en", "fr_CA"):
                with self.subTest(view=path.name, language=language):
                    app = run_view(path.name, language)
                    self.assert_clean(app, path.name)
                    self.assertTrue(app.title or app.markdown, f"{path.name} rendered nothing")

    def test_analysis_views_show_charts_tables_and_exports(self) -> None:
        for name in ("q1_peaks_lows.py", "q2_gaps.py", "q3_shipping_delay.py", "q4_installation_rate.py",
                     "q5_predictability.py", "q6_stock_thresholds.py"):
            with self.subTest(view=name):
                app = run_view(name)
                self.assert_clean(app, name)
                self.assertTrue(app.get("arrow_vega_lite_chart"), f"{name} has no chart")
                self.assertTrue(app.get("expander") or app.dataframe, f"{name} has no table fallback")
                self.assertTrue(app.get("download_button"), f"{name} has no export buttons")

    def test_q3_shows_histogram_and_cohort_trend_and_q4_has_no_row_limit(self) -> None:
        q3 = run_view("q3_shipping_delay.py")
        self.assert_clean(q3, "q3")
        self.assertGreaterEqual(len(q3.get("arrow_vega_lite_chart")), 2)
        q4 = run_view("q4_installation_rate.py")
        self.assert_clean(q4, "q4")
        self.assertFalse(q4.sidebar.number_input)

    def test_proxy_warning_on_purchase_timing_pages(self) -> None:
        for name, expected in (("q1_peaks_lows.py", True), ("q3_shipping_delay.py", True),
                               ("q4_installation_rate.py", True), ("q5_predictability.py", True),
                               ("q6_stock_thresholds.py", False), ("q2_gaps.py", False)):
            with self.subTest(view=name):
                app = run_view(name)
                warned = any("warranty-start proxy" in w.value for w in app.warning)
                self.assertEqual(warned, expected)

    def test_invalid_date_order_shows_error_not_trace(self) -> None:
        app = run_view("q1_peaks_lows.py")
        app.session_state["analysis_from_date"] = app.session_state["analysis_to_date"].replace(year=2099)
        app.run()
        self.assertFalse(app.exception)
        self.assertTrue(any("From date" in e.value for e in app.error))

    def test_home_shows_six_readiness_badges(self) -> None:
        app = run_view("home.py")
        self.assert_clean(app, "home.py")
        text = " ".join(m.value for m in app.markdown)
        self.assertEqual(text.count("Ready"), 6, text)
        self.assertEqual(len(app.metric), 4)
        self.assertTrue(any("Question 1" in m.value for m in app.markdown), text)


if __name__ == "__main__":
    unittest.main()
