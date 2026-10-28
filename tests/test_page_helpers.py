"""Scaffolding behaviour: shared dates, empty/error states, readiness, refresh, read-only warning."""

from __future__ import annotations

import io
import os
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from streamlit.testing.v1 import AppTest

import insightquest.config  # noqa: F401  (loads .env before the environment is isolated below)
from insightquest import page_helpers
from insightquest.config import get_config
from insightquest.exports import build_excel_workbook

ROOT = Path(__file__).resolve().parents[1]
TIMEOUT = 120


def setUpModule() -> None:
    global _env
    isolated = {key: value for key, value in os.environ.items() if not key.startswith("IQ_")}
    isolated["IQ_DEMO_MODE"] = "true"
    _env = patch.dict(os.environ, isolated, clear=True)
    _env.start()


def tearDownModule() -> None:
    _env.stop()


def view(name: str) -> AppTest:
    return AppTest.from_file(str(ROOT / "views" / name), default_timeout=TIMEOUT)


class ReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = replace(
            get_config(), demo_mode=True, col_installed_at="TankInstallDate", col_sales_date="PurchaseDate",
            col_shipped_at="PurchaseDate", col_territory="Territory", col_customer="CompanyId",
            col_product_type="DeviceType",
        )
        self.columns = ("TankInstallDate", "PurchaseDate", "Territory", "CompanyId", "DeviceType")

    def readiness(self, number: int, config=None, columns=None) -> str:
        return page_helpers.question_readiness(
            page_helpers.QUESTIONS[number], config or self.config, columns or self.columns
        )

    def test_all_questions_ready_when_everything_is_mapped(self) -> None:
        self.assertEqual({n: self.readiness(n) for n in range(1, 7)}, {n: "ready" for n in range(1, 7)})

    def test_partial_when_optional_fields_are_missing(self) -> None:
        config = replace(self.config, col_territory="", col_customer="")
        self.assertEqual(self.readiness(2, config), "partial")
        self.assertEqual(self.readiness(5, config), "partial")
        self.assertEqual(self.readiness(1, replace(self.config, col_sales_date="")), "partial")

    def test_unavailable_when_core_fields_are_missing(self) -> None:
        self.assertEqual(self.readiness(3, replace(self.config, col_shipped_at="")), "unavailable")
        self.assertEqual(self.readiness(6, self.config, ("PurchaseDate",)), "unavailable")
        self.assertEqual(self.readiness(2, replace(self.config, col_installed_at="Nope")), "unavailable")

    def test_mapped_status_separates_unmapped_from_absent(self) -> None:
        config = replace(self.config, col_territory="", col_customer="Gone")
        status = page_helpers.mapped_status(config, self.columns, ("col_territory", "col_customer", "col_device"))
        self.assertEqual(status["col_territory"], "unmapped")
        self.assertEqual(status["col_customer"], "absent")
        self.assertIn("IQ_COL_TERRITORY", page_helpers.describe_missing(config, status))


class SharedDateTests(unittest.TestCase):
    def test_range_survives_navigation_between_views(self) -> None:
        app = view("q1_peaks_lows.py").run()
        app.sidebar.date_input[0].set_value(date(2025, 1, 1)).run()
        self.assertEqual(app.session_state["analysis_from_date"], date(2025, 1, 1))
        # A second view sharing the same session state starts from the chosen range.
        other = view("q3_shipping_delay.py")
        other.session_state["analysis_from_date"] = app.session_state["analysis_from_date"]
        other.session_state["analysis_to_date"] = app.session_state["analysis_to_date"]
        other.run()
        self.assertEqual(other.sidebar.date_input[0].value, date(2025, 1, 1))
        self.assertFalse(other.exception)

    def test_default_range_is_consistent_and_ends_at_latest_demo_data(self) -> None:
        a, b = view("q1_peaks_lows.py").run(), view("q2_gaps.py").run()
        self.assertEqual(
            (a.session_state["analysis_from_date"], a.session_state["analysis_to_date"]),
            (b.session_state["analysis_from_date"], b.session_state["analysis_to_date"]),
        )
        self.assertLess(a.session_state["analysis_to_date"], date.today())


class StateTests(unittest.TestCase):
    def test_from_after_to_is_a_friendly_error(self) -> None:
        app = view("q1_peaks_lows.py").run()
        app.session_state["analysis_from_date"] = date(2099, 1, 1)
        app.run()
        self.assertFalse(app.exception)
        self.assertTrue(any("From date must be earlier" in e.value for e in app.error))
        self.assertFalse(app.metric)

    def test_range_without_rows_shows_empty_state(self) -> None:
        app = view("q1_peaks_lows.py").run()
        app.session_state["analysis_from_date"] = date(1990, 1, 1)
        app.session_state["analysis_to_date"] = date(1990, 12, 31)
        app.run()
        self.assertFalse(app.exception)
        self.assertTrue(any("No rows found" in i.value for i in app.info))

    def test_row_limit_reached_is_reported(self) -> None:
        app = view("overview.py").run()
        app.sidebar.number_input[0].set_value(100).run()
        self.assertFalse(app.exception)
        self.assertTrue(any("Row limit reached (100 rows)" in w.value for w in app.warning))

    def test_q3_and_q4_read_no_raw_rows_and_have_no_row_limit(self) -> None:
        for name in ("q3_shipping_delay.py", "q4_installation_rate.py"):
            with patch("insightquest.data.fetch_flat_table_rows", side_effect=AssertionError("raw rows read")):
                app = view(name).run()
            self.assertFalse(app.exception)
            self.assertFalse(app.error)
            self.assertFalse(app.sidebar.number_input, name)

    def test_demo_mode_hints_at_env_overrides_that_miss_the_demo_columns(self) -> None:
        with patch.dict(os.environ, {"IQ_COL_INSTALLED_AT": "AnalysisInstalledAt"}):
            app = view("q2_gaps.py").run()
        hints = [i.value for i in app.info if "IQ_COL_INSTALLED_AT" in i.value and "Demo mode is on" in i.value]
        self.assertTrue(hints, [i.value for i in app.info])
        self.assertIn("AnalysisInstalledAt", hints[0])
        clean = view("q2_gaps.py").run()
        self.assertFalse(any("Demo mode is on" in i.value for i in clean.info))

    def test_unmapped_column_lists_the_missing_field(self) -> None:
        with patch.dict(os.environ, {"IQ_COL_INSTALLED_AT": "NotInTheTable"}):
            app = view("q3_shipping_delay.py").run()
        self.assertFalse(app.exception)
        self.assertTrue(any("Required columns are missing" in i.value and "NotInTheTable" in i.value for i in app.info))
        self.assertFalse(app.metric)

    def test_unreachable_source_reads_as_not_configured(self) -> None:
        with patch("insightquest.page_helpers._source_columns", side_effect=ValueError("no table")):
            app = view("q2_gaps.py").run()
        self.assertFalse(app.exception)
        self.assertTrue(any("data source is not configured" in i.value for i in app.info))

    def test_failures_show_generic_message_without_trace_or_details(self) -> None:
        with patch("insightquest.cache.cached_event_counts", side_effect=RuntimeError("secret-detail")):
            with self.assertLogs("insightquest.page_helpers", level="ERROR"):
                app = view("q1_peaks_lows.py").run()
        self.assertFalse(app.exception)
        self.assertTrue(any("Something went wrong" in e.value for e in app.error))
        self.assertFalse(any("secret-detail" in e.value for e in app.error))

    def test_invalid_inventory_inputs_show_validation_message(self) -> None:
        app = view("q6_stock_thresholds.py").run()
        by_label = {widget.label: widget for widget in app.sidebar.number_input}
        by_label["Max cover days"].set_value(10).run()
        self.assertFalse(app.exception)
        self.assertTrue(any("max_cover_days must be greater than lead_time_days" in e.value for e in app.error))

    def test_refresh_button_clears_cached_data(self) -> None:
        app = AppTest.from_file(str(ROOT / "dashboard.py"), default_timeout=TIMEOUT).run()
        with patch("insightquest.page_helpers.clear_data_cache") as clear:
            next(b for b in app.sidebar.button if b.label == "Refresh data").click().run()
        clear.assert_called_once()
        self.assertFalse(app.exception)

    def test_freshness_caption_is_shown(self) -> None:
        app = view("q1_peaks_lows.py").run()
        self.assertTrue(any(c.value.startswith("Data last refreshed:") for c in app.caption))


class ReadOnlyWarningTests(unittest.TestCase):
    def warn(self, demo: bool, status) -> int:
        config = replace(get_config(), demo_mode=demo)
        with patch("insightquest.page_helpers._read_only_status", return_value=status), \
                patch("insightquest.page_helpers.st.warning") as warning:
            page_helpers.warn_if_writable(config)
        return warning.call_count

    def test_warns_only_when_the_login_can_write(self) -> None:
        self.assertEqual(self.warn(False, False), 1)
        self.assertEqual(self.warn(False, True), 0)
        self.assertEqual(self.warn(False, None), 0)

    def test_skipped_in_demo_mode(self) -> None:
        self.assertEqual(self.warn(True, False), 0)

    def test_a_failing_check_never_breaks_the_page(self) -> None:
        config = replace(get_config(), demo_mode=False)
        with patch("insightquest.page_helpers._read_only_status", side_effect=RuntimeError("down")), \
                patch("insightquest.page_helpers.st.warning") as warning:
            page_helpers.warn_if_writable(config)
        warning.assert_not_called()


class ExportFiltersTests(unittest.TestCase):
    def filters_sheet(self, name: str):
        captured = {}
        with patch("insightquest.page_helpers.render_export_buttons",
                   side_effect=lambda sheets, page, **options: captured.update(sheets=sheets, page=page, **options)):
            app = view(name).run()
        self.assertFalse(app.exception)
        self.assertIn("sheets", captured, name)
        workbook = build_excel_workbook(captured["sheets"], **{k: v for k, v in captured.items() if k not in ("sheets",)})
        return pd.read_excel(io.BytesIO(workbook), sheet_name="Filters")

    def test_date_range_appears_once_in_the_filters_sheet(self) -> None:
        for name in ("q1_peaks_lows.py", "q3_shipping_delay.py", "q4_installation_rate.py"):
            with self.subTest(view=name):
                sheet = self.filters_sheet(name)
                self.assertEqual(int((sheet["Field"] == "Date range").sum()), 1, sheet.to_string())

    def test_pages_without_date_filters_export_no_date_range(self) -> None:
        sheet = self.filters_sheet("q6_stock_thresholds.py")
        self.assertEqual(int((sheet["Field"] == "Date range").sum()), 0)


class InvalidConfigTests(unittest.TestCase):
    BAD = {"IQ_CUSTOMER_SIZE_EDGES": "abc"}

    def assert_friendly(self, app: AppTest, setting: str) -> None:
        self.assertFalse(app.exception, [e.value for e in app.exception])
        errors = [e.value for e in app.error]
        self.assertTrue(any("configuration is invalid" in e and setting in e for e in errors), errors)
        self.assertFalse(any("Traceback" in e for e in errors))

    def test_every_view_shows_a_friendly_error_naming_the_setting(self) -> None:
        for path in sorted((ROOT / "views").glob("*.py")):
            with self.subTest(view=path.name), patch.dict(os.environ, self.BAD):
                with self.assertLogs(level="ERROR"):
                    app = view(path.name).run()
                self.assert_friendly(app, "IQ_CUSTOMER_SIZE_EDGES")
                self.assertFalse(app.metric)

    def test_other_invalid_settings_are_named_or_hinted(self) -> None:
        for variable, value in (("IQ_CACHE_TTL_SECONDS", "0"), ("IQ_DB_POOL_SIZE", "1000"), ("IQ_SAMPLE_INSTALLATIONS", "-5")):
            with self.subTest(variable=variable), patch.dict(os.environ, {variable: value}):
                with self.assertLogs(level="ERROR"):
                    app = view("home.py").run()
                self.assert_friendly(app, variable)

    def test_unnamed_errors_get_a_generic_hint(self) -> None:
        with patch.dict(os.environ, {"IQ_DB_PORT": "abc"}):
            with self.assertLogs(level="ERROR"):
                app = view("q1_peaks_lows.py").run()
        self.assertTrue(any("check your IQ_* settings" in e.value for e in app.error), [e.value for e in app.error])

    def test_french_message(self) -> None:
        app = view("home.py")
        app.session_state["iq_lang"] = "fr_CA"
        with patch.dict(os.environ, self.BAD), self.assertLogs(level="ERROR"):
            app.run()
        self.assertTrue(any("configuration est invalide" in e.value for e in app.error))

    def test_entry_point_survives_a_bad_configuration(self) -> None:
        with patch.dict(os.environ, self.BAD):
            app = AppTest.from_file(str(ROOT / "dashboard.py"), default_timeout=TIMEOUT).run()
        self.assertFalse(app.exception, [e.value for e in app.exception])

    def test_warn_if_writable_ignores_invalid_configuration(self) -> None:
        with patch("insightquest.page_helpers.current_config", side_effect=ValueError("IQ_X is bad")), \
                patch("insightquest.page_helpers.st.warning") as warning:
            page_helpers.warn_if_writable()
        warning.assert_not_called()


class ChatStateTests(unittest.TestCase):
    def test_history_persists_across_reruns_in_a_session(self) -> None:
        app = view("ask_the_data.py").run()
        app.session_state["iq_chat_history"] = ["kept"]
        app.run()
        app.run()
        self.assertEqual(app.session_state["iq_chat_history"], ["kept"])

    def test_history_from_the_former_implementation_is_dropped_once(self) -> None:
        app = view("ask_the_data.py")
        app.session_state["iq_chat_history"] = ["legacy"]
        app.run()
        self.assertNotIn("iq_chat_history", app.session_state)

    def test_clear_private_chat_also_removes_history(self) -> None:
        app = view("ask_the_data.py").run()
        app.session_state["iq_chat_history"] = ["x"]
        next(b for b in app.button if b.label == "Clear chat").click().run()
        self.assertNotIn("iq_chat_history", app.session_state)


if __name__ == "__main__":
    unittest.main()
