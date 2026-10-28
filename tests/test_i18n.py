from __future__ import annotations

import re
from datetime import date, datetime

import pytest
import streamlit as st

from insightquest import i18n

_PLACEHOLDER = re.compile(r"\{(\w+)\}")


@pytest.fixture(autouse=True)
def _reset_language():
    st.session_state.pop(i18n.LANGUAGE_STATE_KEY, None)
    yield
    st.session_state.pop(i18n.LANGUAGE_STATE_KEY, None)


def test_catalogs_have_identical_keys_and_placeholders():
    english = i18n.load_catalog("en")
    french = i18n.load_catalog("fr_CA")

    assert set(english) == set(french)
    for key, text in english.items():
        assert set(_PLACEHOLDER.findall(text)) == set(_PLACEHOLDER.findall(french[key])), key
        if key in _INTENTIONALLY_EMPTY:
            assert not text.strip() and not french[key].strip(), key
        else:
            assert text.strip() and french[key].strip(), key


# No grouping means no "by ..." text in the chat scope line.
_INTENTIONALLY_EMPTY = {"chat.by.none"}


def test_french_original_question_titles_are_preserved():
    st.session_state[i18n.LANGUAGE_STATE_KEY] = "fr_CA"
    assert i18n.t("question.1.title") == "Pics et creux des achats et installations"
    assert i18n.t("question.4.title") == "Taux mensuel d'installation"
    assert i18n.t("question.6.title") == "Seuils critiques pour risques de rupture ou de surstock"


def test_default_language_is_english_and_switches():
    assert i18n.t("sidebar.from_date") == "From date"
    i18n.set_language("fr-CA")
    assert st.session_state[i18n.LANGUAGE_STATE_KEY] == "fr_CA"
    assert i18n.t("sidebar.from_date") == "Date de début"


def test_unknown_language_and_key_fall_back():
    st.session_state[i18n.LANGUAGE_STATE_KEY] = "de"
    assert i18n.get_language() == "en"
    assert i18n.t("does.not.exist") == "does.not.exist"


def test_formatting_and_missing_placeholders():
    assert i18n.t("state.row_limit_reached", limit="5 000").startswith("Row limit reached (5 000 rows)")
    assert "{limit}" in i18n.t("state.row_limit_reached")
    assert i18n.t("export.csv", table="Sales") == "Export Sales to CSV"


def test_number_and_date_formatting():
    assert i18n.format_number(1234.5, 1, "en") == "1,234.5"
    assert i18n.format_number(1234.5, 1, "fr_CA") == "1\u00a0234,5"
    assert i18n.format_int(1234567, "fr_CA") == "1\u00a0234\u00a0567"
    assert i18n.format_percent(0.256, 1, "fr_CA") == "25,6\u00a0%"
    assert i18n.format_number(None) == ""
    assert i18n.format_number(float("nan")) == ""
    assert i18n.format_date(date(2026, 3, 4), "fr_CA") == "2026-03-04"
    assert i18n.format_datetime(datetime(2026, 3, 4, 5, 6), "en") == "2026-03-04 05:06"


def test_locale_files_ship_with_docker_context():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    ignored = (root / ".dockerignore").read_text(encoding="utf-8").split()
    assert "insightquest" not in ignored and "*.json" not in ignored
    assert (root / "insightquest" / "locales" / "fr_CA.json").is_file()
