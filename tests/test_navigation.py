"""Navigation structure, entry point and translation coverage of the views."""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

from insightquest.i18n import load_catalog
from insightquest.page_helpers import QUESTIONS, QuestionSpec

ROOT = Path(__file__).resolve().parents[1]
VIEWS = ROOT / "views"
SOURCES = [*sorted(VIEWS.glob("*.py")), ROOT / "dashboard.py", ROOT / "insightquest" / "page_helpers.py"]
LITERAL_KEY = re.compile(r"""\bt\(\s*f?["']([a-z0-9_.]+)["']""")


def calls(path: Path) -> list[str]:
    names = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                names.append(f"{func.value.id}.{func.attr}")
    return names


class NavigationTests(unittest.TestCase):
    def test_dashboard_is_a_thin_entry_with_one_page_config_and_navigation(self) -> None:
        names = calls(ROOT / "dashboard.py")
        self.assertEqual(names.count("st.set_page_config"), 1)
        self.assertIn("st.navigation", names)
        self.assertLess(len((ROOT / "dashboard.py").read_text(encoding="utf-8").splitlines()), 60)

    def test_legacy_pages_directory_is_gone_so_discovery_cannot_duplicate_pages(self) -> None:
        self.assertFalse((ROOT / "pages").exists())

    def test_every_question_has_a_view_file(self) -> None:
        self.assertEqual(sorted(QUESTIONS), [1, 2, 3, 4, 5, 6])
        for spec in QUESTIONS.values():
            self.assertIsInstance(spec, QuestionSpec)
            self.assertTrue((ROOT / spec.page).is_file(), spec.page)
        for extra in ("views/home.py", "views/overview.py", "views/ask_the_data.py"):
            self.assertTrue((ROOT / extra).is_file(), extra)

    def test_dashboard_registers_every_view(self) -> None:
        source = (ROOT / "insightquest" / "navigation.py").read_text(encoding="utf-8")
        for path in ("views/home.py", "views/overview.py", "views/ask_the_data.py"):
            self.assertIn(path, source)
        self.assertIn("spec.page", source)
        self.assertNotIn("Q{spec.number} ·", source)

    def test_page_names_are_short_plain_language_in_both_languages(self) -> None:
        for language in ("en", "fr_CA"):
            catalog = load_catalog(language)
            for key in [f"page.q{n}" for n in range(1, 7)] + ["page.overview", "page.ask_the_data"]:
                name = catalog[key]
                self.assertLessEqual(len(name.split()), 4, name)
                self.assertFalse(re.match(r"Q\d", name), name)

    def test_url_paths_are_readable_slugs(self) -> None:
        from insightquest.navigation import CHAT_PAGE_IDS, QUESTION_SLUGS

        self.assertEqual(
            list(QUESTION_SLUGS.values()),
            ["peaks-and-lows", "market-gaps", "ship-to-install", "install-rate", "buying-outlook", "stock-watch"],
        )
        self.assertEqual(CHAT_PAGE_IDS["install-rate"], "q4")
        source = (ROOT / "insightquest" / "navigation.py").read_text(encoding="utf-8")
        for slug in ("install-map", '"ask"', '"home"'):
            self.assertIn(slug, source)

    def test_streamlit_config_is_dark_and_shipped_in_docker(self) -> None:
        import tomllib

        config = tomllib.loads((ROOT / ".streamlit" / "config.toml").read_text(encoding="utf-8"))
        self.assertEqual(config["theme"]["base"], "dark")
        for key in ("primaryColor", "backgroundColor", "secondaryBackgroundColor", "textColor"):
            self.assertRegex(config["theme"][key], r"^#[0-9A-Fa-f]{6}$")
        ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8").split()
        self.assertNotIn(".streamlit", ignored)
        self.assertNotIn(".streamlit/", ignored)
        self.assertTrue((ROOT / "assets" / "insightquest-logo-dark.png").is_file())

    def test_views_never_configure_the_page_or_show_stack_traces(self) -> None:
        for path in VIEWS.glob("*.py"):
            self.assertNotIn("st.set_page_config", calls(path), path.name)
            self.assertNotIn("st.exception", calls(path), path.name)

    def test_docker_entry_still_runs_dashboard(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("ARG APP_ENTRY=dashboard.py", dockerfile)
        self.assertIn('streamlit run \\"$APP_ENTRY\\"', dockerfile)

    def test_question_views_stay_small(self) -> None:
        for spec in QUESTIONS.values():
            lines = (ROOT / spec.page).read_text(encoding="utf-8").splitlines()
            self.assertLess(len(lines), 150, spec.page)


class TranslationCoverageTests(unittest.TestCase):
    def test_every_literal_translation_key_exists_in_both_languages(self) -> None:
        english, french = load_catalog("en"), load_catalog("fr_CA")
        for path in SOURCES:
            for key in LITERAL_KEY.findall(path.read_text(encoding="utf-8")):
                if key.count(".") < 1 or key in {"installs", "purchases"}:
                    continue
                self.assertIn(key, english, f"{path.name}: {key}")
                self.assertIn(key, french, f"{path.name}: {key}")

    def test_dynamic_key_families_are_complete(self) -> None:
        english = load_catalog("en")
        families = {
            "period": ("day", "week", "month"),
            "badge": ("ready", "partial", "unavailable"),
            "label": ("high", "medium", "low", "insufficient"),
            "growth": ("growing", "declining", "stable", "new", "inactive"),
            "trend": ("up", "flat", "down"),
            "status": ("stockout_risk", "reorder", "healthy", "overstock", "no_demand"),
            "q6.rule": ("stockout_risk", "reorder", "healthy", "overstock", "no_demand"),
            "question": tuple(f"{n}.title" for n in range(1, 7)) + tuple(f"{n}.desc" for n in range(1, 7)),
            "inventory": ("lead_time_days_help", "safety_days_help", "max_cover_days_help", "demand_window_days_help"),
        }
        for prefix, names in families.items():
            for name in names:
                self.assertIn(f"{prefix}.{name}", english)
        for attribute in {a for spec in QUESTIONS.values() for a in spec.required}:
            self.assertIn(f"field.{attribute.removeprefix('col_')}", english)


if __name__ == "__main__":
    unittest.main()
