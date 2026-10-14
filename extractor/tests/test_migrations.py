from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock

from insight_project.migrations import (
    Migration, TARGET_DATABASE, load_migrations, migrate, pending_migrations,
)


def connection_with_history(history=(), database=TARGET_DATABASE):
    connection = MagicMock()
    connection.autocommit = False
    cursor = connection.cursor.return_value
    cursor.execute.return_value = cursor
    cursor.fetchone.return_value = (database,)
    cursor.fetchall.return_value = history
    return connection


class MigrationTests(unittest.TestCase):
    def test_GivenPackagedSql_WhenLoading_ThenFindsGuidBaseline(self):
        # Arrange
        expected_names = [
            "0001_create_insight_project.sql", "0002_add_device_coordinates.sql"
        ]
        # Act
        migrations = load_migrations()
        # Assert
        self.assertEqual([migration.name for migration in migrations], expected_names)
        self.assertIn("[Id] UNIQUEIDENTIFIER NOT NULL", migrations[0].sql)
        self.assertEqual(len(migrations[0].checksum), 64)

    def test_GivenDuplicateVersions_WhenLoading_ThenRejectsFiles(self):
        # Arrange
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "0001_first.sql").write_text("SELECT 1;", encoding="utf-8")
            (root / "0001_second.sql").write_text("SELECT 2;", encoding="utf-8")
            # Act
            with self.assertRaisesRegex(ValueError, "duplicate"):
                load_migrations(root)
            # Assert
            self.assertEqual(len(list(root.glob("*.sql"))), 2)

    def test_GivenDifferentLineEndings_WhenLoading_ThenChecksumsMatch(self):
        # Arrange
        with TemporaryDirectory() as lf_directory, TemporaryDirectory() as crlf_directory:
            lf_root, crlf_root = Path(lf_directory), Path(crlf_directory)
            (lf_root / "0001_first.sql").write_bytes(b"SELECT 1;\n")
            (crlf_root / "0001_first.sql").write_bytes(b"SELECT 1;\r\n")
            expected = load_migrations(lf_root)[0].checksum
            # Act
            actual = load_migrations(crlf_root)[0].checksum
            # Assert
            self.assertEqual(actual, expected)

    def test_GivenGoBatch_WhenLoading_ThenRejectsUnsupportedSql(self):
        # Arrange
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "0001_first.sql").write_text("SELECT 1;\nGO\n", encoding="utf-8")
            # Act
            with self.assertRaisesRegex(ValueError, "GO"):
                load_migrations(root)
            # Assert
            self.assertTrue((root / "0001_first.sql").exists())

    def test_GivenEditedMigration_WhenPlanning_ThenRejectsChecksumChange(self):
        # Arrange
        migration = Migration(1, "0001_first.sql", "SELECT 1;", "new-checksum")
        applied = {1: (migration.name, "old-checksum")}
        # Act
        with self.assertRaisesRegex(ValueError, "modified") as raised:
            pending_migrations([migration], applied)
        # Assert
        self.assertIn(migration.name, str(raised.exception))

    def test_GivenMissingMigration_WhenPlanning_ThenRejectsUnknownVersion(self):
        # Arrange
        migration = Migration(1, "0001_first.sql", "SELECT 1;", "checksum")
        # Act
        with self.assertRaisesRegex(ValueError, "missing") as raised:
            pending_migrations([migration], {2: ("0002_missing.sql", "checksum")})
        # Assert
        self.assertIn("versions", str(raised.exception))

    def test_GivenHistoryGap_WhenPlanning_ThenRejectsOutOfOrderMigration(self):
        # Arrange
        first = Migration(1, "0001_first.sql", "SELECT 1;", "first")
        second = Migration(2, "0002_second.sql", "SELECT 2;", "second")
        # Act
        with self.assertRaisesRegex(ValueError, "gap") as raised:
            pending_migrations([first, second], {2: (second.name, second.checksum)})
        # Assert
        self.assertIn("out-of-order", str(raised.exception))

    def test_GivenNewDatabase_WhenRehearsing_ThenRollsBack(self):
        # Arrange
        connection = connection_with_history()
        # Act
        names = migrate(connection)
        # Assert
        self.assertEqual(names, [migration.name for migration in load_migrations()])
        connection.rollback.assert_called_once()
        connection.commit.assert_not_called()

    def test_GivenNewDatabase_WhenApplying_ThenCommitsBaseline(self):
        # Arrange
        connection = connection_with_history()
        # Act
        names = migrate(connection, apply=True)
        # Assert
        self.assertEqual(names, [migration.name for migration in load_migrations()])
        connection.commit.assert_called_once()
        connection.rollback.assert_not_called()

    def test_GivenAppliedBaseline_WhenApplyingAgain_ThenSkipsMigration(self):
        # Arrange
        migrations = load_migrations()
        connection = connection_with_history(
            [(migration.version, migration.name, migration.checksum) for migration in migrations]
        )
        # Act
        names = migrate(connection, apply=True)
        # Assert
        self.assertEqual(names, [])
        statements = [call.args[0] for call in connection.cursor().execute.call_args_list]
        for migration in migrations:
            self.assertNotIn(migration.sql, statements)

    def test_GivenWrongDatabase_WhenApplying_ThenRejectsBeforeSchemaChanges(self):
        # Arrange
        connection = connection_with_history(database="another_database")
        # Act
        with self.assertRaisesRegex(ValueError, "Expected database"):
            migrate(connection, apply=True)
        # Assert
        connection.commit.assert_not_called()
        connection.rollback.assert_called_once()
        self.assertEqual(connection.cursor().execute.call_count, 1)

    def test_GivenSqlFailure_WhenApplying_ThenRollsBackAndPropagates(self):
        # Arrange
        connection = connection_with_history()
        connection.cursor().execute.side_effect = RuntimeError("Simulated SQL failure")
        # Act
        with self.assertRaisesRegex(RuntimeError, "Simulated SQL failure"):
            migrate(connection, apply=True)
        # Assert
        connection.rollback.assert_called_once()
        connection.commit.assert_not_called()
        connection.cursor().close.assert_called_once()

    def test_GivenAutocommitConnection_WhenApplying_ThenRejectsUnsafeConnection(self):
        # Arrange
        connection = connection_with_history()
        connection.autocommit = True
        # Act
        with self.assertRaisesRegex(ValueError, "autocommit=False"):
            migrate(connection, apply=True)
        # Assert
        connection.cursor.assert_not_called()

    def test_GivenLaterSqlFailure_WhenApplying_ThenRollsBackEntireBatch(self):
        # Arrange
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "0001_first.sql").write_text("SELECT 1;", encoding="utf-8")
            (root / "0002_second.sql").write_text("SELECT 2;", encoding="utf-8")
            connection = connection_with_history()
            cursor = connection.cursor()

            def execute(sql, *parameters):
                if sql == "SELECT 2;":
                    raise RuntimeError("Second migration failed")
                return cursor

            cursor.execute.side_effect = execute
            # Act
            with self.assertRaisesRegex(RuntimeError, "Second migration failed"):
                migrate(connection, apply=True, directory=root)
            # Assert
            cursor.execute.assert_any_call("SELECT 1;")
            connection.rollback.assert_called_once()
            connection.commit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
