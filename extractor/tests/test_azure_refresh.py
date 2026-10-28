import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + ".py"))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pilot = load_script("pilot_azure_flat_table")
with patch.dict(sys.modules, {"pilot_azure_flat_table": pilot}):
    refresh = load_script("refresh_azure_flat_table")


class AzureRefreshTests(unittest.TestCase):
    def connection(self, actual=100):
        connection = MagicMock()
        cursor = connection.cursor.return_value
        cursor.execute.return_value = cursor
        cursor.fetchall.side_effect = [
            [("Id", "uniqueidentifier", 16, 0, 0), ("Latitude", "decimal", 5, 9, 6)],
            [("Id", "uniqueidentifier", 16, 0, 0), ("Latitude", "int", 4, 10, 0)],
        ]
        cursor.fetchone.side_effect = [(500,), (90,), (10,), (410,), (actual, actual), (0,), (1,), (1,)]
        return connection

    def test_batch_query_preserves_selection_rules(self):
        query = refresh.stage_query((SCRIPTS / "preview_device_flat_table.sql").read_text())
        self.assertIn("JOIN #BatchDevices", query)
        self.assertIn("ORDER BY o.[CreatedDate] DESC, o.[Id] DESC", query)
        self.assertIn("MIN(w.[StartDate])", query)
        self.assertIn("MIN(t.[InstalledDate])", query)
        self.assertFalse(query.endswith(";"))

    def test_changed_preview_source_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "source changed"):
            refresh.stage_query("SELECT * FROM other")

    def test_installation_sample_uses_fallback_and_deterministic_ranking(self):
        query = refresh.sampled_query((SCRIPTS / "stage_azure_devices.sql").read_text(), 1_000_000)
        self.assertIn("SELECT TOP (1000000)", query)
        self.assertIn("WHERE COALESCE(tank.[TankInstallDate], d.[InitialInstallDate]) IS NOT NULL", query)
        self.assertIn("ORDER BY COALESCE(tank.[TankInstallDate], d.[InitialInstallDate]) DESC, d.[Id] DESC", query)

    def test_sample_limit_rejects_invalid_or_noninteger_values(self):
        for limit in (0, -1, 1_000_001, True, "100; DROP TABLE x"):
            with self.assertRaisesRegex(ValueError, "must be an integer"):
                refresh.sampled_query("SELECT\n    d.[Id]", limit)

    def test_committed_pruning_requires_explicit_sample_and_apply(self):
        for values in ({}, {"apply": False, "sample_installations": 100}):
            arguments = {"apply": True, "prune_first": True, **values}
            with self.assertRaisesRegex(ValueError, "Committed pruning requires"):
                refresh.refresh(**arguments)

    def test_batched_enrichment_requires_explicit_apply(self):
        with self.assertRaisesRegex(ValueError, "Batched promotion requires"):
            refresh.refresh(apply=False, promotion_batch_size=100)

    def test_batched_enrichment_commits_schema_rows_and_verification_separately(self):
        connection = self.connection()
        connection.cursor().fetchone.side_effect = [
            (500,), (100,), ("00000000-0000-0000-0000-000000000001",),
            (90,), (10,), (0,), (410,), (100, 100), (0,), (1,), (1,),
        ]
        with patch("builtins.print"):
            refresh.promote(connection, 100, True, batch_size=100)
        self.assertEqual(connection.commit.call_count, 4)
        connection.rollback.assert_not_called()
        sql = "\n".join(call.args[0] for call in connection.cursor().execute.call_args_list)
        self.assertIn("JOIN #PromotionBatch batch ON batch.Id=source.Id", sql)

    def test_rehearsal_rolls_back_schema_and_reload(self):
        connection = self.connection()
        with patch("builtins.print"):
            refresh.promote(connection, 100, False)
        connection.rollback.assert_called_once()
        connection.commit.assert_not_called()
        sql = "\n".join(call.args[0] for call in connection.cursor().execute.call_args_list)
        self.assertNotIn("TRUNCATE", sql)
        self.assertIn("UPDATE target SET", sql)
        self.assertIn("EXCEPT SELECT target.", sql)
        self.assertIn("INSERT INTO [dbo].[Insight Project]", sql)
        self.assertIn("DELETE target", sql)
        self.assertIn("ALTER COLUMN [Latitude] decimal(9,6)", sql)

    def test_apply_commits_only_after_verification(self):
        connection = self.connection()
        with patch("builtins.print"):
            refresh.promote(connection, 100, True)
        connection.commit.assert_called_once()
        connection.rollback.assert_not_called()

    def test_count_mismatch_rolls_back(self):
        connection = self.connection(actual=99)
        with self.assertRaisesRegex(ValueError, "do not reconcile"):
            refresh.promote(connection, 100, True)
        connection.rollback.assert_called_once()
        connection.commit.assert_not_called()

    def test_unknown_target_column_rejected_before_truncate(self):
        connection = self.connection()
        connection.cursor().fetchall.side_effect = [
            [("Id", "uniqueidentifier", 16, 0, 0)],
            [("Unexpected", "int", 4, 10, 0)],
        ]
        with self.assertRaisesRegex(ValueError, "unexpected target columns"):
            refresh.promote(connection, 100, True)
        sql = "\n".join(call.args[0] for call in connection.cursor().execute.call_args_list)
        self.assertNotIn("TRUNCATE", sql)
        connection.rollback.assert_called_once()
        connection.commit.assert_not_called()
