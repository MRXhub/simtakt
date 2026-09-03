#!/usr/bin/env python3
"""v15 -> v16 schema migration checks for the wall_budget_json column."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from control_plane.data.sqlite_evaluation_repository import (
    SCHEMA_VERSION,
    SQLiteEvaluationRepository,
)


class WallBudgetMigrationTests(unittest.TestCase):
    def test_v15_to_v16_migration_adds_column_and_preserves_bookkeeping(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        database = Path(temp.name) / "control.sqlite3"
        # Build a current schema, then step it back to a faithful v15 snapshot.
        SQLiteEvaluationRepository(database)
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("ALTER TABLE attempts DROP COLUMN wall_budget_json")
            connection.execute("DELETE FROM schema_migrations WHERE version = 16")
            connection.executemany(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) "
                "VALUES (?, ?)",
                [(version, "2026-08-02T00:00:00+00:00") for version in (12, 13, 14, 15)],
            )
            connection.commit()
        with closing(sqlite3.connect(database)) as connection:
            attempt_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(attempts)")
            }
            versions = {
                row[0]
                for row in connection.execute("SELECT version FROM schema_migrations")
            }
        self.assertNotIn("wall_budget_json", attempt_columns)
        self.assertEqual(versions, {12, 13, 14, 15})

        SQLiteEvaluationRepository(database)

        with closing(sqlite3.connect(database)) as connection:
            attempt_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(attempts)")
            }
            evaluation_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(evaluations)")
            }
            versions = {
                row[0]
                for row in connection.execute("SELECT version FROM schema_migrations")
            }
        # The new column is added and the ledger is preserved (old rows kept,
        # v16 appended) -- no bookkeeping row is lost.
        self.assertIn("wall_budget_json", attempt_columns)
        for version in (12, 13, 14, 15, 16):
            self.assertIn(version, versions)
        # Prior releases' columns are retained (additive migration).
        for column in ("termination_state", "execution_plan_json", "session_ref"):
            self.assertIn(column, attempt_columns)
        self.assertIn("origin", evaluation_columns)
        self.assertEqual(SCHEMA_VERSION, 16)

        # Reopening the already-migrated database is idempotent.
        SQLiteEvaluationRepository(database)


if __name__ == "__main__":
    unittest.main()
