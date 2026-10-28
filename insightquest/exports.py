from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from datetime import date, datetime, timezone
from io import BytesIO
from typing import Any

import pandas as pd
import streamlit as st
from openpyxl.cell.cell import Cell

from insightquest.i18n import format_date, t, translate

_INVALID_SHEET_CHARACTERS = re.compile(r"[\[\]:*?/\\]")
_FORMULA_PREFIX = re.compile(r"^[\t\r\n ]*[=+\-@]")
_EXCEL_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def slugify(text: str) -> str:
    """ASCII, lowercase, underscore-separated slug for file names."""
    ascii_text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", ascii_text.casefold()).strip("_") or "export"


def build_export_filename(
    page: str,
    extension: str = "xlsx",
    *,
    table: str | None = None,
    today: date | datetime | None = None,
) -> str:
    """Return ``insightquest_<page>[_<table>]_<YYYYMMDD>.<extension>`` (UTC date by default)."""
    stamp = (today or datetime.now(timezone.utc)).strftime("%Y%m%d")
    parts = ["insightquest", slugify(page)]
    if table:
        parts.append(slugify(table))
    parts.append(stamp)
    return f"{'_'.join(parts)}.{extension.lstrip('.')}"


def _unique_sheet_name(requested_name: str, used_names: set[str]) -> str:
    base_name = _INVALID_SHEET_CHARACTERS.sub("_", requested_name).strip("'")
    base_name = (base_name or "Results")[:31]
    sheet_name = base_name
    suffix = 2
    while sheet_name.casefold() in used_names:
        suffix_text = f" ({suffix})"
        sheet_name = f"{base_name[:31 - len(suffix_text)]}{suffix_text}"
        suffix += 1
    used_names.add(sheet_name.casefold())
    return sheet_name


def _clean_frame(data: pd.DataFrame) -> pd.DataFrame:
    frame = data.copy()
    frame = frame.replace([float("inf"), float("-inf")], None)
    for column in frame.select_dtypes(include=["datetimetz"]).columns:
        frame[column] = frame[column].dt.tz_convert("UTC").dt.tz_localize(None)
    return frame


def _stringify(value: Any, language: str | None) -> str:
    if value is None:
        return translate("export.none", language)
    if isinstance(value, (list, tuple, set, frozenset)):
        items = sorted(value, key=str) if isinstance(value, (set, frozenset)) else value
        return ", ".join(_stringify(item, language) for item in items) or translate("export.none", language)
    if isinstance(value, (date, datetime)):
        return format_date(value, language)
    return str(value)


def _filters_frame(
    sheets: Mapping[str, pd.DataFrame],
    page: str | None,
    filters: Mapping[str, Any] | None,
    date_range: tuple[Any, Any] | None,
    generated_at: datetime | None,
    row_count: int | None,
    proxy_label: str | None,
    language: str | None,
) -> pd.DataFrame:
    moment = generated_at or datetime.now(timezone.utc)
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc)
    total_rows = row_count if row_count is not None else sum(len(frame) for frame in sheets.values())

    rows: list[tuple[str, str]] = []
    if page:
        rows.append((translate("export.page", language), str(page)))
    for name, value in (filters or {}).items():
        rows.append((str(name), _stringify(value, language)))
    if date_range:
        start, end = date_range
        rows.append((
            translate("export.date_range", language),
            f"{format_date(start, language)} – {format_date(end, language)}",
        ))
    rows.append((translate("export.generated_at", language), moment.strftime("%Y-%m-%d %H:%M:%S")))
    rows.append((translate("export.row_count", language), str(total_rows)))
    if proxy_label:
        rows.append((translate("export.proxy_label", language), proxy_label))
    return pd.DataFrame(rows, columns=[translate("export.field", language), translate("export.value", language)])


def build_excel_workbook(
    sheets: dict[str, pd.DataFrame],
    *,
    page: str | None = None,
    filters: Mapping[str, Any] | None = None,
    date_range: tuple[Any, Any] | None = None,
    generated_at: datetime | None = None,
    row_count: int | None = None,
    proxy_label: str | None = None,
    language: str | None = None,
) -> bytes:
    """Build an xlsx workbook. Passing any of the keyword arguments appends a 'Filters' sheet.

    ``generated_at`` defaults to now (UTC) and ``row_count`` to the total rows of ``sheets``.
    """
    if not sheets:
        raise ValueError("At least one worksheet is required.")

    stamp = any(value is not None for value in (page, filters, date_range, generated_at, row_count, proxy_label))
    used_names: set[str] = set()
    stamp_name = ""
    if stamp:
        stamp_name = _unique_sheet_name(translate("export.filters_sheet", language), used_names)

    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        for requested_name, data in sheets.items():
            sheet_name = _unique_sheet_name(requested_name, used_names)
            _clean_frame(data).to_excel(writer, sheet_name=sheet_name, index=False)
            _protect_formulas(writer.sheets[sheet_name])

        if stamp:
            frame = _filters_frame(
                sheets, page, filters, date_range, generated_at, row_count, proxy_label, language
            )
            frame.to_excel(writer, sheet_name=stamp_name, index=False)
            worksheet = writer.sheets[stamp_name]
            _protect_formulas(worksheet)
            worksheet.column_dimensions["A"].width = 24
            worksheet.column_dimensions["B"].width = 60
    return output.getvalue()


def _protect_formulas(worksheet: Any) -> None:
    for row in worksheet.iter_rows(min_row=2):
        for cell in row:
            _preserve_string_cell(cell)


def _preserve_string_cell(cell: Cell) -> None:
    if isinstance(cell.value, str) and _FORMULA_PREFIX.match(cell.value):
        cell.data_type = "s"


def build_csv(data: pd.DataFrame) -> bytes:
    """UTF-8 (with BOM so Excel reads accents) CSV; formula-like text cells are prefixed with an apostrophe."""
    frame = _clean_frame(data)
    for column in frame.select_dtypes(include=["object", "string"]).columns:
        frame[column] = frame[column].map(
            lambda value: f"'{value}" if isinstance(value, str) and _FORMULA_PREFIX.match(value) else value
        )
    return frame.to_csv(index=False).encode("utf-8-sig")


def render_excel_download(
    sheets: dict[str, pd.DataFrame],
    filename: str,
    label: str | None = None,
    *,
    key: str | None = None,
    **workbook_options: Any,
) -> None:
    """Download button for an Excel workbook; ``workbook_options`` go to :func:`build_excel_workbook`."""
    if not sheets:
        return
    st.download_button(
        label or t("export.excel"),
        data=build_excel_workbook(sheets, **workbook_options),
        file_name=filename,
        mime=_EXCEL_MIME,
        key=key or f"excel-export-{filename}",
    )


def render_csv_download(
    data: pd.DataFrame,
    filename: str,
    label: str | None = None,
    *,
    table_name: str | None = None,
    key: str | None = None,
) -> None:
    """Download button for one table as CSV."""
    if data is None or data.empty:
        return
    if label is None:
        label = t("export.csv", table=table_name) if table_name else t("export.csv_default")
    st.download_button(
        label,
        data=build_csv(data),
        file_name=filename,
        mime="text/csv",
        key=key or f"csv-export-{filename}",
    )


def render_export_buttons(
    sheets: dict[str, pd.DataFrame],
    page: str,
    **workbook_options: Any,
) -> None:
    """Excel (named ``insightquest_<page>_<YYYYMMDD>.xlsx``, with Filters sheet) plus one CSV per table."""
    if not sheets:
        return
    workbook_options.setdefault("page", page)
    render_excel_download(sheets, build_export_filename(page, "xlsx"), **workbook_options)
    for name, frame in sheets.items():
        render_csv_download(
            frame,
            build_export_filename(page, "csv", table=name),
            table_name=name,
            key=f"csv-export-{slugify(page)}-{slugify(name)}",
        )
