from __future__ import annotations

import json
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd

from insightquest.ai_chat import Analysis, execute_analysis, planner_payload
from insightquest.ai_query import (
    MAX_SOURCE_ROWS,
    MIN_GROUP_ROWS,
    Metric,
    aggregate_payload,
    aggregate_query,
    execute_query,
    explain_aggregates,
    propose_query,
    validate_plan,
)
from insightquest.config import get_config
from insightquest.db import create_db_engine


def plan_values(**changes: object) -> dict[str, object]:
    values: dict[str, object] = {
        "metric": "installations", "group_by": "none", "period": "month",
        "date_field": "none", "start_date": None, "end_date": None,
        "order": "asc", "top_n": 50,
    }
    values.update(changes)
    return values


def response(content: str | None) -> SimpleNamespace:
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


class GuidedQueryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = replace(
            get_config(), demo_mode=True,
            demo_csv_path=Path(__file__).resolve().parents[1] / "demo_data.csv",
            col_installed_at="TankInstallDate", col_sales_date="PurchaseDate",
            col_product_type="DeviceType", openai_api_key="test-not-a-real-key",
            ai_allow_external_data=True,
        )

    def test_valid_plan_round_trips(self) -> None:
        values = plan_values(date_field="purchase_date", start_date="2026-01-01", end_date="2026-01-31")
        self.assertEqual(validate_plan(values).payload(), values)

    def test_rejects_malformed_or_injected_plans(self) -> None:
        bad_values = [
            None, [], "SELECT * FROM records", {},
            plan_values(sql="DROP TABLE anything"),
            plan_values(metric="DELETE"),
            plan_values(group_by="company_id"),
            plan_values(date_field="install_date]; DROP TABLE x"),
            plan_values(period="year"),
            plan_values(order="random"),
            plan_values(top_n=True), plan_values(top_n=0), plan_values(top_n=51),
            plan_values(top_n=1.5), plan_values(top_n="5"),
            plan_values(start_date="2026-01-01"),
            plan_values(date_field="install_date"),
            plan_values(date_field="install_date", start_date="2026-02-30", end_date="2026-03-01"),
            plan_values(date_field="install_date", start_date="2026-2-01", end_date="2026-03-01"),
            plan_values(date_field="install_date", start_date=123, end_date=456),
            plan_values(date_field="install_date", start_date="2026-02-01", end_date="2026-01-01"),
            plan_values(date_field="install_date", start_date="2026-01-01", end_date="9999-12-31"),
        ]
        for value in bad_values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_plan(value)

    def test_demo_runs_without_database_or_external_call(self) -> None:
        with patch("openai.OpenAI") as client:
            result = execute_query(None, self.config, validate_plan(plan_values(group_by="device_type")))
        client.assert_not_called()
        self.assertFalse(result.table.empty)
        self.assertLessEqual(len(result.table), 50)
        self.assertEqual(set(result.table.columns), {"group", "count"})

    def test_date_scope_includes_end_day_and_periods_sort_chronologically(self) -> None:
        data = pd.DataFrame({"install_date": [
            "2026-02-01 00:00:00", "2026-01-31 23:59:59",
            "2026-01-01 00:00:00", "2025-12-31 00:00:00",
        ]})
        plan = validate_plan(plan_values(
            group_by="period", date_field="install_date",
            start_date="2026-01-01", end_date="2026-01-31",
        ))
        result = aggregate_query(data, plan)
        self.assertEqual(result.table.to_dict("records"), [{"group": "2026-01-01", "count": 2}])

    def test_week_begins_monday_and_descending_order(self) -> None:
        frame = pd.DataFrame({"install_date": ["2026-01-04", "2026-01-05"]})
        result = aggregate_query(frame, validate_plan(plan_values(
            group_by="period", period="week", order="desc",
        )))
        self.assertEqual(result.table["group"].tolist(), ["2026-01-05", "2025-12-29"])

    def test_pending_uses_purchase_period_and_invalid_install_dates(self) -> None:
        frame = pd.DataFrame({
            "purchase_date": ["2026-01-01", "2026-02-01", None],
            "install_date": [None, "invalid", None],
        })
        result = aggregate_query(frame, validate_plan(plan_values(
            metric="pending_installations", group_by="period",
        )))
        self.assertEqual(result.table["count"].tolist(), [1, 1])
        self.assertEqual(result.excluded_rows, 1)

    def test_delay_excludes_negative_and_missing_dates(self) -> None:
        frame = pd.DataFrame({
            "purchase_date": ["2026-01-01", "2026-01-05", None, "2026-01-01"],
            "install_date": ["2026-01-03", "2026-01-04", "2026-01-03", "2026-01-05"],
        })
        result = aggregate_query(frame, validate_plan(plan_values(metric="purchase_to_install_delay")))
        self.assertEqual(result.excluded_rows, 2)
        self.assertEqual(result.table.iloc[0]["mean_days"], 3)
        self.assertEqual(result.table.iloc[0]["median_days"], 3)
        self.assertEqual(result.table.iloc[0]["count"], 2)
        self.assertNotIn("min", result.table)
        self.assertNotIn("max", result.table)

    def test_type_ranking_top_n_and_local_labels(self) -> None:
        frame = pd.DataFrame({
            "install_date": ["2026-01-01"] * 30,
            "device_type": ["secret-type-A"] * 10 + ["secret-type-B"] * 20,
        })
        result = aggregate_query(frame, validate_plan(plan_values(
            group_by="device_type", order="desc", top_n=1,
        )))
        self.assertEqual(result.table.iloc[0]["group"], "secret-type-B")
        self.assertTrue(result.omitted_groups)
        payload = aggregate_payload(result)
        self.assertEqual(payload["rows"], [{"group": "Type group 1", "count": 20}])
        self.assertNotIn("secret-type", json.dumps(payload))
        self.assertNotIn("source_rows", payload)

    def test_result_payload_is_bounded_to_fifty_groups(self) -> None:
        dates = pd.date_range("2026-01-01", periods=55).repeat(MIN_GROUP_ROWS)
        result = aggregate_query(
            pd.DataFrame({"install_date": dates}),
            validate_plan(plan_values(group_by="period", period="day")),
        )
        self.assertEqual(len(result.table), 50)
        self.assertEqual(len(aggregate_payload(result)["rows"]), 50)
        self.assertTrue(result.omitted_groups)

    def test_delay_payload_contains_only_count_mean_and_median(self) -> None:
        frame = pd.DataFrame({
            "purchase_date": ["2026-01-01"] * MIN_GROUP_ROWS,
            "install_date": ["2026-01-03"] * MIN_GROUP_ROWS,
        })
        result = aggregate_query(frame, validate_plan(plan_values(metric="purchase_to_install_delay")))
        self.assertEqual(aggregate_payload(result)["rows"], [{
            "group": "All matching rows", "count": MIN_GROUP_ROWS,
            "mean_days": 2.0, "median_days": 2.0,
        }])

    def test_threshold_checks_all_groups_before_top_n(self) -> None:
        for count in (0, MIN_GROUP_ROWS - 1, MIN_GROUP_ROWS):
            frame = pd.DataFrame({
                "install_date": ["2026-01-01"] * count,
            })
            result = aggregate_query(frame, validate_plan(plan_values()))
            with self.subTest(count=count):
                if count < MIN_GROUP_ROWS:
                    with self.assertRaises(ValueError):
                        aggregate_payload(result)
                else:
                    self.assertEqual(aggregate_payload(result)["rows"], [
                        {"group": "All matching rows", "count": count},
                    ])
        frame = pd.DataFrame({
            "install_date": ["2026-01-01"] * 21,
            "device_type": ["large"] * 20 + ["small"],
        })
        result = aggregate_query(frame, validate_plan(plan_values(
            group_by="device_type", order="desc", top_n=1,
        )))
        self.assertEqual(result.table.iloc[0]["count"], 20)
        with self.assertRaisesRegex(ValueError, "fewer than"):
            aggregate_payload(result)

    def test_row_limit_overflow_is_not_aggregated(self) -> None:
        with (
            patch("insightquest.ai_query.fetch_flat_table_columns", return_value={"TankInstallDate"}),
            patch("insightquest.ai_query.fetch_flat_table_rows", return_value=pd.DataFrame(index=range(MAX_SOURCE_ROWS + 1))) as fetch,
        ):
            with self.assertRaisesRegex(ValueError, "partial results"):
                execute_query(None, self.config, validate_plan(plan_values()))
        self.assertEqual(fetch.call_args.kwargs["limit"], MAX_SOURCE_ROWS + 1)

    def test_exact_source_limit_is_accepted(self) -> None:
        frame = pd.DataFrame({"install_date": ["2026-01-01"] * MAX_SOURCE_ROWS})
        with (
            patch("insightquest.ai_query.fetch_flat_table_columns", return_value={"TankInstallDate"}),
            patch("insightquest.ai_query.fetch_flat_table_rows", return_value=frame),
        ):
            result = execute_query(None, self.config, validate_plan(plan_values()))
        self.assertEqual(result.table.iloc[0]["count"], MAX_SOURCE_ROWS)

    def test_only_required_columns_are_requested(self) -> None:
        with (
            patch("insightquest.ai_query.fetch_flat_table_columns", return_value={"TankInstallDate"}),
            patch("insightquest.ai_query.fetch_flat_table_rows", return_value=pd.DataFrame({"install_date": ["2026-01-01"]})) as fetch,
        ):
            execute_query(None, self.config, validate_plan(plan_values()))
        self.assertEqual(fetch.call_args.args[2], {"install_date": "col_installed_at"})

    def test_missing_mapping_fails_without_reading_rows(self) -> None:
        with (
            patch("insightquest.ai_query.fetch_flat_table_rows") as fetch,
            self.assertRaisesRegex(ValueError, "mapping is unavailable"),
        ):
            execute_query(None, replace(self.config, col_installed_at=""), validate_plan(plan_values()))
        fetch.assert_not_called()

    def test_database_requires_read_only_attestation(self) -> None:
        with patch("insightquest.ai_query.fetch_flat_table_columns") as columns:
            with self.assertRaisesRegex(ValueError, "SELECT-only"):
                execute_query(None, replace(self.config, demo_mode=False, ai_db_read_only_confirmed=False),
                              validate_plan(plan_values()))
        columns.assert_not_called()

    def test_sql_reader_uses_select_and_bound_values(self) -> None:
        config = replace(self.config, demo_mode=False, ai_db_read_only_confirmed=True)
        plan = validate_plan(plan_values(
            date_field="install_date", start_date="2026-01-01", end_date="2026-01-31",
        ))
        engine = MagicMock()
        with (
            patch("insightquest.ai_query.fetch_flat_table_columns", return_value={"TankInstallDate"}),
            patch("insightquest.data.fetch_flat_table_columns", return_value={"TankInstallDate"}),
            patch("insightquest.data.pd.read_sql", return_value=pd.DataFrame({"install_date": ["2026-01-01"]})) as read,
        ):
            execute_query(engine, config, plan)
        sql = str(read.call_args.args[0])
        self.assertTrue(sql.startswith("SELECT TOP (:limit)"))
        self.assertIn(">= :start_dt", sql)
        self.assertIn("< :end_exclusive", sql)
        self.assertNotIn("2026-01-01", sql)
        self.assertNotIn("Id", sql)
        self.assertEqual(read.call_args.kwargs["params"]["end_exclusive"].isoformat(), "2026-02-01T00:00:00")

    def test_query_timeout_is_set_on_odbc_connection_not_cursor(self) -> None:
        with patch("insightquest.db.create_engine") as create, patch("insightquest.db.event.listens_for") as listen:
            create_db_engine(self.config, query_timeout_seconds=30)
        self.assertEqual(listen.call_args.args[1], "connect")
        callback = listen.return_value.call_args.args[0]
        connection = SimpleNamespace(timeout=0)
        callback(connection, None)
        self.assertEqual(connection.timeout, 30)
        with self.assertRaises(ValueError):
            create_db_engine(self.config, query_timeout_seconds=0)
        create.assert_called_once()

    def test_planner_sends_question_but_not_source_data(self) -> None:
        with patch("openai.OpenAI") as client:
            client.return_value.chat.completions.create.return_value = response(json.dumps(plan_values()))
            plan = propose_query("Installations by month?", self.config)
        self.assertEqual(plan.metric, Metric.INSTALLATIONS)
        options = client.return_value.chat.completions.create.call_args.kwargs
        request = json.dumps(options)
        self.assertIn("Installations by month?", request)
        for value in ("TankInstallDate", "CompanyId", "Latitude", self.config.source_table):
            self.assertNotIn(value, request)
        self.assertEqual(client.call_args.kwargs["max_retries"], 0)
        self.assertEqual(client.call_args.kwargs["timeout"], 30)
        self.assertEqual(options["max_completion_tokens"], 600)

    def test_bad_or_empty_model_outputs_fail_explicitly(self) -> None:
        outputs = [None, "", "not JSON", '{"sql":"SELECT 1"}', json.dumps(plan_values(top_n=999))]
        for output in outputs:
            with self.subTest(output=output), patch("openai.OpenAI") as client:
                client.return_value.chat.completions.create.return_value = response(output)
                with self.assertRaises(ValueError):
                    propose_query("How many installations?", self.config)
        with patch("openai.OpenAI") as client:
            client.return_value.chat.completions.create.return_value = SimpleNamespace(choices=[])
            with self.assertRaises(ValueError):
                propose_query("How many installations?", self.config)

    def test_unsupported_plan_cannot_execute(self) -> None:
        with patch("openai.OpenAI") as client:
            client.return_value.chat.completions.create.return_value = response(json.dumps(plan_values(metric="unsupported")))
            plan = propose_query("Give me raw records", self.config)
        with self.assertRaisesRegex(ValueError, "cannot be represented"):
            execute_query(None, self.config, plan)

    def test_outbound_requests_require_explicit_configuration(self) -> None:
        result = aggregate_query(pd.DataFrame({"install_date": ["2026-01-01"] * 10}),
                                 validate_plan(plan_values()))
        for config in (
            replace(self.config, ai_allow_external_data=False),
            replace(self.config, openai_api_key=""),
        ):
            with patch("openai.OpenAI") as client:
                with self.assertRaises(ValueError):
                    propose_query("Installations?", config)
                with self.assertRaises(ValueError):
                    explain_aggregates(result, config)
                client.assert_not_called()
        for question in ("", " " * 5, "a" * 2001):
            with patch("openai.OpenAI") as client, self.assertRaises(ValueError):
                propose_query(question, self.config)
            client.assert_not_called()

    def test_explanation_sends_only_safe_payload(self) -> None:
        frame = pd.DataFrame({
            "install_date": ["2026-01-01"] * 10,
            "device_type": ["confidential-category"] * 10,
            "company_id": ["secret-company"] * 10,
            "device_id": ["secret-device"] * 10,
            "latitude": [45.5] * 10,
        })
        result = aggregate_query(frame, validate_plan(plan_values(group_by="device_type")))
        with patch("openai.OpenAI") as client:
            client.return_value.chat.completions.create.return_value = response("Ten installation rows.")
            self.assertEqual(explain_aggregates(result, self.config), "Ten installation rows.")
        messages = client.return_value.chat.completions.create.call_args.kwargs["messages"]
        self.assertEqual(json.loads(messages[1]["content"]), aggregate_payload(result))
        request = json.dumps(messages)
        for value in ("confidential-category", "secret-company", "secret-device", "45.5"):
            self.assertNotIn(value, request)

    def test_unsafe_or_empty_explanation_never_calls_provider(self) -> None:
        result = aggregate_query(pd.DataFrame({"install_date": ["2026-01-01"]}),
                                 validate_plan(plan_values()))
        with patch("openai.OpenAI") as client, self.assertRaises(ValueError):
            explain_aggregates(result, self.config)
        client.assert_not_called()
        safe = aggregate_query(pd.DataFrame({"install_date": ["2026-01-01"] * 10}),
                               validate_plan(plan_values()))
        with patch("openai.OpenAI") as client:
            client.return_value.chat.completions.create.return_value = response(None)
            with self.assertRaisesRegex(ValueError, "no explanation"):
                explain_aggregates(safe, self.config)

    def test_private_fixed_label_mode_still_works(self) -> None:
        self.assertEqual(planner_payload([Analysis.INSTALLS]), {"candidates": ["installations_by_month"]})
        result = execute_analysis(pd.DataFrame({"install_date": ["2026-01-01"]}), Analysis.INSTALLS)
        self.assertEqual(result.iloc[0]["Rows with date"], 1)


if __name__ == "__main__":
    unittest.main()
