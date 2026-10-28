from __future__ import annotations

import logging
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from sqlalchemy.dialects.mssql.pyodbc import MSDialect_pyodbc

from insightquest.config import (
    config_fingerprint,
    get_config,
    parse_customer_size_edges,
)
from insightquest.data import load_market_series
from insightquest.db import (
    build_engine_url,
    build_odbc_connection_string,
    check_db_read_only,
    create_db_engine,
)


def sql_config(**overrides):
    values = dict(
        demo_mode=False, db_server="srv.example.net", db_port=1433, db_name="Db",
        db_auth_mode="sql", db_user="svc", db_password="p@ss", db_driver="ODBC Driver 18 for SQL Server",
        db_trust_cert=True,
    )
    values.update(overrides)
    return replace(get_config(), **values)


class ConfigTests(unittest.TestCase):
    def env(self, **values):
        return patch.dict(os.environ, values)

    def test_defaults(self) -> None:
        keys = [
            "IQ_COL_TERRITORY", "IQ_COL_SHIPPED_AT", "IQ_SHIPPED_AT_IS_PROXY", "IQ_CUSTOMER_SIZE_EDGES",
            "IQ_MARKET_CSV", "IQ_CACHE_TTL_SECONDS", "IQ_DB_QUERY_TIMEOUT_SECONDS", "IQ_DEMO_MODE",
            "IQ_DB_POOL_SIZE", "IQ_DB_POOL_TIMEOUT_SECONDS",
        ]
        with patch.dict(os.environ, {}), patch("insightquest.config.load_dotenv"):
            for key in keys:
                os.environ.pop(key, None)
            config = get_config()
        self.assertEqual(config.col_territory, "Territory")
        self.assertEqual(config.col_shipped_at, "PurchaseDate")
        self.assertTrue(config.shipped_at_is_proxy)
        self.assertEqual(config.customer_size_edges, (10, 100))
        self.assertEqual(config.market_csv_path, "")
        self.assertEqual(config.cache_ttl_seconds, 300)
        self.assertEqual(config.db_query_timeout_seconds, 60)

    def test_overrides_and_proxy_flag(self) -> None:
        with self.env(IQ_SHIPPED_AT_IS_PROXY="false", IQ_COL_SHIPPED_AT="ShipDate",
                      IQ_CUSTOMER_SIZE_EDGES="5, 50, 500", IQ_CACHE_TTL_SECONDS="30",
                      IQ_DB_QUERY_TIMEOUT_SECONDS="120", IQ_MARKET_CSV="m.csv"):
            config = get_config()
        self.assertFalse(config.shipped_at_is_proxy)
        self.assertEqual(config.col_shipped_at, "ShipDate")
        self.assertEqual(config.customer_size_edges, (5, 50, 500))
        self.assertEqual((config.cache_ttl_seconds, config.db_query_timeout_seconds), (30, 120))
        self.assertEqual(config.market_csv_path, "m.csv")

    def test_edges_validation(self) -> None:
        self.assertEqual(parse_customer_size_edges(None), (10, 100))
        self.assertEqual(parse_customer_size_edges(" 3 "), (3,))
        for bad in ["10,abc", "0,10", "-5,10", "100,10", "10,10", "10,,20"]:
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "IQ_CUSTOMER_SIZE_EDGES"):
                parse_customer_size_edges(bad)

    def test_numeric_settings_are_validated(self) -> None:
        for name in ("IQ_CACHE_TTL_SECONDS", "IQ_DB_QUERY_TIMEOUT_SECONDS", "IQ_DB_POOL_SIZE"):
            for bad in ("0", "-1", "abc"):
                with self.subTest(name=name, bad=bad), self.env(**{name: bad}):
                    with self.assertRaisesRegex(ValueError, name):
                        get_config()

    def test_fingerprint_excludes_secrets_and_tracks_changes(self) -> None:
        config = sql_config(openai_api_key="sk-secret")
        fingerprint = config_fingerprint(config)
        self.assertEqual(fingerprint, config_fingerprint(replace(config, db_password="other", openai_api_key="x")))
        self.assertNotEqual(fingerprint, config_fingerprint(replace(config, col_territory="Region")))
        self.assertNotEqual(fingerprint, config_fingerprint(replace(config, customer_size_edges=(5, 50))))
        self.assertNotIn("p@ss", fingerprint)


class DbEngineTests(unittest.TestCase):
    def test_odbc_values_with_delimiters_are_brace_escaped(self) -> None:
        raw = build_odbc_connection_string(sql_config(db_password="a;b}c=d", db_user="u;1"))
        self.assertIn("PWD={a;b}}c=d};", raw)
        self.assertIn("UID={u;1};", raw)
        self.assertIn("DRIVER={ODBC Driver 18 for SQL Server};", raw)
        self.assertIn("SERVER=srv.example.net,1433;", raw)
        self.assertIn("TrustServerCertificate=yes", raw)

    def test_special_characters_survive_the_pyodbc_dialect_round_trip(self) -> None:
        password = "p+ss%41 w;rd}"
        config = sql_config(db_password=password)
        args, _ = MSDialect_pyodbc().create_connect_args(build_engine_url(config))
        self.assertEqual(args[0], build_odbc_connection_string(config))
        self.assertIn("PWD={p+ss%41 w;rd}}};", args[0])

    def test_windows_auth_has_no_credentials(self) -> None:
        raw = build_odbc_connection_string(sql_config(db_auth_mode="windows"))
        self.assertIn("Trusted_Connection=yes", raw)
        self.assertNotIn("PWD", raw)

    def test_auth_validation(self) -> None:
        with self.assertRaisesRegex(ValueError, "IQ_DB_PASSWORD"):
            build_odbc_connection_string(sql_config(db_password=""))
        with self.assertRaisesRegex(ValueError, "IQ_DB_AUTH_MODE"):
            build_odbc_connection_string(sql_config(db_auth_mode="kerberos"))

    def test_engine_uses_pool_settings_and_default_timeout(self) -> None:
        config = sql_config(db_pool_size=7, db_pool_timeout_seconds=11, db_query_timeout_seconds=45)
        with patch("insightquest.db.create_engine") as create, patch("insightquest.db.event.listens_for") as listen:
            create_db_engine(config)
        kwargs = create.call_args.kwargs
        self.assertEqual(
            (kwargs["pool_size"], kwargs["pool_timeout"], kwargs["pool_recycle"], kwargs["pool_pre_ping"]),
            (7, 11, 1800, True),
        )
        self.assertGreaterEqual(kwargs["max_overflow"], 0)
        connection = SimpleNamespace(timeout=0)
        listen.return_value.call_args.args[0](connection, None)
        self.assertEqual(connection.timeout, 45)

    def test_explicit_ai_timeout_overrides_default(self) -> None:
        with patch("insightquest.db.create_engine"), patch("insightquest.db.event.listens_for") as listen:
            create_db_engine(sql_config(db_query_timeout_seconds=60), query_timeout_seconds=30)
        connection = SimpleNamespace(timeout=0)
        listen.return_value.call_args.args[0](connection, None)
        self.assertEqual(connection.timeout, 30)

    def test_password_is_never_logged_by_read_only_check(self) -> None:
        engine = MagicMock()
        engine.connect.side_effect = RuntimeError("boom p@ss")
        with self.assertLogs("insightquest.db", level=logging.WARNING) as logs:
            self.assertIsNone(check_db_read_only(engine, "dbo.Insight Project"))
        self.assertNotIn("p@ss", "\n".join(logs.output))


class ReadOnlyCheckTests(unittest.TestCase):
    def engine_returning(self, *results):
        engine = MagicMock()
        connection = engine.connect.return_value.__enter__.return_value
        responses = []
        for result in results:
            response = MagicMock()
            response.one.return_value = result
            response.fetchall.return_value = result
            responses.append(response)
        connection.execute.side_effect = responses
        return engine, connection

    def test_true_when_no_write_permissions(self) -> None:
        engine, connection = self.engine_returning((0, 0, 0))
        self.assertIs(check_db_read_only(engine, "dbo.Insight Project"), True)
        statement, params = connection.execute.call_args.args
        self.assertIn("HAS_PERMS_BY_NAME(:obj, 'OBJECT', 'INSERT')", str(statement))
        self.assertEqual(params, {"obj": "[dbo].[Insight Project]"})

    def test_false_when_any_write_permission(self) -> None:
        for row in ((1, 0, 0), (0, 1, 0), (0, 0, 1)):
            engine, _ = self.engine_returning(row)
            self.assertIs(check_db_read_only(engine, "dbo.Insight Project"), False)

    def test_falls_back_to_fn_my_permissions_when_unknown(self) -> None:
        engine, connection = self.engine_returning((None, None, None), [("SELECT",), ("VIEW DEFINITION",)])
        self.assertIs(check_db_read_only(engine, "dbo.Insight Project"), True)
        self.assertIn("fn_my_permissions", str(connection.execute.call_args.args[0]))
        engine, _ = self.engine_returning((None, None, None), [("SELECT",), ("UPDATE",)])
        self.assertIs(check_db_read_only(engine, "dbo.Insight Project"), False)

    def test_none_when_undeterminable_and_never_raises(self) -> None:
        engine, _ = self.engine_returning((None, None, None), [])
        self.assertIsNone(check_db_read_only(engine, "dbo.Insight Project"))
        failing = MagicMock()
        failing.connect.side_effect = RuntimeError("down")
        self.assertIsNone(check_db_read_only(failing, "dbo.Insight Project"))
        self.assertIsNone(check_db_read_only(MagicMock(), "bad]name"))


class MarketSeriesTests(unittest.TestCase):
    def load(self, content: str | None):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            path = Path(folder) / "market.csv"
            if content is not None:
                path.write_text(content, encoding="utf-8")
            return load_market_series(replace(get_config(), market_csv_path=str(path)))

    def test_not_configured_returns_none(self) -> None:
        self.assertIsNone(load_market_series(replace(get_config(), market_csv_path="")))

    def test_valid_file_is_normalised_to_month_starts(self) -> None:
        series = self.load("Month, Value\n2024-02-15,3.5\n2024-01-01,2\n")
        self.assertEqual(list(series.columns), ["month", "value"])
        self.assertEqual([m.strftime("%Y-%m-%d") for m in series["month"]], ["2024-01-01", "2024-02-01"])
        self.assertEqual(series["value"].tolist(), [2.0, 3.5])

    def test_invalid_files_are_rejected(self) -> None:
        cases = {
            "wrong columns": "date,amount\n2024-01-01,1\n",
            "empty": "month,value\n",
            "bad date": "month,value\nnot-a-date,1\n",
            "bad number": "month,value\n2024-01-01,abc\n",
            "duplicate": "month,value\n2024-01-01,1\n2024-01-20,2\n",
        }
        for label, content in cases.items():
            with self.subTest(label), self.assertRaises(ValueError):
                self.load(content)
        with self.assertRaises(FileNotFoundError):
            self.load(None)


if __name__ == "__main__":
    unittest.main()
