from __future__ import annotations

from datetime import date, timedelta

import streamlit as st
from openai import OpenAIError
from sqlalchemy.exc import SQLAlchemyError

from insightquest.ai_chat import (
    ANALYSIS_LABELS,
    Analysis,
    available_chat_fields,
    execute_analysis,
    planner_candidates,
    planner_payload,
    suggest_analysis,
)
from insightquest.config import get_config
from insightquest.data import fetch_flat_table_rows
from insightquest.db import create_db_engine
from insightquest.ai_query import (
    MAX_QUESTION_LENGTH,
    MIN_GROUP_ROWS,
    GroupBy,
    Metric,
    aggregate_payload,
    execute_query,
    explain_aggregates,
    propose_query,
)
from insightquest.page_helpers import (
    app_page_icon,
    get_source_context,
    render_analysis_page,
    warn_if_row_limit_reached,
)

def render_guided_queries() -> None:
    config = get_config()
    st.info(
        "Guided mode sends your approved question to OpenAI to propose a constrained query. "
        "The server validates the plan and calculates locally; the model cannot run SQL or code. "
        "An optional, separately approved call explains only the aggregate payload shown below."
    )
    st.warning(
        "Remove confidential information, company/device IDs, and precise locations from your "
        "question before sharing. Anonymization and minimum group sizes do not guarantee privacy."
    )
    st.caption(
        "Supported: purchase/installation row counts, pending installations, and mean/median "
        "purchase-to-install delay. Group by day/week/month or device type, filter one inclusive "
        "date range, and return up to 50 groups. Questions are independent. Unsupported requests "
        "are rejected rather than silently approximated. No source schema or sample records are sent."
    )
    if st.sidebar.button("Clear guided query"):
        for key in tuple(st.session_state):
            if key.startswith("iq_guided_"):
                del st.session_state[key]
        st.rerun()
    if not config.ai_allow_external_data:
        st.warning(
            "External sharing is disabled by default. After reviewing your organization's "
            "data-sharing policy, set IQ_AI_ALLOW_EXTERNAL_DATA=true in .env and restart the app. "
            "Private mode remains available."
        )
        return
    if not config.openai_api_key:
        st.warning("Set IQ_OPENAI_API_KEY in .env to use guided mode.")
        return
    with st.form("guided_question"):
        question = st.text_area(
            "Question to send to OpenAI",
            placeholder="How many installations were there by month in 2026?",
            max_chars=MAX_QUESTION_LENGTH,
            key="iq_guided_input",
        )
        approved = st.checkbox(
            "I approve sending this question to OpenAI.", key="iq_guided_question_approved",
        )
        propose = st.form_submit_button("Propose query")
    if propose:
        for key in (
            "iq_guided_plan", "iq_guided_result", "iq_guided_explanation",
            "iq_guided_aggregate_approved",
        ):
            st.session_state.pop(key, None)
        if not approved:
            st.error("Approve question sharing before requesting a plan. Nothing was sent.")
        else:
            try:
                with st.spinner("Requesting a constrained query plan..."):
                    plan = propose_query(question, config)
                if plan.metric == Metric.UNSUPPORTED:
                    st.warning("This request is unsupported. Rephrase it using the supported operations, or use private mode.")
                else:
                    st.session_state.iq_guided_plan = plan
                    st.session_state.iq_guided_question = question
            except (OpenAIError, ValueError, TypeError) as ex:
                st.error(f"Unable to produce a valid query plan: {ex}")
    if "iq_guided_plan" not in st.session_state:
        return
    plan = st.session_state.iq_guided_plan
    st.write(st.session_state.iq_guided_question)
    st.subheader("Review the proposed query")
    st.json(plan.payload())
    st.caption(
        "Check that the plan matches your question before running. Counts mean source rows, "
        "not distinct devices. Period ordering is chronological; type groups rank by count "
        "(delay: median days). No date filter means all available dates."
    )
    ready = config.demo_mode or config.ai_db_read_only_confirmed
    if not ready:
        st.warning(
            "Database execution is blocked until an administrator configures a SELECT-only "
            "account/source and sets IQ_AI_DB_READ_ONLY_CONFIRMED=true. This flag is an "
            "attestation, not a replacement for database permissions."
        )
    if st.button("Run confirmed query locally", disabled=not ready):
        st.session_state.pop("iq_guided_result", None)
        st.session_state.pop("iq_guided_explanation", None)
        st.session_state.pop("iq_guided_aggregate_approved", None)
        engine = None
        try:
            if not config.demo_mode:
                engine = create_db_engine(config, query_timeout_seconds=30)
            with st.spinner("Calculating locally (maximum 100,000 source rows)..."):
                st.session_state.iq_guided_result = execute_query(engine, config, plan)
        except (SQLAlchemyError, ValueError, OSError) as ex:
            st.error(f"Unable to run the local query: {ex}")
        finally:
            if engine is not None:
                engine.dispose()
    if "iq_guided_result" not in st.session_state:
        return
    result = st.session_state.iq_guided_result
    st.subheader("Locally calculated results")
    st.dataframe(result.table, use_container_width=True, hide_index=True)
    st.caption(
        f"{result.source_rows:,} source rows loaded in scope; "
        f"{result.excluded_rows:,} excluded for this metric (missing/invalid dates, negative "
        "delays, or not matching its conditions). No partial source results are used."
    )
    if result.omitted_groups:
        st.warning(f"Only the first {plan.top_n} groups in the selected order are displayed.")
    if plan.group_by == GroupBy.TYPE:
        st.caption(
            "For external explanation, device-type labels are replaced with Type group 1, "
            "Type group 2, etc., in the displayed row order. The label mapping stays local."
        )
    try:
        payload = aggregate_payload(result)
    except ValueError as ex:
        st.info(str(ex))
        return
    with st.expander("Exact aggregate payload for optional external explanation", expanded=True):
        st.json(payload)
        st.caption(
            f"No question, history, raw records, source IDs, coordinates, type labels, or "
            f"source schema are included. If any group has fewer than {MIN_GROUP_ROWS} rows, "
            "the entire external explanation is blocked, even if that group is outside top-N. "
            "Repeated overlapping queries can still reveal information; this is not differential privacy."
        )
    with st.form("guided_explanation"):
        share = st.checkbox(
            "I approve sending the displayed aggregate payload to OpenAI.",
            key="iq_guided_aggregate_approved",
        )
        explain = st.form_submit_button("Explain approved aggregates")
    if explain:
        st.session_state.pop("iq_guided_explanation", None)
        if not share:
            st.error("Approve aggregate sharing first. No results were sent.")
        else:
            try:
                with st.spinner("Explaining approved aggregates..."):
                    st.session_state.iq_guided_explanation = explain_aggregates(result, config)
            except (OpenAIError, ValueError, TypeError) as ex:
                st.error(f"Unable to explain the aggregates: {ex}")
    if "iq_guided_explanation" in st.session_state:
        st.subheader("AI explanation")
        st.text(st.session_state.iq_guided_explanation)
        st.caption("AI-generated interpretation; verify it against the local results above.")


st.set_page_config(
    page_title="Ask the Data | InsightQuest",
    page_icon=app_page_icon(),
    layout="wide",
)
render_analysis_page("Ask the data", "The model plans; your server validates and calculates.")
mode = st.radio("Analysis mode", ["Private (fixed labels only)", "Guided (approved external sharing)"])
if mode == "Guided (approved external sharing)":
    render_guided_queries()
    st.stop()
st.info(
    "Only fixed analysis labels go to OpenAI, never your question, history, records, "
    "schema, identifiers, coordinates, or results. Labels reveal the general topic. "
    "Review the proposed analysis and set any filters locally before running it."
)
st.caption(
    "Each question is independent. Named-company/device filters, arbitrary dates in "
    "questions, top-N requests, and follow-up references are not interpreted. "
    "Use the local controls below; breakdowns show all groups."
)

# Discard history/context from the former data-sharing implementation.
st.session_state.pop("iq_chat_history", None)
config = get_config()
if st.sidebar.button("Clear private chat"):
    for key in ("iq_private_question", "iq_private_payload", "iq_private_result", "iq_private_analysis"):
        st.session_state.pop(key, None)
    st.rerun()

if not config.openai_api_key:
    st.warning("Set IQ_OPENAI_API_KEY in .env for model suggestions, or choose an analysis manually.")

question = st.chat_input("Ask about installations, purchases, company/type counts, or delays")
if question:
    st.session_state.iq_private_question = question
    st.session_state.pop("iq_private_result", None)
    candidates = planner_candidates(question)
    st.session_state.iq_private_payload = planner_payload(candidates)
    if config.openai_api_key:
        try:
            with st.spinner("Selecting an analysis (no business data sent)..."):
                suggested = suggest_analysis(candidates, config)
        except (OpenAIError, ValueError, TypeError, IndexError):
            st.error("The model could not produce a valid plan. Choose an analysis manually; no results were sent.")
            suggested = Analysis.UNSUPPORTED
    else:
        suggested = candidates[0] if len(candidates) == 1 else Analysis.UNSUPPORTED
    if suggested == Analysis.UNSUPPORTED:
        st.session_state.iq_private_analysis = Analysis.OVERVIEW
        st.warning("The question could not be mapped unambiguously. Select an analysis below.")
    else:
        st.session_state.iq_private_analysis = suggested

if "iq_private_question" in st.session_state:
    with st.chat_message("user"):
        st.write(st.session_state.iq_private_question)
if "iq_private_payload" in st.session_state:
    with st.expander("External planner payload (fixed labels only)"):
        st.json(st.session_state.iq_private_payload)
        st.caption("Accompanied only by static instructions and a fixed output schema. No chat history is sent.")

with st.form("private_analysis"):
    analysis = st.selectbox(
        "Confirm analysis",
        list(ANALYSIS_LABELS),
        format_func=lambda item: ANALYSIS_LABELS[item],
        key="iq_private_analysis",
    )
    date_field = st.selectbox(
        "Local date filter",
        ["No date filter", "purchase_date", "install_date"],
    )
    start = st.date_input("From (inclusive; ignored with no date filter)", date.today() - timedelta(days=365))
    end = st.date_input("To (inclusive; ignored with no date filter)", date.today())
    limit = int(st.number_input("Maximum source rows", min_value=100, max_value=500_000, value=100_000, step=1_000))
    run = st.form_submit_button("Run confirmed analysis locally")

if run:
    st.session_state.pop("iq_private_result", None)
    try:
        config, engine, columns = get_source_context()
        fields = available_chat_fields(config, set(columns))
        if not fields:
            raise ValueError("No mapped analysis columns were found.")
        filtering = date_field != "No date filter"
        if filtering and date_field not in fields:
            raise ValueError("The selected date field is not mapped in the source.")
        frame = fetch_flat_table_rows(
            engine, config, fields,
            date_field=fields[date_field] if filtering else None,
            from_date=start if filtering else None,
            to_date=end if filtering else None,
            limit=limit,
        )
        result = execute_analysis(frame, analysis)
    except (SQLAlchemyError, ValueError, OSError) as ex:
        st.error("Unable to run the local analysis.")
        st.exception(ex)
    else:
        scope = f"{date_field}: {start} to {end}" if filtering else "No date filter"
        st.session_state.iq_private_result = (ANALYSIS_LABELS[analysis], scope, result, len(frame), limit)

if "iq_private_result" in st.session_state:
    title, scope, result, row_count, limit = st.session_state.iq_private_result
    with st.chat_message("assistant"):
        st.write(title)
        st.caption(f"{scope}. Based on {row_count:,} loaded source rows, not necessarily the entire table. Counts are row counts unless labeled distinct.")
        warn_if_row_limit_reached(row_count, limit)
        if result.empty:
            st.info("No matching rows were found.")
        else:
            st.dataframe(result, use_container_width=True, hide_index=True)
        if result.attrs.get("note"):
            st.caption(result.attrs["note"])
        st.caption("Calculated and formatted locally. These results were not sent to OpenAI.")
