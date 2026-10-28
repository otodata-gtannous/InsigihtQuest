"""Chat widget and full-screen chat: every page, no key, chips, history, privacy gates."""

from __future__ import annotations

import json
import os
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import insightquest.config  # noqa: F401  (loads .env before the environment is isolated below)
from insightquest.ai_chat import PAGE_CHIPS, interpret_question, page_chip_keys
from insightquest.config import get_config
from insightquest.i18n import translate

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "views" / "ask_the_data.py"
TIMEOUT = 120
PAGES = ("home", "q1", "q2", "q3", "q4", "q5", "q6", "overview", "ask", "something_else")


def setUpModule() -> None:
    global _env
    isolated = {key: value for key, value in os.environ.items() if not key.startswith("IQ_")}
    isolated["IQ_DEMO_MODE"] = "true"
    _env = patch.dict(os.environ, isolated, clear=True)
    _env.start()


def tearDownModule() -> None:
    _env.stop()


def widget_app(page_key: str) -> AppTest:
    script = (
        "from insightquest.chat_widget import render_chat_widget\n"
        f"render_chat_widget({page_key!r}, {{'question': 'Lead time between shipping and installation'}})\n"
    )
    return AppTest.from_string(script, default_timeout=TIMEOUT)


class ChatBase(unittest.TestCase):
    allow_external = False
    api_key = ""

    def setUp(self) -> None:
        self.config = replace(
            get_config(), demo_mode=True, ai_allow_external_data=self.allow_external,
            openai_api_key=self.api_key,
        )
        patcher = patch("insightquest.config.get_config", return_value=self.config)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.provider = patch("openai.OpenAI", side_effect=AssertionError("Unexpected provider call"))
        self.provider.start()
        self.addCleanup(self.provider.stop)

    @staticmethod
    def labels(app: AppTest) -> list[str]:
        return [button.label for button in app.button]

    @staticmethod
    def ask(app: AppTest, question: str) -> AppTest:
        return app.chat_input[0].set_value(question).run()

    @staticmethod
    def click(app: AppTest, label: str) -> AppTest:
        return next(b for b in app.button if b.label == label).click().run()


class WidgetTests(ChatBase):
    def test_renders_on_every_page_key(self) -> None:
        for key in PAGES:
            with self.subTest(page=key):
                app = widget_app(key).run()
                self.assertFalse(app.exception, [e.value for e in app.exception])
                if key == "ask":  # the full-screen page hosts the chat itself
                    self.assertFalse(app.chat_input)
                else:
                    self.assertTrue(app.chat_input)

    def test_every_page_has_three_or_four_answerable_chips_in_both_languages(self) -> None:
        for key, chips in PAGE_CHIPS.items():
            self.assertIn(len(chips), (3, 4), key)
            for chip in chips:
                for language in ("en", "fr_CA"):
                    question = translate(f"chat.chip.{chip}", language)
                    interpreted = interpret_question(question)
                    self.assertTrue(interpreted.plan or interpreted.analysis, (key, language, question))
        self.assertEqual(page_chip_keys("views/q1_peaks_lows.py"), PAGE_CHIPS["q1"])
        self.assertEqual(page_chip_keys("unknown"), PAGE_CHIPS["home"])

    def test_chips_are_page_specific_and_work_without_a_key(self) -> None:
        app = widget_app("q1").run()
        peak = translate("chat.chip.installs_peak", "en")
        self.assertIn(peak, self.labels(app))
        self.assertNotIn(translate("chat.chip.fewest_type", "en"), self.labels(app))
        app = self.click(app, peak)
        self.assertFalse(app.exception, [e.value for e in app.exception])
        self.assertTrue(any("highest month" in m.value for m in app.markdown))
        self.assertTrue(app.dataframe)
        self.assertIn("Calculated on this server", " ".join(c.value for c in app.caption))

    def test_typed_question_without_key(self) -> None:
        app = self.ask(widget_app("home").run(), "How many installations by device type?")
        self.assertFalse(app.exception)
        self.assertTrue(app.dataframe)
        self.assertTrue(app.get("expander"))  # "Show how this was computed"

    def test_unsupported_question_offers_closest_supported_question(self) -> None:
        app = self.ask(widget_app("q2").run(), "Which territory has the lowest install rate?")
        self.assertFalse(app.exception)
        self.assertTrue(any("can't answer" in m.value for m in app.markdown))
        suggestion = translate("chat.chip.installs_type", "en")
        app = self.click(app, suggestion)
        self.assertTrue(app.dataframe)

    def test_widget_shows_newest_answer_first_without_a_chart(self) -> None:
        app = self.ask(widget_app("home").run(), "How many installations by month?")
        app = self.ask(app, "How many installations by device type?")
        self.assertFalse(app.exception, [e.value for e in app.exception])
        headings = [m.value for m in app.markdown if m.value.startswith("####")]
        self.assertIn("device type", headings[0])
        self.assertFalse(app.get("arrow_vega_lite_chart"))

    def test_history_persists_across_reruns_and_clears(self) -> None:
        app = self.ask(widget_app("home").run(), "How many installations by month?")
        app.run()
        self.assertEqual(len(app.session_state["iq_chat_messages"]), 2)
        self.click(app, "Clear chat")
        self.assertNotIn("iq_chat_messages", app.session_state)

    def test_guided_toggle_hidden_unless_administrator_allows_it(self) -> None:
        self.assertFalse(widget_app("home").run().toggle)
        for changes in ({"ai_allow_external_data": True}, {"openai_api_key": "k"}):
            with patch("insightquest.config.get_config", return_value=replace(self.config, **changes)):
                self.assertFalse(widget_app("home").run().toggle, changes)
        both = replace(self.config, ai_allow_external_data=True, openai_api_key="k")
        with patch("insightquest.config.get_config", return_value=both):
            toggles = widget_app("home").run().toggle
        self.assertEqual(len(toggles), 1)
        self.assertFalse(toggles[0].value)
        self.assertEqual(toggles[0].label, "Let the AI explain results (sends aggregated numbers to OpenAI)")


class GuidedOptInTests(ChatBase):
    allow_external = True
    api_key = "test-not-a-real-key"

    def test_local_answer_and_toggle_off_never_call_the_provider(self) -> None:
        app = self.ask(widget_app("home").run(), "How many installations by month?")
        self.assertFalse(app.exception)
        self.assertNotIn("Explain with AI", self.labels(app))

    def test_explanation_sends_only_the_aggregate_payload(self) -> None:
        question = "Which device type had the most purchases? SECRET-NOTE-123"
        with patch("openai.OpenAI") as client:
            client.return_value.chat.completions.create.return_value = SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="Offline explanation"))])
            app = widget_app("home").run()
            app.toggle[0].set_value(True).run()
            app = self.ask(app, question)
            client.assert_not_called()
            self.assertIn("Explain with AI", self.labels(app))
            self.click(app, "Explain with AI")
        self.assertFalse(app.exception, [e.value for e in app.exception])
        client.return_value.chat.completions.create.assert_called_once()
        sent = json.dumps(client.return_value.chat.completions.create.call_args.kwargs)
        for private in ("SECRET-NOTE-123", "DeviceType", "TankInstallDate", "iq_chat", "most purchases"):
            self.assertNotIn(private, sent)
        self.assertEqual(app.text[0].value, "Offline explanation")

    def test_unsupported_question_can_be_sent_to_the_model_only_by_an_explicit_click(self) -> None:
        app = self.ask(widget_app("home").run(), "Which territory has the lowest install rate?")
        self.assertIn("Let the AI interpret my question (sends the question text to OpenAI)", self.labels(app))


class FullPageTests(ChatBase):
    def test_full_page_shares_the_component_and_history(self) -> None:
        app = AppTest.from_file(str(PAGE), default_timeout=TIMEOUT).run()
        self.assertFalse(app.exception, [e.value for e in app.exception])
        self.assertIn(translate("chat.chip.installs_month", "en"), self.labels(app))
        app = self.ask(app, "How many purchased devices are not installed yet?")
        self.assertFalse(app.exception, [e.value for e in app.exception])
        self.assertTrue(any(m.value.startswith("####") for m in app.markdown))
        self.click(app, "Clear chat")
        self.assertNotIn("iq_chat_messages", app.session_state)

    def test_shared_date_range_is_the_default_scope(self) -> None:
        app = AppTest.from_file(str(PAGE), default_timeout=TIMEOUT).run()
        app = self.ask(app, "How many installations by month?")
        start, end = app.session_state["analysis_from_date"], app.session_state["analysis_to_date"]
        text = " ".join(c.value for c in app.caption)
        self.assertIn(f"{start} to {end}", text)

    def test_range_recorded_by_pages_as_iso_strings_is_applied(self) -> None:
        from datetime import date

        from insightquest import chat_widget

        context = {"from_date": "2024-01-01", "to_date": "2024-06-30"}
        self.assertEqual(chat_widget._scope_dates(context), (date(2024, 1, 1), date(2024, 6, 30)))
        self.assertEqual(chat_widget._scope_dates({"from_date": "garbage", "to_date": None}), (None, None))

    def test_page_context_range_scopes_the_widget_answer(self) -> None:
        script = (
            "from insightquest.chat_widget import render_chat_widget\n"
            "render_chat_widget('home', {'from_date': '2024-01-01', 'to_date': '2024-06-30'})\n"
        )
        app = AppTest.from_string(script, default_timeout=TIMEOUT).run()
        app = self.ask(app, "How many installations by month?")
        self.assertIn("2024-01-01 to 2024-06-30", " ".join(c.value for c in app.caption))


if __name__ == "__main__":
    unittest.main()
