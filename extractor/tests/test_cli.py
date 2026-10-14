from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import unittest
from unittest.mock import MagicMock, patch

import pyodbc

from insight_project.__main__ import get_connection_string, main
from insight_project.extraction import ExtractionResult


class CliTests(unittest.TestCase):
    def test_GivenNoArguments_WhenLaunching_ThenAppliesMigrationsAndExtracts(self):
        # Arrange
        connection = MagicMock()
        output = StringIO()
        with patch("sys.argv", ["insight-project"]), \
                patch("insight_project.__main__.get_connection_string", return_value="configured"), \
                patch("insight_project.__main__.pyodbc.connect", return_value=connection), \
                patch("insight_project.__main__.migrate", return_value=[]) as migration, \
                patch("insight_project.__main__.extract_device_locations",
                      return_value=ExtractionResult(0, 1000, 1000)) as extraction, \
                redirect_stdout(output):
            # Act
            code = main()
            # Assert
            self.assertEqual(code, 0)
            migration.assert_called_once_with(connection, apply=True)
            extraction.assert_called_once_with(connection, apply=True)
            self.assertIn("1000 inserted", output.getvalue())
            connection.close.assert_called_once()

    def test_GivenMigrateCommand_WhenLaunching_ThenDoesNotExtract(self):
        # Arrange
        connection = MagicMock()
        with patch("sys.argv", ["insight-project", "migrate"]), \
                patch("insight_project.__main__.get_connection_string", return_value="configured"), \
                patch("insight_project.__main__.pyodbc.connect", return_value=connection), \
                patch("insight_project.__main__.migrate", return_value=[]) as migration, \
                patch("insight_project.__main__.extract_device_locations") as extraction, \
                redirect_stdout(StringIO()):
            # Act
            code = main()
            # Assert
            self.assertEqual(code, 0)
            migration.assert_called_once_with(connection, apply=False)
            extraction.assert_not_called()

    def test_GivenExtractionFailure_WhenLaunching_ThenReportsFailureWithoutCredentials(self):
        # Arrange
        error_output, output = StringIO(), StringIO()
        with patch("sys.argv", ["insight-project"]), \
                patch("insight_project.__main__.get_connection_string", return_value="configured"), \
                patch("insight_project.__main__.pyodbc.connect"), \
                patch("insight_project.__main__.migrate", return_value=[]), \
                patch("insight_project.__main__.extract_device_locations",
                      side_effect=pyodbc.Error("42000", "private connection details")), \
                redirect_stderr(error_output), redirect_stdout(output):
            # Act
            code = main()
            # Assert
            self.assertEqual(code, 1)
            self.assertIn("42000", error_output.getvalue())
            self.assertNotIn("private connection details", error_output.getvalue())
            self.assertNotIn("Extraction committed", output.getvalue())

    def test_GivenConfiguredConnection_WhenConnecting_ThenSkipsCredentialPrompt(self):
        # Arrange
        with patch.dict("os.environ", {"INSIGHT_PROJECT_CONNECTION_STRING": "configured"}), \
                patch("insight_project.__main__.getpass") as prompt:
            # Act
            result = get_connection_string()
            # Assert
            self.assertEqual(result, "configured")
            prompt.assert_not_called()

    def test_GivenSpecialCharacters_WhenPrompting_ThenEscapesOdbcValues(self):
        # Arrange
        with patch.dict("os.environ", {}, clear=True), \
                patch("sys.stdin.isatty", return_value=True), \
                patch("builtins.input", return_value="user};value"), \
                patch("insight_project.__main__.getpass", return_value="example};value"):
            # Act
            result = get_connection_string()
            # Assert
            self.assertIn("UID={user}};value};", result)
            self.assertIn("PWD={example}};value};", result)
            self.assertIn("Encrypt=yes;TrustServerCertificate=no;", result)
