"""Navigation structure and the per-run wrapper that adds the chat widget to every page."""

from __future__ import annotations

import logging

import streamlit as st
from streamlit.runtime.scriptrunner import StopException

from insightquest.i18n import t
from insightquest.page_helpers import CURRENT_PAGE_KEY, QUESTIONS, refresh_data

logger = logging.getLogger(__name__)

QUESTION_ICONS = {1: "📈", 2: "🧭", 3: "🚚", 4: "📆", 5: "🔮", 6: "📦"}
QUESTION_SLUGS = {
    1: "peaks-and-lows", 2: "market-gaps", 3: "ship-to-install", 4: "install-rate", 5: "buying-outlook", 6: "stock-watch",
}
# The chat widget keys its suggestion chips by these internal page ids, not by URL slugs.
CHAT_PAGE_IDS = {slug: f"q{number}" for number, slug in QUESTION_SLUGS.items()} | {"install-map": "overview"}


def build_sections() -> dict[str, list[st.Page]]:
    """Short plain-language page names; the question number only appears on the pages themselves."""
    return {
        t("nav.start"): [
            st.Page("views/home.py", title=t("page.home"), icon="🏠", url_path="home", default=True),
        ],
        t("nav.questions"): [
            st.Page(spec.page, title=t(f"page.q{spec.number}"), icon=QUESTION_ICONS[spec.number], url_path=QUESTION_SLUGS[spec.number])
            for spec in QUESTIONS.values()
        ],
        t("nav.explore"): [
            st.Page("views/overview.py", title=t("page.overview"), icon="🗺️", url_path="install-map"),
            st.Page("views/ask_the_data.py", title=t("page.ask_the_data"), icon="💬", url_path="ask"),
        ],
    }


def render_refresh_button() -> None:
    st.sidebar.button(t("sidebar.refresh"), key="iq_refresh_data", on_click=refresh_data, help=t("sidebar.refresh_help"))


def _render_chat(page_key: str) -> None:
    try:
        from insightquest.chat_widget import render_chat_widget
    except ImportError:
        return
    context = {key: value for key, value in (st.session_state.get(CURRENT_PAGE_KEY) or {}).items() if value is not None}
    try:
        render_chat_widget(page_key=page_key, context=context or None)
    except Exception:
        logger.exception("Chat widget failed to render.")


def run_page(page: st.Page) -> None:
    """Run the selected page, then the chat widget, even when the page ends early with st.stop()."""
    st.session_state[CURRENT_PAGE_KEY] = {"question": None}
    try:
        page.run()
    except StopException:
        _render_chat(CHAT_PAGE_IDS.get(page.url_path, page.url_path))
        raise
    _render_chat(CHAT_PAGE_IDS.get(page.url_path, page.url_path))
