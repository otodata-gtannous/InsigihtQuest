import importlib.util
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch


SPEC = importlib.util.spec_from_file_location(
    "azure_pilot", Path(__file__).resolve().parents[1] / "scripts" / "pilot_azure_flat_table.py"
)
assert SPEC is not None and SPEC.loader is not None
pilot = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pilot)


class AzurePilotTests(unittest.TestCase):
    def settings(self):
        return {
            "IQ_DB_SERVER": pilot.TARGET_SERVER,
            "IQ_DB_NAME": pilot.TARGET_DATABASE,
            "IQ_DB_USER": "test-user",
            "IQ_DB_PASSWORD": "test};password",
        }

    def test_rejects_other_database_before_connecting(self):
        settings = self.settings()
        settings["IQ_DB_NAME"] = "other"
        with patch.object(pilot, "dotenv_values", return_value=settings), \
                patch.object(pilot.pyodbc, "connect") as connect:
            with self.assertRaisesRegex(ValueError, "approved Azure target"):
                pilot.main()
            connect.assert_not_called()

    def test_success_samples_and_always_rolls_back(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value
        cursor.execute.return_value = cursor
        cursor.fetchone.side_effect = [(pilot.TARGET_DATABASE,), (100, 100, 94, 97, 76)]
        with patch.object(pilot, "dotenv_values", return_value=self.settings()), \
                patch.object(pilot.pyodbc, "connect", return_value=connection) as connect, \
                patch("builtins.print"):
            pilot.main()
        sql = "\n".join(call.args[0] for call in cursor.execute.call_args_list)
        self.assertIn("TOP (100)", sql)
        self.assertIn("INTO #AzureDevicePilot", sql)
        self.assertNotIn("ALTER TABLE [dbo]", sql)
        self.assertIn("PWD={test}};password}", connect.call_args.args[0])
        connection.rollback.assert_called_once()
        connection.commit.assert_not_called()
        connection.close.assert_called_once()

    def test_failed_validation_also_rolls_back(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value
        cursor.execute.return_value = cursor
        cursor.fetchone.side_effect = [(pilot.TARGET_DATABASE,), (99, 99, 94, 97, 76)]
        with patch.object(pilot, "dotenv_values", return_value=self.settings()), \
                patch.object(pilot.pyodbc, "connect", return_value=connection):
            with self.assertRaisesRegex(ValueError, "exactly 100"):
                pilot.main()
        connection.rollback.assert_called_once()
        connection.commit.assert_not_called()
        connection.close.assert_called_once()
