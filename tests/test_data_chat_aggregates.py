from __future__ import annotations

import itertools
from contextlib import nullcontext
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd

from insightquest.ai_chat import (
    ANALYSIS_LABELS,
    MAX_TABLE_ROWS,
    Analysis,
    aggregate_analysis,
    available_chat_fields,
    execute_analysis,
    run_analysis_locally,
)
from insightquest.ai_query import (
    Metric,
    aggregate_payload,
    aggregate_query,
    execute_query,
    validate_plan,
)
from insightquest.config import get_config
from insightquest.data import (
    build_chat_group_query,
    build_chat_row_profile_query,
    fetch_chat_groups,
    fetch_chat_row_profile,
    fetch_flat_table_columns,
    fetch_flat_table_rows,
)

CSV = Path(__file__).resolve().parents[1] / "demo_data.csv"
OLD_FIELDS = {
    "purchase_date": "col_sales_date", "install_date": "col_installed_at", "device_type": "col_product_type",
}


def demo_config(**changes: object):
    values: dict[str, object] = {
        "demo_mode": True, "demo_csv_path": CSV, "col_device": "Id", "col_customer": "CompanyId",
        "col_installed_at": "TankInstallDate", "col_sales_date": "PurchaseDate",
        "col_product_type": "DeviceType", "col_latitude": "Latitude", "col_longitude": "Longitude",
        "openai_api_key": "test-not-a-real-key", "ai_allow_external_data": True,
    }
    return replace(get_config(), **{**values, **changes})


def plan_for(**changes: object):
    values: dict[str, object] = {
        "metric": "installations", "group_by": "none", "period": "month", "date_field": "none",
        "start_date": None, "end_date": None, "order": "asc", "top_n": 50,
    }
    values.update(changes)
    return validate_plan(values)


def old_plan_result(config, plan):
    """The former implementation: load rows (no cap), aggregate in pandas."""
    fields = {"purchase_date"} if plan.metric == Metric.PURCHASES else {"install_date"}
    if plan.metric in {Metric.PENDING, Metric.DELAY}:
        fields.update(("purchase_date", "install_date"))
    if plan.group_by.value == "device_type":
        fields.add("device_type")
    if plan.date_field.value != "none":
        fields.add(plan.date_field.value)
    data = fetch_flat_table_rows(
        None, config, {f: OLD_FIELDS[f] for f in sorted(fields)},
        date_field=OLD_FIELDS[plan.date_field.value] if plan.date_field.value != "none" else None,
        from_date=plan.start_date, to_date=plan.end_date, limit=10**9,
    )
    return aggregate_query(data, plan)


def patched(source):
    """Serve `source(config)` as the demo CSV, or leave the real demo CSV in place."""
    return nullcontext() if source is None else patch("insightquest.data._read_demo_csv", side_effect=source)


def edge_frame() -> pd.DataFrame:
    """Invalid/missing dates, negative and fractional delays, ties, missing/blank types, 60 days."""
    rows = [
        ("2026-01-01 00:00:00", "2026-01-03 12:00:00", "A"), ("2026-01-01", "2026-01-05", "A"),
        ("2026-01-05", "2026-01-04", "A"), ("2026-01-02", None, "B"), (None, "2026-01-03", "B"),
        ("not a date", "2026-01-03", "B"), ("2026-01-09", "also bad", "C"),
        ("2026-01-10 08:00:00", "2026-01-10 08:00:00", "C"), ("2026-02-02", "2026-02-09", None),
        ("2026-02-03", "2026-02-03 18:00:00", ""), ("2026-02-03", "2026-02-20", "a"),
        ("2026-03-30", "2026-04-01", "D"), ("2026-03-31 23:59:59", "2026-04-02", "D"),
    ]
    days = pd.date_range("2025-06-01", periods=60)
    rows += [(d.strftime("%Y-%m-%d"), (d + pd.Timedelta(days=int(i % 4))).strftime("%Y-%m-%d"), f"T{i % 7}")
             for i, d in enumerate(days)] * 2
    frame = pd.DataFrame(rows, columns=["PurchaseDate", "TankInstallDate", "DeviceType"])
    for column in ("PurchaseDate", "TankInstallDate"):  # one timestamp format so every valid value parses
        frame[column] = frame[column].map(lambda s: s + " 00:00:00" if isinstance(s, str) and len(s) == 10 else s)
    frame["Id"] = [f"D{i}" for i in range(len(frame))]
    frame["CompanyId"] = [f"C{i % 5}" if i % 9 else None for i in range(len(frame))]
    frame["Latitude"] = [45.0 if i % 6 else 95.0 for i in range(len(frame))]
    frame["Longitude"] = [-70.0 if i % 5 else None for i in range(len(frame))]
    return frame


SCOPES = (
    ("none", None, None),
    ("purchase_date", "2023-03-01", "2023-06-30"),
    ("install_date", "2023-02-01", "2023-05-31"),
)
EDGE_SCOPES = (("none", None, None), ("purchase_date", "2026-01-01", "2026-02-03"),
               ("install_date", "2026-01-03", "2026-04-01"))


class PlanParityTests(unittest.TestCase):
    """execute_query (SQL-side aggregates; pandas reference in demo mode) == the former row-based result."""

    def check(self, config, scopes, source=None) -> None:
        shapes = [("none", "month")] + [("period", p) for p in ("day", "week", "month")] + [("device_type", "month")]
        metrics = ("purchases", "installations", "pending_installations", "purchase_to_install_delay")
        for metric, (group, period), (field, start, end), order, top in itertools.product(
            metrics, shapes, scopes, ("asc", "desc"), (1, 50),
        ):
            plan = plan_for(metric=metric, group_by=group, period=period, date_field=field,
                            start_date=start, end_date=end, order=order, top_n=top)
            with self.subTest(plan=plan.payload()):
                with patched(source):
                    old = old_plan_result(config, plan)
                    new = execute_query(None, config, plan)
                pd.testing.assert_frame_equal(new.table, old.table, check_dtype=False)
                self.assertEqual(
                    (new.source_rows, new.excluded_rows, new.omitted_groups, new.small_groups),
                    (old.source_rows, old.excluded_rows, old.omitted_groups, old.small_groups),
                )
                try:
                    expected = aggregate_payload(old)
                except ValueError as ex:
                    with self.assertRaises(ValueError, msg=str(ex)):
                        aggregate_payload(new)
                else:
                    self.assertEqual(aggregate_payload(new), expected)

    def test_demo_csv_parity(self) -> None:
        self.check(demo_config(), SCOPES)

    def test_edge_cases_parity(self) -> None:
        frame = edge_frame()
        self.check(demo_config(), EDGE_SCOPES, source=lambda config: frame.copy())

    def test_edge_case_expectations(self) -> None:
        frame = edge_frame()
        with patch("insightquest.data._read_demo_csv", side_effect=lambda config: frame.copy()):
            delay = execute_query(None, demo_config(), plan_for(metric="purchase_to_install_delay"))
            pending = execute_query(None, demo_config(), plan_for(metric="pending_installations"))
            kinds = execute_query(None, demo_config(), plan_for(
                metric="purchases", group_by="device_type", order="desc", top_n=3))
        self.assertEqual(delay.source_rows, len(frame))
        negative_or_missing = delay.excluded_rows
        self.assertGreater(negative_or_missing, 3)
        self.assertEqual(delay.excluded_rows + int(delay.table.iloc[0]["count"]), len(frame))
        self.assertEqual(pending.table.iloc[0]["count"], 2)  # a missing and an unparseable install date
        self.assertTrue(kinds.omitted_groups)
        self.assertEqual(len(kinds.table), 3)
        with patch("insightquest.data._read_demo_csv", side_effect=lambda config: frame.copy()):
            labels = execute_query(
                None, demo_config(), plan_for(metric="purchases", group_by="device_type"),
            ).table["group"].tolist()
        self.assertIn("(missing)", labels)
        self.assertIn("", labels)
        self.assertIn("a", labels)
        self.assertIn("A", labels)


class AnalysisParityTests(unittest.TestCase):
    def check(self, config, source=None) -> None:
        columns = fetch_flat_table_columns(None, config)
        if source is not None:
            columns = set(source.columns)
        fields = available_chat_fields(config, columns)
        for analysis, (start, end) in itertools.product(
            [a for a in Analysis if a in ANALYSIS_LABELS],
            ((None, None), (date(2023, 2, 1), date(2023, 4, 30)), (date(2026, 1, 1), date(2026, 2, 3))),
        ):
            with self.subTest(analysis=analysis.value, start=start):
                with patched(None if source is None else (lambda config: source.copy())):
                    filtering = bool(start and end and "purchase_date" in fields)
                    frame = fetch_flat_table_rows(
                        None, config, fields, date_field=fields["purchase_date"] if filtering else None,
                        from_date=start if filtering else None, to_date=end if filtering else None,
                        limit=10**9,
                    )
                    old = execute_analysis(frame, analysis)
                    new = aggregate_analysis(None, config, fields, analysis, start, end)
                    answer = run_analysis_locally(None, config, columns, analysis, start, end)
                if analysis in {Analysis.TYPES, Analysis.COMPANIES}:
                    key = new.columns[0]
                    old = old.sort_values(["Rows", key], ascending=[False, True], kind="stable")
                elif analysis == Analysis.DELAY_BY_TYPE:
                    old = old.sort_values(["Median", "Device type"], ascending=[False, True], kind="stable")
                pd.testing.assert_frame_equal(
                    new.reset_index(drop=True), old.reset_index(drop=True), check_dtype=False,
                )
                self.assertEqual(new.attrs.get("note", ""), old.attrs.get("note", ""))
                self.assertEqual(answer.note, new.attrs.get("note", ""))
                self.assertEqual(answer.scope, (start, end) if filtering else (None, None))

    def test_demo_csv_parity(self) -> None:
        self.check(demo_config())

    def test_edge_cases_parity(self) -> None:
        frame = edge_frame()
        self.check(demo_config(), source=frame)

    def test_missing_fields_are_reported(self) -> None:
        config = demo_config(col_installed_at="")
        fields = available_chat_fields(config, fetch_flat_table_columns(None, config))
        with self.assertRaisesRegex(ValueError, "Required mapped fields are missing: install_date"):
            aggregate_analysis(None, config, fields, Analysis.INSTALLS, None, None)

    def test_large_per_value_tables_are_capped_and_noted(self) -> None:
        many = pd.DataFrame({"grp": [f"c{i:05d}" for i in range(MAX_TABLE_ROWS + 1)], "cnt": 1})
        fields = {"company_id": "col_customer"}
        with patch("insightquest.ai_chat.fetch_chat_groups", return_value=many.rename(
                columns={"grp": "group", "cnt": "count"})):
            table = aggregate_analysis(None, demo_config(), fields, Analysis.COMPANIES, None, None)
        self.assertEqual(len(table), MAX_TABLE_ROWS)
        self.assertIn(f"Showing the first {MAX_TABLE_ROWS:,} rows", table.attrs["note"])


class ChatSqlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = demo_config(demo_mode=False)
        self.start, self.end = date(2026, 1, 1), date(2026, 1, 31)

    def test_group_queries_bind_values_and_bracket_identifiers(self) -> None:
        for mode, grouping in itertools.product(("rows", "events", "pending", "delay"), ("none", "period", "value")):
            sql, params = build_chat_group_query(
                self.config, mode, grouping, event_attr="col_installed_at", group_attr="col_product_type",
                grain="week", scope_attr="col_sales_date", from_date=self.start, to_date=self.end,
                order="count", limit=51,
            )
            with self.subTest(mode=mode, grouping=grouping):
                self.assertIn("[PurchaseDate] >= :start_dt", sql)
                self.assertIn("[PurchaseDate] < :end_exclusive", sql)
                self.assertNotIn("2026", sql)
                self.assertEqual(params["end_exclusive"].isoformat(), "2026-02-01T00:00:00")
                self.assertNotIn("SELECT *", sql)
                if grouping != "none":
                    self.assertIn("TOP (:limit)", sql)
                    self.assertEqual(params["limit"], 51)

    def test_delay_uses_exact_median_and_millisecond_precision(self) -> None:
        sql, _ = build_chat_group_query(
            self.config, "delay", "value", event_attr="col_installed_at", group_attr="col_product_type",
        )
        self.assertIn("PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY delay_ms) OVER (PARTITION BY grp)", sql)
        self.assertIn("DATEDIFF_BIG(millisecond, [PurchaseDate], [TankInstallDate])", sql)
        self.assertIn("[TankInstallDate] >= [PurchaseDate]", sql)
        self.assertIn("COLLATE Latin1_General_BIN2", sql)
        self.assertIn("N'(missing)'", sql)
        self.assertIn("ORDER BY s.grp", sql)

    def test_event_and_period_expressions(self) -> None:
        sql, _ = build_chat_group_query(
            self.config, "events", "period", event_attr="col_installed_at", grain="week",
        )
        self.assertIn("[TankInstallDate] IS NOT NULL", sql)
        self.assertIn("CONVERT(char(10), DATEADD(day, -(DATEDIFF(day, '19000101', [TankInstallDate]) % 7)", sql)
        self.assertTrue(sql.rstrip().endswith("GROUP BY grp ORDER BY grp"))
        pending, _ = build_chat_group_query(self.config, "pending", "none")
        self.assertIn("[PurchaseDate] IS NOT NULL AND [TankInstallDate] IS NULL", pending)
        self.assertIn("HAVING COUNT(*) > 0", pending)

    def test_profile_query_is_one_aggregate_row(self) -> None:
        sql, params = build_chat_row_profile_query(
            self.config, "col_sales_date", self.start, self.end,
            distinct_attrs={"device_id": "col_device"}, pending=True, delay=True, coordinates=True,
        )
        self.assertTrue(sql.startswith("SELECT COUNT(*) AS row_count"))
        for fragment in ("COUNT(DISTINCT CAST([Id] AS nvarchar(4000))", "AS pending", "AS paired",
                         "AS negative_delay", "TRY_CAST([Latitude] AS float) BETWEEN -90 AND 90"):
            self.assertIn(fragment, sql)
        self.assertNotIn("TOP", sql)
        self.assertEqual(set(params), {"start_dt", "end_exclusive"})

    def test_unsafe_or_unknown_inputs_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_chat_group_query(replace(self.config, col_product_type="x]; DROP TABLE y"),
                                   "rows", "value", group_attr="col_product_type")
        for kwargs in ({"mode": "median"}, {"mode": "rows", "grouping": "sql"},
                       {"mode": "rows", "grouping": "value", "group_attr": "col_territory"},
                       {"mode": "rows", "grouping": "value", "group_attr": "col_product_type; DROP"},
                       {"mode": "events", "grouping": "none"}, {"mode": "rows", "limit": 0},
                       {"mode": "rows", "scope_attr": "col_sales_date"},
                       {"mode": "rows", "from_date": self.start, "to_date": self.end}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                build_chat_group_query(self.config, **kwargs)
        with self.assertRaises(ValueError):
            build_chat_row_profile_query(self.config, distinct_attrs={"Bad Alias": "col_device"})

    def test_database_fetchers_never_read_rows_and_shape_results(self) -> None:
        frame = pd.DataFrame({
            "grp": ["B", "A"], "cnt": [5_000_000, 5_000_000], "mean_days": [1.5, 2.0],
            "median_days": [1.0, 2.0], "min_days": [0.0, 1.0], "max_days": [4.0, 5.0],
        })
        with (
            patch("insightquest.data.fetch_flat_table_columns",
                  return_value={"PurchaseDate", "TankInstallDate", "DeviceType"}),
            patch("insightquest.data._read_aggregate", return_value=frame) as read,
            patch("insightquest.data.fetch_flat_table_rows", side_effect=AssertionError("raw rows read")),
        ):
            groups = fetch_chat_groups(
                MagicMock(), self.config, "delay", "value", event_attr="col_installed_at",
                group_attr="col_product_type", order="count",
            )
            self.assertEqual(groups["group"].tolist(), ["A", "B"])
            self.assertEqual(groups["count"].dtype, np.int64)
            self.assertEqual(groups["count"].tolist(), [5_000_000, 5_000_000])
            read.return_value = pd.DataFrame({"row_count": [5_000_000], "pending": [None]})
            self.assertEqual(
                fetch_chat_row_profile(MagicMock(), self.config, pending=True),
                {"row_count": 5_000_000, "pending": 0},
            )

    def test_empty_database_result_has_typed_columns(self) -> None:
        with (
            patch("insightquest.data.fetch_flat_table_columns", return_value={"PurchaseDate", "TankInstallDate"}),
            patch("insightquest.data._read_aggregate", return_value=pd.DataFrame()),
        ):
            empty = fetch_chat_groups(MagicMock(), self.config, "delay", "none", event_attr="col_installed_at")
        self.assertTrue(empty.empty)
        self.assertEqual(list(empty.columns), ["group", "count", "mean_days", "median_days", "min_days", "max_days"])


if __name__ == "__main__":
    unittest.main()
