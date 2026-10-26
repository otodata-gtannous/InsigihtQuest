from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from insightquest.ai_query import GroupBy, Metric, Period, validate_plan
from insightquest.config import get_config

PAGE = Path(__file__).resolve().parents[1] / "pages" / "6_Ask_the_Data.py"
GUIDED = "Guided (approved external sharing)"


class GuidedPageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = replace(
            get_config(), demo_mode=True, ai_allow_external_data=True,
            openai_api_key="test-not-a-real-key",
            demo_csv_path=PAGE.parents[1] / "demo_data.csv",
            col_installed_at="TankInstallDate", col_product_type="DeviceType",
        )
        self.plan = validate_plan({
            "metric": "installations", "group_by": "device_type", "period": "month",
            "date_field": "none", "start_date": None, "end_date": None,
            "order": "desc", "top_n": 5,
        })
        self.config_patch = patch("insightquest.config.get_config", return_value=self.config)
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        # All UI tests are offline, even if the developer's .env contains a real API key.
        self.provider_patch = patch("openai.OpenAI", side_effect=AssertionError("Unexpected provider call"))
        self.provider_patch.start()
        self.addCleanup(self.provider_patch.stop)

    def guided_app(self) -> AppTest:
        app = AppTest.from_file(str(PAGE)).run()
        self.assertFalse(app.exception)
        app.radio[0].set_value(GUIDED).run()
        self.assertFalse(app.exception)
        return app

    @staticmethod
    def click(app: AppTest, label: str) -> None:
        next(button for button in app.button if button.label == label).click().run()

    def submit_plan(self, app: AppTest) -> None:
        app.text_area[0].set_value("How many installations by device type?")
        app.checkbox[0].check()
        self.click(app, "Propose query")
        self.assertFalse(app.exception)

    def test_disabled_external_mode_never_offers_submit(self) -> None:
        with patch("insightquest.config.get_config", return_value=replace(self.config, ai_allow_external_data=False)):
            app = self.guided_app()
        self.assertFalse(app.text_area)
        self.assertTrue(any("disabled by default" in message.value for message in app.warning))

    def test_question_requires_approval_and_plan_requires_confirmation(self) -> None:
        with patch("insightquest.ai_query.propose_query", return_value=self.plan) as propose:
            app = self.guided_app()
            app.text_area[0].set_value("Installations by type?")
            self.click(app, "Propose query")
            propose.assert_not_called()
            self.assertTrue(any("Nothing was sent" in message.value for message in app.error))
            self.submit_plan(app)
            propose.assert_called_once()
            self.assertFalse(app.dataframe)
            self.click(app, "Run confirmed query locally")
        self.assertFalse(app.exception)
        self.assertEqual(app.dataframe[0].value["count"].tolist(), [48, 48, 48, 48])

    def test_explanation_requires_separate_approval(self) -> None:
        with (
            patch("insightquest.ai_query.propose_query", return_value=self.plan),
            patch("insightquest.ai_query.explain_aggregates", return_value="Offline explanation") as explain,
        ):
            app = self.guided_app()
            self.submit_plan(app)
            self.click(app, "Run confirmed query locally")
            explain.assert_not_called()
            self.click(app, "Explain approved aggregates")
            explain.assert_not_called()
            app.checkbox[1].check()
            self.click(app, "Explain approved aggregates")
            explain.assert_called_once()
            self.assertEqual(app.text[0].value, "Offline explanation")
            self.assertFalse(app.exception)
            self.click(app, "Run confirmed query locally")
            self.assertFalse(app.checkbox[1].value)
            self.assertFalse(app.text)

    def test_failure_removes_old_plan_and_results(self) -> None:
        with patch("insightquest.ai_query.propose_query", return_value=self.plan):
            app = self.guided_app()
            self.submit_plan(app)
            self.click(app, "Run confirmed query locally")
        with patch("insightquest.ai_query.propose_query", side_effect=ValueError("Invalid plan")):
            app.text_area[0].set_value("A new question")
            self.click(app, "Propose query")
        self.assertFalse(app.exception)
        self.assertFalse(app.dataframe)
        self.assertTrue(any("Invalid plan" in error.value for error in app.error))
        self.assertFalse(any(button.label == "Run confirmed query locally" for button in app.button))

    def test_unsupported_question_never_offers_execution(self) -> None:
        unsupported = replace(self.plan, metric=Metric.UNSUPPORTED)
        with patch("insightquest.ai_query.propose_query", return_value=unsupported):
            app = self.guided_app()
            self.submit_plan(app)
        self.assertFalse(any(button.label == "Run confirmed query locally" for button in app.button))
        self.assertTrue(any("unsupported" in message.value for message in app.warning))

    def test_small_groups_keep_local_results_but_block_explanation(self) -> None:
        plan = replace(self.plan, group_by=GroupBy.PERIOD, period=Period.DAY)
        with patch("insightquest.ai_query.propose_query", return_value=plan):
            app = self.guided_app()
            self.submit_plan(app)
            self.click(app, "Run confirmed query locally")
        self.assertFalse(app.exception)
        self.assertTrue(app.dataframe)
        self.assertFalse(any(button.label == "Explain approved aggregates" for button in app.button))
        self.assertTrue(any("fewer than 10" in message.value for message in app.info))

    def test_clear_resets_question_approval_and_results(self) -> None:
        with patch("insightquest.ai_query.propose_query", return_value=self.plan):
            app = self.guided_app()
            self.submit_plan(app)
            self.click(app, "Run confirmed query locally")
            self.click(app, "Clear guided query")
        self.assertFalse(app.exception)
        self.assertEqual(app.text_area[0].value, "")
        self.assertFalse(app.checkbox[0].value)
        self.assertFalse(app.dataframe)

    def test_private_mode_remains_default_and_manual_analysis_works(self) -> None:
        app = AppTest.from_file(str(PAGE)).run()
        self.assertEqual(app.radio[0].value, "Private (fixed labels only)")
        self.click(app, "Run confirmed analysis locally")
        self.assertFalse(app.exception)
        self.assertTrue(app.dataframe)


if __name__ == "__main__":
    unittest.main()
