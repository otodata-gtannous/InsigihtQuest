"""Ask-the-data chat: a floating widget on every page and the full-screen page share this panel.

Questions are interpreted and calculated on this server. Nothing leaves it unless the
administrator enabled IQ_AI_ALLOW_EXTERNAL_DATA and the user explicitly opts in (see the AI controls).
"""

from __future__ import annotations

import logging
from contextlib import nullcontext
from datetime import date, datetime
from typing import Any

import pandas as pd
import streamlit as st
from openai import OpenAIError
from sqlalchemy.exc import SQLAlchemyError

from insightquest import ai_query
from insightquest.ai_chat import (
    EXPLAIN_TOGGLE,
    MAX_MESSAGES,
    MESSAGES,
    PENDING_QUESTION,
    Answer,
    ChatState,
    interpret_question,
    page_chip_keys,
    page_id,
    run_analysis_locally,
)
from insightquest.ai_query import MIN_GROUP_ROWS, DateField, GroupBy, Metric, Order, QueryResult
from insightquest.config import AppConfig
from insightquest.i18n import format_int, format_number, t
from insightquest.page_helpers import FROM_DATE_KEY, TO_DATE_KEY, current_config, get_source_context

logger = logging.getLogger(__name__)

PANEL_KEY = "iq_chat_panel"
FULL_PAGE_PATH = "views/ask_the_data.py"

# Streamlit's own theme variables are not exposed to CSS, so only layout and a neutral shadow
# are set here: colors come from the active (light or dark) theme.
CSS = f"""
<style>
.st-key-iq_chat_fab {{ position: fixed; right: 1.25rem; bottom: 1.25rem; z-index: 999990; width: auto; }}
.st-key-iq_chat_fab button {{ border-radius: 999px; box-shadow: 0 2px 10px rgba(0, 0, 0, 0.45); }}
div[data-testid="stPopoverBody"]:has(.st-key-{PANEL_KEY}_widget) {{
    width: min(470px, 92vw); max-height: 78vh; overflow-y: auto;
}}
</style>
"""


def _panel_key(layout: str) -> str:
    return f"{PANEL_KEY}_{layout}"


def external_ai_available(config: AppConfig) -> bool:
    """Administrator gate for every outbound call; the existing guided-mode requirements."""
    return bool(
        config.ai_allow_external_data and config.openai_api_key
        and (config.demo_mode or config.ai_db_read_only_confirmed)
    )


# -- answering ------------------------------------------------------------------


def _as_date(value: Any) -> date | None:
    """Pages record the range as ISO strings (update_current_page); the sidebar widgets hold dates."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def _scope_dates(context: dict[str, Any] | None) -> tuple[date | None, date | None]:
    context = context or {}
    start = _as_date(context.get("from_date")) or _as_date(st.session_state.get(FROM_DATE_KEY))
    end = _as_date(context.get("to_date")) or _as_date(st.session_state.get(TO_DATE_KEY))
    return (start, end) if start and end else (None, None)


def answer_question(question: str, page_key: str, context: dict[str, Any] | None) -> Answer:
    start, end = _scope_dates(context)
    interpretation = interpret_question(
        question, from_date=start, to_date=end, page_key=page_key,
        page_question=str((context or {}).get("question") or ""),
    )
    if interpretation.plan is None and interpretation.analysis is None:
        return Answer("unsupported", reason=interpretation.reason,
                      suggestions=interpretation.suggestions, question=question)
    try:
        config, engine, columns = get_source_context()
        if interpretation.plan is not None:
            result = ai_query.execute_plan_locally(engine, config, interpretation.plan)
            return Answer("result", plan=interpretation.plan, result=result, intent=interpretation.intent)
        return run_analysis_locally(engine, config, columns, interpretation.analysis, start, end)
    except ValueError as ex:
        logger.warning("Chat question could not be calculated: %s", ex)
        return Answer("error", note=str(ex))
    except (SQLAlchemyError, OSError, KeyError):
        logger.exception("Chat question failed.")
        return Answer("error")


def _label(group: str, result: QueryResult) -> str:
    plan = result.plan
    if plan.group_by != GroupBy.PERIOD:
        return group
    return group[:7] if plan.period.value == "month" else group


def summarize(result: QueryResult, intent: str) -> tuple[str, str]:
    """(headline with the number first, optional detail line)."""
    plan, table = result.plan, result.table
    if table.empty:
        return t("chat.empty"), ""
    noun = t(f"chat.noun.{plan.metric.value}")
    delay = plan.metric == Metric.DELAY
    value = "median_days" if delay else "count"
    if plan.group_by == GroupBy.NONE:
        row = table.iloc[0]
        if delay:
            return (t("chat.h.delay", median=format_number(row["median_days"])),
                    t("chat.h.delay_detail", mean=format_number(row["mean_days"]), n=format_int(row["count"])))
        return t("chat.h.total", n=format_int(row["count"]), noun=noun), ""
    if plan.group_by == GroupBy.TYPE:
        side = "low" if plan.order == Order.ASC else "high"
        key = f"chat.h.{'delay_type' if delay else 'type'}_{side}"
        row = table.iloc[0]
        return t(key, n=format_number(row[value]) if delay else format_int(row[value]),
                 noun=noun, label=row["group"], k=len(table)), ""
    unit = t(f"chat.unit.{plan.period.value}")
    labels = table["group"].map(lambda g: _label(g, result))
    if delay:
        return t("chat.h.delay_period", lo=format_number(table[value].min()),
                 hi=format_number(table[value].max()), k=len(table)), ""
    high, low = table[value].idxmax(), table[value].idxmin()
    if intent in ("high", "low"):
        pos = high if intent == "high" else low
        return t(f"chat.h.{intent}_period", n=format_int(table.loc[pos, value]), noun=noun,
                 label=labels[pos], unit=unit), ""
    detail = t("chat.h.period_detail", unit=unit, high=f"{labels[high]} ({format_int(table.loc[high, value])})",
               low=f"{labels[low]} ({format_int(table.loc[low, value])})")
    return t("chat.h.period_total", n=format_int(table[value].sum()), noun=noun), detail


def scope_text(plan: ai_query.QueryPlan) -> str:
    what = t(f"chat.noun.{plan.metric.value}")
    by = t(f"chat.by.{plan.period.value if plan.group_by == GroupBy.PERIOD else plan.group_by.value}")
    if plan.date_field == DateField.NONE:
        text = t("chat.scope_all", what=what, by=by)
    else:
        text = t("chat.scope", what=what, by=by, start=plan.start_date, end=plan.end_date)
    return " ".join(text.replace(" ,", ",").split())


def display_table(result: QueryResult) -> pd.DataFrame:
    plan, table = result.plan, result.table.copy()
    table["group"] = table["group"].map(lambda g: _label(g, result))
    names = {"group": t("chat.col.period" if plan.group_by == GroupBy.PERIOD else "chat.col.device_type"),
             "count": t("chat.col.count"), "mean_days": t("chat.col.mean_days"),
             "median_days": t("chat.col.median_days")}
    return table.rename(columns=names)


# -- rendering --------------------------------------------------------------------


def _queue(question: str) -> None:
    st.session_state[PENDING_QUESTION] = question


def _suggestion_buttons(keys: tuple[str, ...], prefix: str) -> None:
    for key in keys:
        text = t(f"chat.chip.{key}")
        st.button(text, key=f"{prefix}_{key}", on_click=_queue, args=(text,), use_container_width=True)


def _render_result(answer: Answer, index: int, layout: str, config: AppConfig, chat: ChatState) -> None:
    result = answer.result
    headline, detail = summarize(result, answer.intent)
    st.markdown(f"#### {headline}")
    if detail:
        st.caption(detail)
    if result.table.empty:
        return
    st.caption(f"{scope_text(result.plan)} · " + t(
        "chat.rows_note", rows=format_int(result.source_rows), excluded=format_int(result.excluded_rows)))
    if result.plan.group_by != GroupBy.NONE:
        shown = display_table(result)
        value = shown.columns[-1] if result.plan.metric == Metric.DELAY else t("chat.col.count")
        if layout == "widget":  # a short table keeps the number and scope visible in the small panel
            st.dataframe(shown, use_container_width=True, hide_index=True, height=min(140, 38 + 35 * len(shown)))
        else:
            st.bar_chart(shown.set_index(shown.columns[0])[value], height=170)
            st.dataframe(shown, use_container_width=True, hide_index=True, height=min(260, 38 + 35 * len(shown)))
    if result.omitted_groups:
        st.caption(t("chat.top_note", n=result.plan.top_n))
    with st.expander(t("chat.how")):
        st.json(result.plan.payload())
        st.caption(t("chat.how_caption"))
    if answer.explanation:
        st.caption(t("chat.ai.label"))
        st.text(answer.explanation)
    elif chat.get(f"{EXPLAIN_TOGGLE}_{layout}") and external_ai_available(config):
        _render_explain(answer, index, layout, config)
    if not answer.explanation:
        st.caption(t("chat.local"))


def _render_explain(answer: Answer, index: int, layout: str, config: AppConfig) -> None:
    try:
        payload = ai_query.aggregate_payload(answer.result)
    except ValueError:
        st.caption(t("chat.ai.blocked", min=MIN_GROUP_ROWS))
        return
    with st.expander(t("chat.ai.payload")):
        st.json(payload)
        st.caption(t("chat.ai.payload_caption"))
    if st.button(t("chat.ai.explain"), key=f"{layout}_explain_{index}"):
        try:
            with st.spinner("…"):
                answer.explanation = ai_query.explain_aggregates(answer.result, config)
        except (OpenAIError, ValueError, TypeError) as ex:
            st.error(t("chat.ai.error", detail=ex))
        else:
            st.caption(t("chat.ai.label"))
            st.text(answer.explanation)


def _render_proposal(answer: Answer, index: int, layout: str, config: AppConfig, state: ChatState) -> None:
    st.markdown(t("chat.ai.proposal", what=scope_text(answer.plan)))
    with st.expander(t("chat.how")):
        st.json(answer.plan.payload())
    if answer.done:
        return
    if st.button(t("chat.ai.run"), key=f"{layout}_run_{index}"):
        answer.done = True
        engine = None
        try:
            _, engine, _ = get_source_context()
            result = ai_query.execute_query(engine, config, answer.plan)
            reply = Answer("result", plan=answer.plan, result=result)
        except (SQLAlchemyError, ValueError, OSError) as ex:
            reply = Answer("error", note=str(ex))
        _append(state, "assistant", "", reply)


def _render_message(message: dict[str, Any], index: int, layout: str, config: AppConfig, chat: ChatState) -> None:
    if message["role"] == "user":
        with st.chat_message("user"):
            st.write(message["text"])
        return
    answer: Answer = message["answer"]
    with st.chat_message("assistant"):
        if answer.kind == "result":
            _render_result(answer, index, layout, config, chat)
        elif answer.kind == "table":
            st.markdown(f"#### {answer.title}")
            st.dataframe(answer.table, use_container_width=True, hide_index=True)
            if answer.note:
                st.caption(answer.note)
            st.caption(t("chat.local"))
        elif answer.kind == "proposal":
            _render_proposal(answer, index, layout, config, chat)
        elif answer.kind == "unsupported":
            st.write(t(f"chat.{answer.reason}" if answer.reason in ("ambiguous", "unclear") else "chat.unsupported"))
            _suggestion_buttons(answer.suggestions, f"{layout}_sugg_{index}")
            if answer.question and external_ai_available(config) and not answer.done:
                if st.button(t("chat.ai.interpret"), key=f"{layout}_ai_{index}"):
                    answer.done = True
                    _ask_model(answer.question, chat, config)
        else:
            st.write(t("chat.error"))
            if answer.note:
                st.caption(answer.note)


def _ask_model(question: str, chat: ChatState, config: AppConfig) -> None:
    """Opt-in: only the question text goes to OpenAI; the returned plan is validated and shown first."""
    try:
        plan = ai_query.propose_query(question, config)
    except (OpenAIError, ValueError, TypeError) as ex:
        _append(chat, "assistant", "", Answer("error", note=str(ex)))
        return
    if plan.metric == Metric.UNSUPPORTED:
        _append(chat, "assistant", "", Answer("error", note=t("chat.ai.unsupported")))
    else:
        _append(chat, "assistant", "", Answer("proposal", plan=plan))


def _append(chat: ChatState, role: str, text: str, answer: Answer | None = None) -> None:
    messages = [*chat.get(MESSAGES, []), {"role": role, "text": text, "answer": answer}]
    chat.set(MESSAGES, messages[-MAX_MESSAGES:])


def _clear(chat: ChatState) -> None:
    chat.clear_private()
    chat.drop(MESSAGES, PENDING_QUESTION)
    chat.clear_guided()


def render_chat_panel(page_key: str, context: dict[str, Any] | None = None, layout: str = "widget") -> None:
    """The chat itself. layout 'widget' (inside the popover) or 'page' (full screen)."""
    chat = ChatState(st.session_state)
    chat.discard_legacy_history()
    try:
        config = current_config()
    except ValueError:
        return
    # The widget needs a keyed container for CSS; on the page the input must stay top level to pin to the bottom.
    with st.container(key=_panel_key(layout)) if layout == "widget" else nullcontext():
        if layout == "widget":
            top = st.columns([3, 1])
            top[0].caption(t("chat.intro"))
        else:  # the page header already carries the intro
            top = st.columns([5, 1])
        top[1].button(t("chat.clear"), key=f"{layout}_clear", on_click=_clear, args=(chat,))
        if external_ai_available(config):
            st.toggle(t("chat.ai.toggle"), key=f"{EXPLAIN_TOGGLE}_{layout}", value=False)
        # The input comes first: in the widget the newest answer then sits right below it, with no inner scroll
        # area (which would auto-scroll past the headline). On the page the input pins to the bottom anyway.
        typed = st.chat_input(t("chat.placeholder"), key=f"iq_chat_input_{layout}")
        question = chat.get(PENDING_QUESTION) or typed
        chat.drop(PENDING_QUESTION)
        if question:
            _append(chat, "user", question)
            _append(chat, "assistant", "", answer_question(question, page_key, context))
        messages = chat.get(MESSAGES, [])
        with st.container():
            if not messages:
                st.markdown(f"**{t('chat.try')}**")
                _suggestion_buttons(page_chip_keys(page_key), f"{layout}_chip")
            if layout == "widget":
                _render_newest_first(chat, config)
            else:
                index = 0  # re-read each time: running a proposal appends its reply while rendering
                while index < len(chat.get(MESSAGES, [])):
                    _render_message(chat.get(MESSAGES)[index], index, layout, config, chat)
                    index += 1


def _render_newest_first(chat: ChatState, config: AppConfig) -> None:
    """Question + answer blocks, latest first, so the newest answer is visible when the panel opens."""
    messages = chat.get(MESSAGES, [])
    starts = [i for i, m in enumerate(messages) if m["role"] == "user"] or [0]
    ends = [*starts[1:], len(messages)]
    for start, end in reversed(list(zip(starts, ends))):
        for index in range(start, end):
            _render_message(messages[index], index, "widget", config, chat)
    if len(chat.get(MESSAGES, [])) > len(messages):  # a proposal just appended its reply
        st.rerun(scope="fragment")


@st.fragment
def _widget_fragment(page_key: str, context: dict[str, Any] | None) -> None:
    with st.container(key="iq_chat_fab"):
        with st.popover(f"💬 {t('chat.open')}"):
            render_chat_panel(page_key, context, "widget")
            try:
                st.page_link(FULL_PAGE_PATH, label=t("chat.fullscreen"), icon=":material/open_in_full:")
            except Exception:  # not part of a navigation (tests, embedded use)
                pass


def render_chat_widget(page_key: str, context: dict[str, Any] | None = None) -> None:
    """Floating 'Ask the data' button on every page; no-op on the full-screen chat page."""
    if page_id(page_key) == "ask":
        return
    try:
        current_config()
    except ValueError:
        return
    st.markdown(CSS, unsafe_allow_html=True)
    _widget_fragment(page_key, context)
