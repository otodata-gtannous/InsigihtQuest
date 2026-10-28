from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import MutableMapping
from dataclasses import dataclass, replace
from datetime import date
from enum import StrEnum
from typing import Any

import pandas as pd

from insightquest.ai_query import (
    MAX_QUESTION_LENGTH,
    MAX_RESULT_ROWS,
    DateField,
    GroupBy,
    Metric,
    Order,
    Period,
    QueryPlan,
    QueryResult,
    validate_plan,
)
from insightquest.config import AppConfig
from insightquest.data import fetch_chat_groups, fetch_chat_row_profile

CHAT_FIELDS = {
    "device_id": "col_device",
    "purchase_date": "col_sales_date",
    "device_type": "col_product_type",
    "install_date": "col_installed_at",
    "company_id": "col_customer",
    "latitude": "col_latitude",
    "longitude": "col_longitude",
}


GUIDED_PREFIX = "iq_guided_"
GUIDED_INPUT = "iq_guided_input"
GUIDED_QUESTION_APPROVED = "iq_guided_question_approved"
GUIDED_AGGREGATE_APPROVED = "iq_guided_aggregate_approved"
GUIDED_PLAN = "iq_guided_plan"
GUIDED_QUESTION = "iq_guided_question"
GUIDED_RESULT = "iq_guided_result"
GUIDED_EXPLANATION = "iq_guided_explanation"
PRIVATE_QUESTION = "iq_private_question"
PRIVATE_PAYLOAD = "iq_private_payload"
PRIVATE_RESULT = "iq_private_result"
PRIVATE_ANALYSIS = "iq_private_analysis"
LEGACY_CHAT_HISTORY = "iq_chat_history"
LEGACY_PURGED = "iq_legacy_history_purged"


class ChatState:
    """Named access to the Ask-the-data session state (pass st.session_state).

    Holds UI state only. Nothing stored here is ever included in an external request:
    private mode sends fixed analysis labels, guided mode sends only what the user approves.
    """

    def __init__(self, state: MutableMapping[str, Any]) -> None:
        self._state = state

    def get(self, key: str, default: Any = None) -> Any:
        return self._state.get(key, default)

    def has(self, key: str) -> bool:
        return key in self._state

    def set(self, key: str, value: Any) -> None:
        self._state[key] = value

    def drop(self, *keys: str) -> None:
        for key in keys:
            self._state.pop(key, None)

    def discard_legacy_history(self) -> None:
        """Drop history left by the former data-sharing implementation, once per session.

        Later reruns must not touch it: this used to run on every load, which is the bug
        that cleared the history on each rerun.
        """
        if not self._state.get(LEGACY_PURGED):
            self.drop(LEGACY_CHAT_HISTORY)
            self._state[LEGACY_PURGED] = True

    def clear_guided(self) -> None:
        for key in tuple(self._state):
            if str(key).startswith(GUIDED_PREFIX):
                del self._state[key]

    def reset_guided_plan(self) -> None:
        self.drop(GUIDED_PLAN, GUIDED_RESULT, GUIDED_EXPLANATION, GUIDED_AGGREGATE_APPROVED)

    def reset_guided_results(self) -> None:
        self.drop(GUIDED_RESULT, GUIDED_EXPLANATION, GUIDED_AGGREGATE_APPROVED)

    def clear_private(self) -> None:
        self.drop(PRIVATE_QUESTION, PRIVATE_PAYLOAD, PRIVATE_RESULT, PRIVATE_ANALYSIS, LEGACY_CHAT_HISTORY)


class Analysis(StrEnum):
    OVERVIEW = "overview"
    PURCHASES = "purchases_by_month"
    INSTALLS = "installations_by_month"
    TYPES = "rows_by_device_type"
    COMPANIES = "rows_by_company"
    DELAY = "purchase_to_install_delay"
    DELAY_BY_TYPE = "delay_by_device_type"
    PENDING = "purchased_not_installed"
    LOCATIONS = "coordinate_coverage"
    UNSUPPORTED = "unsupported"


ANALYSIS_LABELS = {
    Analysis.OVERVIEW: "Dataset overview",
    Analysis.PURCHASES: "Purchases by month",
    Analysis.INSTALLS: "Installations by month",
    Analysis.TYPES: "Rows by device type",
    Analysis.COMPANIES: "Rows by company",
    Analysis.DELAY: "Purchase-to-install delay",
    Analysis.DELAY_BY_TYPE: "Purchase-to-install delay by device type",
    Analysis.PENDING: "Purchased but not installed",
    Analysis.LOCATIONS: "Coordinate coverage (no coordinates displayed)",
}

SYSTEM_PROMPT = (
    "Select one predefined analysis from the supplied candidate labels. "
    "These labels were extracted locally; no question or business data is provided. "
    "If there is not exactly one candidate, select unsupported. "
    "Return only the JSON object specified by the response schema."
)


def available_chat_fields(config: AppConfig, source_columns: set[str]) -> dict[str, str]:
    available = {name.casefold() for name in source_columns}
    return {
        alias: attribute
        for alias, attribute in CHAT_FIELDS.items()
        if getattr(config, attribute, "")
        and getattr(config, attribute).casefold() in available
    }


def planner_candidates(question: str) -> list[Analysis]:
    # Never copy words or values from the question into an external request.
    words = set(re.findall(r"[a-z]+", question.casefold()))
    install = bool(words & {"install", "installed", "installation", "installations", "installs"})
    purchase = bool(words & {"purchase", "purchased", "purchases"})
    delay = bool(words & {"delay", "delays", "longest", "median", "elapsed"})
    types = bool(words & {"type", "types"})
    company = bool(words & {"company", "companies", "customer", "customers"})
    candidates: list[Analysis] = []
    if delay:
        candidates.append(Analysis.DELAY_BY_TYPE if types else Analysis.DELAY)
    elif install and words & {"not", "pending", "uninstalled"}:
        candidates.append(Analysis.PENDING)
    elif types:
        candidates.append(Analysis.TYPES)
    elif company:
        candidates.append(Analysis.COMPANIES)
    else:
        if install:
            candidates.append(Analysis.INSTALLS)
        if purchase:
            candidates.append(Analysis.PURCHASES)
    if words & {"location", "locations", "coordinates", "map", "latitude", "longitude"}:
        candidates.append(Analysis.LOCATIONS)
    if words & {"overview", "summary", "summarize", "total"} and not candidates:
        candidates.append(Analysis.OVERVIEW)
    return candidates


def planner_payload(candidates: list[Analysis]) -> dict[str, list[str]]:
    if any(not isinstance(candidate, Analysis) for candidate in candidates):
        raise ValueError("Planner input must contain predefined analysis labels only.")
    return {"candidates": [candidate.value for candidate in candidates]}


def suggest_analysis(candidates: list[Analysis], config: AppConfig) -> Analysis:
    from openai import OpenAI

    payload = planner_payload(candidates)
    if len(candidates) != 1:
        return Analysis.UNSUPPORTED
    client = OpenAI(api_key=config.openai_api_key, timeout=30.0, max_retries=0)
    response = client.chat.completions.create(
        model=config.openai_model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload)},
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "analysis_plan",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "analysis": {
                            "type": "string",
                            "enum": [item.value for item in Analysis],
                        }
                    },
                    "required": ["analysis"],
                    "additionalProperties": False,
                },
            },
        },
    )
    content = response.choices[0].message.content
    if not content:
        raise ValueError("The planner returned no analysis. Choose an analysis manually.")
    plan = json.loads(content)
    if not isinstance(plan, dict) or set(plan) != {"analysis"}:
        raise ValueError("The planner returned an invalid analysis object.")
    analysis = Analysis(plan["analysis"])
    if analysis not in (*candidates, Analysis.UNSUPPORTED):
        raise ValueError("The planner selected an analysis outside the allowed candidates.")
    return analysis


def execute_analysis(
    frame: pd.DataFrame,
    analysis: Analysis,
) -> pd.DataFrame:
    if analysis not in ANALYSIS_LABELS:
        raise ValueError("Unsupported analysis. Select one of the predefined analyses.")
    data = frame.copy()
    for column in ("purchase_date", "install_date"):
        if column in data:
            data[column] = pd.to_datetime(data[column], errors="coerce")

    def require(*columns: str) -> None:
        missing = set(columns) - set(data.columns)
        if missing:
            raise ValueError(f"Required mapped fields are missing: {', '.join(sorted(missing))}")

    if analysis == Analysis.OVERVIEW:
        metrics = {"Rows": len(data)}
        for field, label in (("device_id", "Distinct device IDs"), ("company_id", "Distinct companies")):
            if field in data:
                metrics[label] = data[field].nunique()
        return pd.DataFrame(metrics.items(), columns=["Metric", "Count"])
    if analysis in {Analysis.PURCHASES, Analysis.INSTALLS}:
        field = "purchase_date" if analysis == Analysis.PURCHASES else "install_date"
        require(field)
        counts = data[field].dropna().dt.to_period("M").astype(str).value_counts().sort_index()
        result = counts.rename_axis("Month").reset_index(name="Rows with date")
        result.attrs["note"] = f"Rows missing a valid date: {int(data[field].isna().sum()):,}."
        return result
    if analysis in {Analysis.TYPES, Analysis.COMPANIES}:
        field = "device_type" if analysis == Analysis.TYPES else "company_id"
        require(field)
        counts = data[field].astype("string").fillna("(missing)").value_counts()
        return counts.rename_axis(field).reset_index(name="Rows")
    if analysis in {Analysis.DELAY, Analysis.DELAY_BY_TYPE, Analysis.PENDING}:
        require("purchase_date", "install_date")
        if analysis == Analysis.PENDING:
            pending = data["purchase_date"].notna() & data["install_date"].isna()
            return pd.DataFrame({"Metric": ["Purchased rows without a valid install date"], "Count": [int(pending.sum())]})
        days = (data["install_date"] - data["purchase_date"]).dt.total_seconds() / 86400
        valid = days[days >= 0]
        if analysis == Analysis.DELAY_BY_TYPE:
            require("device_type")
            grouped = pd.DataFrame({
                "Device type": data["device_type"].astype("string").fillna("(missing)"),
                "Delay days": days,
            }).loc[days >= 0]
            result = grouped.groupby("Device type")["Delay days"].agg(
                Count="count", Median="median", Mean="mean", Min="min", Max="max"
            ).round(2).sort_values("Median", ascending=False).reset_index()
        else:
            result = pd.DataFrame({
                "Valid paired rows": [len(valid)],
                "Mean days": [valid.mean()],
                "Median days": [valid.median()],
                "Min days": [valid.min()],
                "Max days": [valid.max()],
            }).round(2)
        result.attrs["note"] = (
            f"Excluded {int(days.isna().sum()):,} rows with missing dates and "
            f"{int((days < 0).sum()):,} rows with negative delay."
        )
        return result
    require("latitude", "longitude")
    lat = pd.to_numeric(data["latitude"], errors="coerce")
    lon = pd.to_numeric(data["longitude"], errors="coerce")
    valid_coords = lat.between(-90, 90) & lon.between(-180, 180)
    return pd.DataFrame({
        "Metric": ["Rows with valid coordinates", "Rows with missing or invalid coordinates"],
        "Count": [int(valid_coords.sum()), int((~valid_coords).sum())],
    })


# -- local question interpretation (no model, nothing leaves the server) ------------

MESSAGES = "iq_chat_messages"
PENDING_QUESTION = "iq_chat_pending_question"
EXPLAIN_TOGGLE = "iq_guided_explain_toggle"  # guided prefix: cleared with the other guided state
MAX_MESSAGES = 24

PAGE_CHIPS: dict[str, tuple[str, ...]] = {
    "home": ("installs_month", "pending", "installs_type", "delay_median"),
    "q1": ("installs_peak", "purchases_low", "purchases_month", "installs_week"),
    "q2": ("fewest_type", "installs_type", "delay_type", "most_purchases_type"),
    "q3": ("delay_median", "delay_type", "pending", "installs_month"),
    "q4": ("installs_month", "pending", "purchases_month", "installs_peak"),
    "q5": ("purchases_month", "most_purchases_type", "purchases_low", "installs_type"),
    "q6": ("installs_type", "fewest_type", "delay_type", "pending"),
    "overview": ("overview", "installs_month", "installs_type", "pending"),
    "ask": ("installs_month", "pending", "installs_type", "delay_median"),
}
DEFAULT_PAGE = "home"
PAGE_DEFAULT_QUESTION = {"q1": "installs_peak", "q2": "installs_type", "q3": "delay_median",
                         "q4": "installs_month", "q5": "purchases_month", "q6": "installs_type"}
UNSUPPORTED_HINTS: dict[str, str] = {
    "territory": "installs_type", "region": "installs_type", "province": "installs_type",
    "revenue": "purchases_month", "price": "purchases_month", "cost": "purchases_month",
    "forecast": "installs_month", "predict": "installs_month",
    "ship": "delay_median", "livr": "delay_median", "expedi": "delay_median",
}


def page_id(page_key: str) -> str:
    """Normalise 'q1', 'q1_peaks_lows', 'views/q1_peaks_lows.py', ... to a PAGE_CHIPS key."""
    text = str(page_key or "").casefold().replace("\\", "/").rsplit("/", 1)[-1]
    for key in PAGE_CHIPS:
        if text == key or text.startswith(f"{key}_"):
            return key
    return DEFAULT_PAGE


def page_chip_keys(page_key: str) -> tuple[str, ...]:
    return PAGE_CHIPS[page_id(page_key)]

@dataclass(frozen=True)
class Interpretation:
    plan: QueryPlan | None = None
    analysis: Analysis | None = None
    intent: str = "list"  # list | high | low
    suggestions: tuple[str, ...] = ()  # chip keys of the closest supported questions
    reason: str = ""  # unsupported | ambiguous | unclear


_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
                 "eight": 8, "nine": 9, "ten": 10}
_HIGH = {"highest", "peak", "busiest", "most", "top", "largest", "longest", "slowest", "maximum",
         "best", "plus", "meilleur", "meilleurs", "max"}
_LOW = {"lowest", "fewest", "least", "quietest", "smallest", "shortest", "fastest", "worst",
        "moins", "minimum", "min", "creux"}


def _normalize(text: str) -> str:
    stripped = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in stripped if not unicodedata.combining(c))


def _has(words: set[str], *stems: str) -> bool:
    return any(word.startswith(stem) for word in words for stem in stems)


def _date_scope(text: str, words: set[str], reference: date | None) -> tuple[date, date] | None:
    year = next((int(y) for y in re.findall(r"\b((?:19|20)\d{2})\b", text)), None)
    base = (reference or date.today()).year
    if year is None and ("this year" in text or "cette annee" in text):
        year = base
    if year is None and ("last year" in text or "annee derniere" in text):
        year = base - 1
    return (date(year, 1, 1), date(year, 12, 31)) if year else None


def interpret_question(
    question: str,
    *,
    from_date: date | None = None,
    to_date: date | None = None,
    page_key: str = DEFAULT_PAGE,
    page_question: str = "",
    _depth: int = 0,
) -> Interpretation:
    """Map a free-text question onto the supported analyses with fixed keyword rules."""
    text = _normalize(question.strip()[:MAX_QUESTION_LENGTH])
    words = set(re.findall(r"[a-z]+", text))
    page_chips = page_chip_keys(page_key)

    for stem, chip in UNSUPPORTED_HINTS.items():
        if _has(words, stem):
            return Interpretation(suggestions=(chip, *page_chips[:1]), reason="unsupported")
    if _has(words, "custom", "client") and words & {"size", "taille"}:
        return Interpretation(suggestions=("installs_type",), reason="unsupported")

    pending = "pending" in words or _has(words, "uninstall", "awaiting", "backlog") or any(
        phrase in text for phrase in ("not installed", "not yet installed", "pas installe", "pas encore installe", "non installe", "en attente")
    )
    delay = bool(words & {"slowest", "longest", "slow", "takes", "lent", "plus long"}) or _has(
        words, "delay", "delai", "retard", "median", "mediane") or any(
        phrase in text for phrase in ("lead time", "how long", "time to install", "days to install", "days between", "de temps")
    )
    install = _has(words, "install") and not pending
    purchase = _has(words, "purchase", "achat", "vente", "commande") or bool(
        words & {"bought", "sales", "sold", "orders", "vendu", "vendus", "achete", "achetes"}
    )
    company = _has(words, "compan", "customer", "client", "entreprise")
    high = bool(words & _HIGH)
    low = bool(words & _LOW)
    intent = "low" if (low and not high) or "bas" in words else "high" if high else "list"

    metric: Metric | None = None
    if pending:
        metric = Metric.PENDING
    elif delay:
        metric = Metric.DELAY
    elif install and purchase:
        return Interpretation(suggestions=("installs_month", "purchases_month"), reason="ambiguous")
    elif install:
        metric = Metric.INSTALLATIONS
    elif purchase:
        metric = Metric.PURCHASES
    if metric is not None and company:
        return Interpretation(suggestions=("installs_type", "pending"), reason="unsupported")
    if metric is None:
        if company:
            return Interpretation(analysis=Analysis.COMPANIES)
        if _has(words, "locat", "coordinat", "map", "latitude", "longitude"):
            return Interpretation(analysis=Analysis.LOCATIONS)
        if _has(words, "overview", "summar", "total", "resume", "apercu", "devices") or "how many" in text:
            return Interpretation(analysis=Analysis.OVERVIEW)
        if _depth == 0 and page_question:
            inner = interpret_question(page_question, from_date=from_date, to_date=to_date,
                                       page_key=page_key, _depth=1)
            return replace(inner, intent=intent if intent != "list" else inner.intent)
        return Interpretation(suggestions=page_chips[:3], reason="unclear")

    group = GroupBy.NONE
    period = Period.MONTH
    if _has(words, "type", "product", "model", "produit", "modele") or (
        "which" in words and not words & {"month", "week", "day"} and "when" not in words and intent != "list"
    ):
        group = GroupBy.TYPE
    elif words & {"month", "months", "monthly", "mois", "mensuel", "mensuels", "mensuelle"}:
        group = GroupBy.PERIOD
    elif words & {"week", "weeks", "weekly", "semaine", "semaines", "hebdomadaire"}:
        group, period = GroupBy.PERIOD, Period.WEEK
    elif words & {"day", "days", "daily", "jour", "jours", "quotidien"} and metric != Metric.DELAY:
        group, period = GroupBy.PERIOD, Period.DAY
    elif "when" in words or "quand" in words or (intent != "list" and metric in (Metric.INSTALLATIONS, Metric.PURCHASES)):
        group = GroupBy.PERIOD
    if group == GroupBy.TYPE and metric == Metric.PENDING:
        return Interpretation(suggestions=("pending", "installs_type"), reason="unsupported")

    top_n = MAX_RESULT_ROWS
    match = re.search(r"\btop\s+(\d+)\b", text)
    if match:
        top_n = int(match.group(1))
    elif "top" in words:
        top_n = next((_NUMBER_WORDS[w] for w in words if w in _NUMBER_WORDS), top_n)
    top_n = max(1, min(top_n, MAX_RESULT_ROWS))

    scope = _date_scope(text, words, to_date) or (
        (from_date, to_date) if from_date and to_date and from_date <= to_date else None
    )
    date_field = DateField.NONE
    if scope:
        date_field = DateField.PURCHASE if metric in (Metric.PURCHASES, Metric.PENDING) else DateField.INSTALL
    plan = validate_plan({
        "metric": metric.value, "group_by": group.value, "period": period.value,
        "date_field": date_field.value,
        "start_date": scope[0].isoformat() if scope else None,
        "end_date": scope[1].isoformat() if scope else None,
        "order": Order.ASC.value if group == GroupBy.TYPE and intent == "low" else Order.DESC.value
        if group == GroupBy.TYPE else Order.ASC.value,
        "top_n": top_n,
    })
    return Interpretation(plan=plan, intent=intent)



@dataclass
class Answer:
    """One displayed assistant reply. Held in the session only; never sent anywhere."""

    kind: str  # result | table | unsupported | error | proposal
    title: str = ""
    plan: QueryPlan | None = None
    result: QueryResult | None = None
    table: pd.DataFrame | None = None
    intent: str = "list"
    suggestions: tuple[str, ...] = ()
    reason: str = ""
    note: str = ""
    question: str = ""  # kept for model-proposed plans only (shown back to the user)
    scope: tuple[date | None, date | None] = (None, None)
    explanation: str = ""
    done: bool = False  # a proposal was run / an AI interpretation was requested


MAX_ANALYSIS_GROUPS = 100_000  # safety net on grouped results, independent of source row count
MAX_TABLE_ROWS = 5_000  # rows shown for per-value tables (rows by type/company, delay by type)


def _require_fields(fields: dict[str, str], *aliases: str) -> None:
    missing = set(aliases) - set(fields)
    if missing:
        raise ValueError(f"Required mapped fields are missing: {', '.join(sorted(missing))}")


def _capped(table: pd.DataFrame, shown: int) -> pd.DataFrame:
    note = table.attrs.get("note", "")
    table = table.head(shown).reset_index(drop=True)
    table.attrs["note"] = f"{note} Showing the first {shown:,} rows.".strip()
    return table


def aggregate_analysis(
    engine: Any, config: AppConfig, fields: dict[str, str], analysis: Analysis,
    from_date: date | None, to_date: date | None,
) -> pd.DataFrame:
    """Same tables as execute_analysis, computed by SQL GROUP BY/aggregates (no source rows loaded)."""
    if analysis not in ANALYSIS_LABELS:
        raise ValueError("Unsupported analysis. Select one of the predefined analyses.")
    filtering = bool(from_date and to_date and "purchase_date" in fields)
    scope = {
        "scope_attr": fields["purchase_date"] if filtering else None,
        "from_date": from_date if filtering else None, "to_date": to_date if filtering else None,
    }

    def groups(mode: str, grouping: str = "none", *, event: str | None = None, group: str | None = None,
               order: str = "group", limit: int = MAX_ANALYSIS_GROUPS + 1) -> pd.DataFrame:
        frame = fetch_chat_groups(
            engine, config, mode, grouping, event_attr=fields[event] if event else None,
            group_attr=fields[group] if group else None, order=order, limit=limit, **scope,
        )
        if len(frame) > MAX_ANALYSIS_GROUPS and order == "group":
            raise ValueError(
                f"The analysis produces more than {MAX_ANALYSIS_GROUPS:,} groups. "
                "Use a different date range."
            )
        return frame

    if analysis == Analysis.OVERVIEW:
        distinct = {alias: fields[field] for field, alias in (
            ("device_id", "device_id"), ("company_id", "company_id")) if field in fields}
        profile = fetch_chat_row_profile(engine, config, distinct_attrs=distinct, **scope)
        metrics = {"Rows": profile["row_count"]}
        for field, label in (("device_id", "Distinct device IDs"), ("company_id", "Distinct companies")):
            if field in fields:
                metrics[label] = profile[f"distinct_{field}"]
        return pd.DataFrame(metrics.items(), columns=["Metric", "Count"])
    if analysis in {Analysis.PURCHASES, Analysis.INSTALLS}:
        field = "purchase_date" if analysis == Analysis.PURCHASES else "install_date"
        _require_fields(fields, field)
        counts = groups("events", "period", event=field)
        total = fetch_chat_row_profile(engine, config, **scope)["row_count"]
        result = pd.DataFrame({"Month": counts["group"].str[:7], "Rows with date": counts["count"]})
        result.attrs["note"] = f"Rows missing a valid date: {total - int(counts['count'].sum()):,}."
        return result
    if analysis in {Analysis.TYPES, Analysis.COMPANIES}:
        field = "device_type" if analysis == Analysis.TYPES else "company_id"
        _require_fields(fields, field)
        counts = groups("rows", "value", group=field, order="count", limit=MAX_TABLE_ROWS + 1)
        result = pd.DataFrame({field: counts["group"], "Rows": counts["count"]})
        return _capped(result, MAX_TABLE_ROWS) if len(result) > MAX_TABLE_ROWS else result
    if analysis == Analysis.PENDING:
        _require_fields(fields, "purchase_date", "install_date")
        profile = fetch_chat_row_profile(engine, config, pending=True, **scope)
        return pd.DataFrame({"Metric": ["Purchased rows without a valid install date"],
                             "Count": [profile["pending"]]})
    if analysis in {Analysis.DELAY, Analysis.DELAY_BY_TYPE}:
        _require_fields(fields, "purchase_date", "install_date")
        profile = fetch_chat_row_profile(engine, config, delay=True, **scope)
        if analysis == Analysis.DELAY_BY_TYPE:
            _require_fields(fields, "device_type")
            stats = groups("delay", "value", event="install_date", group="device_type")
            result = pd.DataFrame({
                "Device type": stats["group"], "Count": stats["count"], "Median": stats["median_days"],
                "Mean": stats["mean_days"], "Min": stats["min_days"], "Max": stats["max_days"],
            }).round(2).sort_values("Median", ascending=False, kind="stable").reset_index(drop=True)
        else:
            stats = groups("delay", "none", event="install_date")
            row = stats.iloc[0] if len(stats) else None
            result = pd.DataFrame({
                "Valid paired rows": [int(row["count"]) if row is not None else 0],
                "Mean days": [row["mean_days"] if row is not None else float("nan")],
                "Median days": [row["median_days"] if row is not None else float("nan")],
                "Min days": [row["min_days"] if row is not None else float("nan")],
                "Max days": [row["max_days"] if row is not None else float("nan")],
            }).round(2)
        result.attrs["note"] = (
            f"Excluded {profile['row_count'] - profile['paired']:,} rows with missing dates and "
            f"{profile['negative_delay']:,} rows with negative delay."
        )
        return _capped(result, MAX_TABLE_ROWS) if len(result) > MAX_TABLE_ROWS else result
    _require_fields(fields, "latitude", "longitude")
    profile = fetch_chat_row_profile(engine, config, coordinates=True, **scope)
    valid = profile["valid_coordinates"]
    return pd.DataFrame({
        "Metric": ["Rows with valid coordinates", "Rows with missing or invalid coordinates"],
        "Count": [valid, profile["row_count"] - valid],
    })


def run_analysis_locally(
    engine: Any, config: AppConfig, columns: tuple[str, ...] | set[str], analysis: Analysis,
    from_date: date | None, to_date: date | None,
) -> Answer:
    fields = available_chat_fields(config, set(columns))
    if not fields:
        raise ValueError("No mapped analysis columns were found.")
    filtering = bool(from_date and to_date and "purchase_date" in fields)
    table = aggregate_analysis(engine, config, fields, analysis, from_date, to_date)
    return Answer("table", ANALYSIS_LABELS[analysis], table=table, note=table.attrs.get("note", ""),
                  scope=(from_date, to_date) if filtering else (None, None))