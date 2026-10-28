"""Lightweight internationalisation (English / Canadian French) for the Streamlit UI."""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import streamlit as st

LANGUAGE_STATE_KEY = "iq_lang"
_SELECTOR_WIDGET_KEY = "iq_lang_selector"
DEFAULT_LANGUAGE = "en"
SUPPORTED_LANGUAGES: dict[str, str] = {
    "en": "English",
    "fr_CA": "Français (Canada)",
}

_LOCALES_DIR = Path(__file__).with_name("locales")
_PLACEHOLDER = re.compile(r"\{(\w+)\}")
_NBSP = "\u00a0"


@lru_cache(maxsize=None)
def load_catalog(language: str) -> dict[str, str]:
    path = _LOCALES_DIR / f"{language}.json"
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def normalize_language(value: object) -> str:
    """Map values such as 'fr', 'fr-CA' or 'FR_ca' to a supported code."""
    text = str(value or "").strip().replace("-", "_").casefold()
    for code in SUPPORTED_LANGUAGES:
        if text == code.casefold():
            return code
    if text.startswith("fr"):
        return "fr_CA"
    return DEFAULT_LANGUAGE


def get_language() -> str:
    try:
        value = st.session_state.get(LANGUAGE_STATE_KEY, DEFAULT_LANGUAGE)
    except Exception:  # no Streamlit session (scripts, tests)
        value = DEFAULT_LANGUAGE
    return normalize_language(value)


def set_language(language: str) -> None:
    st.session_state[LANGUAGE_STATE_KEY] = normalize_language(language)


def _interpolate(template: str, values: dict[str, Any]) -> str:
    if not values:
        return template
    return _PLACEHOLDER.sub(
        lambda match: str(values[match.group(1)]) if match.group(1) in values else match.group(0),
        template,
    )


def translate(key: str, language: str | None = None, **fmt: Any) -> str:
    """Translate ``key`` for ``language`` (default: current), falling back to English, then the key."""
    code = normalize_language(language) if language else get_language()
    for candidate in (code, DEFAULT_LANGUAGE):
        try:
            template = load_catalog(candidate).get(key)
        except (OSError, ValueError):
            template = None
        if template is not None:
            return _interpolate(template, fmt)
    return key


def t(key: str, **fmt: Any) -> str:
    """Translate ``key`` using the language in ``st.session_state['iq_lang']``."""
    return translate(key, None, **fmt)


def render_language_selector(container: Any | None = None) -> str:
    """Render the language picker (sidebar by default) and return the active language code."""
    target = container if container is not None else st.sidebar
    codes = list(SUPPORTED_LANGUAGES)
    current = get_language()

    def _on_change() -> None:
        set_language(st.session_state[_SELECTOR_WIDGET_KEY])

    # The widget key is dropped by Streamlit when a page does not render it, so the
    # chosen language lives under a separate, persistent key.
    target.selectbox(
        t("app.language"),
        codes,
        index=codes.index(current),
        format_func=lambda code: SUPPORTED_LANGUAGES[code],
        key=_SELECTOR_WIDGET_KEY,
        on_change=_on_change,
    )
    return get_language()


def format_number(value: float | int | None, decimals: int = 1, language: str | None = None) -> str:
    """Format a number per locale: en '1,234.5'; fr_CA '1 234,5' (no-break space)."""
    if value is None or value != value:
        return ""
    code = normalize_language(language) if language else get_language()
    text = f"{float(value):,.{max(decimals, 0)}f}"
    if code == "fr_CA":
        text = text.replace(",", _NBSP).replace(".", ",")
    return text


def format_int(value: float | int | None, language: str | None = None) -> str:
    return format_number(value, 0, language)


def format_percent(value: float | None, decimals: int = 1, language: str | None = None) -> str:
    """Format a ratio (0.256 -> '25.6 %' in fr_CA with a no-break space, '25.6%' in en)."""
    if value is None or value != value:
        return ""
    code = normalize_language(language) if language else get_language()
    number = format_number(float(value) * 100, decimals, code)
    return f"{number}{_NBSP}%" if code == "fr_CA" else f"{number}%"


def format_date(value: date | datetime | str | None, language: str | None = None) -> str:
    """Format a date as YYYY-MM-DD (ISO, unambiguous in both languages)."""
    if value is None or value == "":
        return ""
    if isinstance(value, str):
        return value[:10]
    try:
        return value.strftime("%Y-%m-%d")
    except (AttributeError, ValueError):
        return str(value)


def format_datetime(value: datetime | str | None, language: str | None = None) -> str:
    """Format a timestamp as 'YYYY-MM-DD HH:MM'."""
    if value is None or value == "":
        return ""
    if isinstance(value, str):
        return value
    try:
        return value.strftime("%Y-%m-%d %H:%M")
    except (AttributeError, ValueError):
        return str(value)
