from __future__ import annotations

import re
from io import BytesIO

import pandas as pd
import streamlit as st
from openpyxl.cell.cell import Cell

_INVALID_SHEET_CHARACTERS = re.compile(r"[\[\]:*?/\\]")
_FORMULA_PREFIX = re.compile(r"^[\t\r\n ]*[=+\-@]")


def build_excel_workbook(sheets: dict[str, pd.DataFrame]) -> bytes:
    if not sheets:
        raise ValueError("At least one worksheet is required.")

    output = BytesIO()
    used_names: set[str] = set()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        for requested_name, data in sheets.items():
            base_name = _INVALID_SHEET_CHARACTERS.sub("_", requested_name).strip("'")
            base_name = (base_name or "Results")[:31]
            sheet_name = base_name
            suffix = 2
            while sheet_name.casefold() in used_names:
                suffix_text = f" ({suffix})"
                sheet_name = f"{base_name[:31 - len(suffix_text)]}{suffix_text}"
                suffix += 1
            used_names.add(sheet_name.casefold())

            frame = data.copy()
            frame = frame.replace([float("inf"), float("-inf")], None)
            for column in frame.select_dtypes(include=["datetimetz"]).columns:
                frame[column] = frame[column].dt.tz_convert("UTC").dt.tz_localize(None)

            frame.to_excel(writer, sheet_name=sheet_name, index=False)
            worksheet = writer.sheets[sheet_name]
            for row in worksheet.iter_rows(min_row=2):
                for cell in row:
                    _preserve_string_cell(cell)
    return output.getvalue()


def _preserve_string_cell(cell: Cell) -> None:
    if isinstance(cell.value, str) and _FORMULA_PREFIX.match(cell.value):
        cell.data_type = "s"


def render_excel_download(
    sheets: dict[str, pd.DataFrame],
    filename: str,
    label: str = "Export results to Excel",
) -> None:
    if not sheets:
        return
    st.download_button(
        label,
        data=build_excel_workbook(sheets),
        file_name=filename,
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key=f"excel-export-{filename}",
    )
