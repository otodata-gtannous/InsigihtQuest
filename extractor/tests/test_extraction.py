import unittest
from unittest.mock import MagicMock

from insight_project.extraction import EXTRACT_SQL, ExtractionResult, extract_device_locations
from insight_project.migrations import TARGET_DATABASE


def extraction_connection(counts=(0, 1000, 1000), database=TARGET_DATABASE):
    connection = MagicMock()
    connection.autocommit = False
    cursor = connection.cursor.return_value
    cursor.execute.return_value = cursor
    cursor.fetchone.side_effect = [(database,), counts]
    return connection


class ExtractionTests(unittest.TestCase):
    def test_GivenMissingLocations_WhenApplying_ThenCommitsAndReportsCounts(self):
        # Arrange
        connection = extraction_connection()
        # Act
        result = extract_device_locations(connection, apply=True)
        # Assert
        self.assertEqual(result, ExtractionResult(0, 1000, 1000))
        connection.commit.assert_called_once()
        connection.rollback.assert_not_called()
        connection.cursor().execute.assert_any_call(EXTRACT_SQL)

    def test_GivenExistingSample_WhenApplying_ThenReportsNoNewRows(self):
        # Arrange
        connection = extraction_connection(counts=(1000, 0, 1000))
        # Act
        result = extract_device_locations(connection, apply=True)
        # Assert
        self.assertEqual(result.inserted_rows, 0)
        self.assertIn("NOT EXISTS", EXTRACT_SQL)
        self.assertIn("SELECT TOP (1000)", EXTRACT_SQL)
        self.assertIn("ORDER BY [Id]", EXTRACT_SQL)

    def test_GivenMissingLocations_WhenRehearsing_ThenRollsBack(self):
        # Arrange
        connection = extraction_connection()
        # Act
        result = extract_device_locations(connection)
        # Assert
        self.assertEqual(result.inserted_rows, 1000)
        connection.rollback.assert_called_once()
        connection.commit.assert_not_called()

    def test_GivenWrongDatabase_WhenExtracting_ThenRejectsBeforeInserting(self):
        # Arrange
        connection = extraction_connection(database="wrong_database")
        # Act
        with self.assertRaisesRegex(ValueError, "Expected database"):
            extract_device_locations(connection, apply=True)
        # Assert
        connection.rollback.assert_called_once()
        connection.commit.assert_not_called()
        self.assertEqual(connection.cursor().execute.call_count, 1)

    def test_GivenSqlError_WhenExtracting_ThenRollsBackAndPropagates(self):
        # Arrange
        connection = extraction_connection()
        connection.cursor().execute.side_effect = RuntimeError("SQL failure")
        # Act
        with self.assertRaisesRegex(RuntimeError, "SQL failure"):
            extract_device_locations(connection, apply=True)
        # Assert
        connection.rollback.assert_called_once()
        connection.commit.assert_not_called()
        connection.cursor().close.assert_called_once()

    def test_GivenInvalidCounts_WhenExtracting_ThenRollsBack(self):
        # Arrange
        connection = extraction_connection(counts=(0, 1000, 999))
        # Act
        with self.assertRaisesRegex(ValueError, "reconcile"):
            extract_device_locations(connection, apply=True)
        # Assert
        connection.rollback.assert_called_once()
        connection.commit.assert_not_called()

    def test_GivenMissingCounts_WhenExtracting_ThenRollsBack(self):
        # Arrange
        connection = extraction_connection(counts=None)
        # Act
        with self.assertRaisesRegex(ValueError, "verification counts"):
            extract_device_locations(connection, apply=True)
        # Assert
        connection.rollback.assert_called_once()
        connection.commit.assert_not_called()

    def test_GivenAutocommit_WhenExtracting_ThenRejectsUnsafeConnection(self):
        # Arrange
        connection = extraction_connection()
        connection.autocommit = True
        # Act
        with self.assertRaisesRegex(ValueError, "autocommit=False"):
            extract_device_locations(connection, apply=True)
        # Assert
        connection.cursor.assert_not_called()
