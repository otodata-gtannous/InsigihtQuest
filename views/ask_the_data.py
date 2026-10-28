"""Full-screen Ask the data: the same chat panel as the floating widget."""

from __future__ import annotations

from insightquest.chat_widget import render_chat_panel
from insightquest.i18n import t
from insightquest.page_helpers import render_analysis_page, render_date_filters

render_analysis_page(t("page.ask_the_data"), t("chat.intro"))
render_date_filters()
render_chat_panel("ask", None, "page")
