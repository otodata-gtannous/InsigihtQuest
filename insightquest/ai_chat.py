from __future__ import annotations

import json
import re
from enum import StrEnum

import pandas as pd

from insightquest.config import AppConfig

CHAT_FIELDS = {
    "device_id": "col_device",
    "purchase_date": "col_sales_date",
    "device_type": "col_product_type",
    "install_date": "col_installed_at",
    "company_id": "col_customer",
    "latitude": "col_latitude",
    "longitude": "col_longitude",
}


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
