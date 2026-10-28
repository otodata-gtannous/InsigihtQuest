from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from streamlit.testing.v1 import AppTest

from insightquest.ai_query import Metric, validate_plan
from insightquest.config import get_config

PAGE = Path(__file__).resolve().parents[1] / "views" / "ask_the_data.py"
UNSUPPORTED_QUESTION = "Which territory has the lowest install rate?"
INTERPRET = "Let the AI interpret my question (sends the question text to OpenAI)"


def expected_install_counts_by_type() -> list[int]:
    """Installation rows per device type in the demo CSV, largest first (the plan orders desc)."""
    frame = pd.read_csv(PAGE.parents[1] / "demo_data.csv")
    installed = frame.loc[pd.to_datetime(frame["TankInstallDate"], errors="coerce").notna()]
    return installed.groupby("DeviceType").size().sort_values(ascending=False).tolist()


class OptInModelPlanTests(unittest.TestCase):
    """Model-proposed plans: opt-in, validated, shown in plain language, run only on an explicit click."""

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
        for target, kwargs in (
            ("insightquest.config.get_config", {"return_value": self.config}),
            # All UI tests are offline, even if the developer's .env contains a real API key.
            ("openai.OpenAI", {"side_effect": AssertionError("Unexpected provider call")}),
        ):
            patcher = patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)

    def page(self) -> AppTest:
        app = AppTest.from_file(str(PAGE), default_timeout=120).run()
        self.assertFalse(app.exception)
        return app

    @staticmethod
    def click(app: AppTest, label: str) -> None:
        next(button for button in app.button if button.label == label).click().run()

    def ask_unsupported(self, app: AppTest) -> None:
        app.chat_input[0].set_value(UNSUPPORTED_QUESTION).run()
        self.assertFalse(app.exception)

    def test_model_is_never_called_until_the_user_clicks(self) -> None:
        with patch("insightquest.ai_query.propose_query", return_value=self.plan) as propose:
            app = self.page()
            self.ask_unsupported(app)
            propose.assert_not_called()
            self.click(app, INTERPRET)
            propose.assert_called_once_with(UNSUPPORTED_QUESTION, self.config)

    def test_proposed_plan_runs_only_after_confirmation(self) -> None:
        with patch("insightquest.ai_query.propose_query", return_value=self.plan):
            app = self.page()
            self.ask_unsupported(app)
            self.click(app, INTERPRET)
            self.assertFalse(app.dataframe)
            self.click(app, "Run this analysis")
        self.assertFalse(app.exception)
        self.assertEqual(app.dataframe[0].value["Count"].tolist(), expected_install_counts_by_type())

    def test_unsupported_or_invalid_proposals_never_offer_execution(self) -> None:
        for outcome in ({"return_value": replace(self.plan, metric=Metric.UNSUPPORTED)},
                        {"side_effect": ValueError("Invalid plan")}):
            with self.subTest(outcome=outcome), patch("insightquest.ai_query.propose_query", **outcome):
                app = self.page()
                self.ask_unsupported(app)
                self.click(app, INTERPRET)
                self.assertFalse(app.exception)
                self.assertFalse(app.dataframe)
                self.assertNotIn("Run this analysis", [button.label for button in app.button])

    def test_disabled_external_mode_never_offers_the_model(self) -> None:
        with patch("insightquest.config.get_config", return_value=replace(self.config, ai_allow_external_data=False)):
            app = self.page()
            self.ask_unsupported(app)
        self.assertNotIn(INTERPRET, [button.label for button in app.button])
        self.assertFalse(app.toggle)

    def test_small_groups_keep_local_results_but_block_explanation(self) -> None:
        app = self.page()
        app.toggle[0].set_value(True).run()
        app.chat_input[0].set_value("How many installations by day?").run()
        self.assertFalse(app.exception)
        self.assertTrue(app.dataframe)
        self.assertNotIn("Explain with AI", [button.label for button in app.button])
        self.assertTrue(any("fewer than 10" in c.value for c in app.caption))

    def test_clear_resets_results_and_the_opt_in_toggle(self) -> None:
        app = self.page()
        app.toggle[0].set_value(True).run()
        app.chat_input[0].set_value("How many installations by device type?").run()
        self.assertTrue(app.dataframe)
        self.click(app, "Clear chat")
        self.assertFalse(app.exception)
        self.assertFalse(app.dataframe)
        self.assertFalse(app.toggle[0].value)


if __name__ == "__main__":
    unittest.main()
