from __future__ import annotations

import logging

import streamlit as st
from streamlit.errors import StreamlitAPIException

from insightquest.cache import cached_segment_counts
from insightquest.headlines import question_answer
from insightquest.i18n import format_int, format_percent, t
from insightquest.page_helpers import (
    QUESTIONS,
    data_freshness,
    describe_missing,
    load_config,
    load_source,
    mapped_status,
    question_readiness,
    render_date_filters,
    render_status_banners,
    update_current_page,
)

logger = logging.getLogger(__name__)

BADGES = {
    "ready": (":green[✅ {}]", "badge.ready"),
    "partial": (":orange[🟠 {}]", "badge.partial"),
    "unavailable": (":red[⛔ {}]", "badge.unavailable"),
}
EXPLORE_PAGES = (("views/overview.py", "page.overview", "🗺️"), ("views/ask_the_data.py", "page.ask_the_data", "💬"))


def page_link(path: str, label: str, icon: str | None = None) -> None:
    try:
        st.page_link(path, label=label, icon=icon)
    except StreamlitAPIException:
        # Running outside st.navigation (for example a standalone script test): no link target exists.
        logger.debug("No navigation page registered for %s", path)


def render_kpis(config, columns: tuple[str, ...]) -> None:
    dimension = next(
        (name for name, attribute in (("product_type", "col_product_type"), ("territory", "col_territory"))
         if mapped_status(config, columns, (attribute,))[attribute] == "ok"),
        None,
    )
    if dimension is None or mapped_status(config, columns, ("col_installed_at",))["col_installed_at"] != "ok":
        st.info(t("home.kpi_unavailable"))
        return
    try:
        segments = cached_segment_counts(config, dimension)
    except Exception:
        logger.exception("Home KPIs failed.")
        st.error(t("state.error_generic", detail=t("state.error_detail")))
        return
    devices = int(segments["devices"].sum())
    not_installed = int(segments["not_installed"].sum())
    installed = devices - not_installed
    cols = st.columns(4)
    cols[0].metric(t("home.kpi_devices"), format_int(devices), help=t("home.kpi_devices_help"))
    cols[1].metric(t("home.kpi_installed"), format_int(installed))
    cols[2].metric(t("home.kpi_not_installed"), format_int(not_installed), help=t("home.kpi_not_installed_help"))
    cols[3].metric(
        t("home.kpi_rate"), format_percent(installed / devices if devices else None, 1),
        help=t("home.kpi_rate_help"),
    )


def render_howto() -> None:
    with st.container(border=True):
        st.markdown(f"**{t('home.howto_title')}**")
        for column, key in zip(st.columns(3), ("home.howto_1", "home.howto_2", "home.howto_3")):
            column.markdown(t(key))


def card_answer(spec, config, columns, readiness: str, start, end) -> tuple[bool, str]:
    if readiness == "unavailable":
        statuses = mapped_status(config, columns, spec.required)
        missing = describe_missing(config, {a: s for a, s in statuses.items() if s != "ok"})
        return False, t("home.answer.unavailable", reason=t("home.reason.missing_columns", columns=missing))
    with st.spinner(t("state.loading")):
        return question_answer(config, columns, spec.number, start, end)


def render_guide(config, columns: tuple[str, ...], start, end) -> None:
    st.subheader(t("home.guide_title"))
    st.caption(t("home.guide_caption"))
    questions = list(QUESTIONS.values())
    for start_index in range(0, len(questions), 3):
        for column, spec in zip(st.columns(3), questions[start_index:start_index + 3]):
            with column.container(border=True):
                readiness = question_readiness(spec, config, columns)
                template, key = BADGES[readiness]
                st.markdown(f"**{t(f'page.q{spec.number}')}** :blue-background[{t('home.question_badge', number=spec.number)}]")
                st.caption(t(f"question.{spec.number}.title"))
                ok, answer = card_answer(spec, config, columns, readiness, start, end)
                st.markdown(answer if ok else f":gray[{answer}]")
                st.markdown(template.format(t(key)))
                page_link(spec.page, t("home.open_question"), "➡️")
    st.subheader(t("home.explore_title"))
    for column, (path, key, icon) in zip(st.columns(len(EXPLORE_PAGES)), EXPLORE_PAGES):
        with column:
            page_link(path, t(key), icon)


def main() -> None:
    config = load_config()
    if config is None:
        st.title(t("app.name"))
        return
    st.title(t("app.name"))
    st.caption(t("home.tagline"))
    render_howto()
    render_status_banners(config)
    update_current_page(question=None)
    st.sidebar.header(t("sidebar.filters"))
    start, end = render_date_filters(config)
    update_current_page(from_date=start, to_date=end)

    source = load_source(config)
    if source.error:
        st.info(t("state.source_not_configured"))
    else:
        render_kpis(config, source.columns)
    if start > end:
        st.error(t("state.date_order_error"))
        return
    render_guide(config, source.columns, start, end)
    st.caption(t("state.data_freshness", timestamp=data_freshness(config).strftime("%Y-%m-%d %H:%M")))


main()
