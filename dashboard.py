"""InsightQuest entry point: page config, language, navigation. Pages live in views/."""

from __future__ import annotations

import streamlit as st

from insightquest.i18n import render_language_selector
from insightquest.navigation import build_sections, render_refresh_button, run_page
from insightquest.page_helpers import app_page_icon, keep_shared_state, render_app_logo, warn_if_writable

st.set_page_config(page_title="InsightQuest", page_icon=app_page_icon(), layout="wide")
render_app_logo()
render_language_selector()
render_refresh_button()
keep_shared_state()
warn_if_writable()

run_page(st.navigation(build_sections()))