import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import db


class DatabaseHelperTests(unittest.TestCase):
    def test_get_db_path_prefers_configured_path(self):
        with patch.dict(os.environ, {"DB_PATH": "/tmp/custom-skydive.db"}, clear=False):
            self.assertEqual(db.get_db_path(), "/tmp/custom-skydive.db")

    def test_connect_db_applies_row_factory_and_sqlite_pragmas(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as temp_db:
            db_path = temp_db.name

        try:
            conn = db.connect_db(path=db_path, row_factory=sqlite3.Row)
            self.assertIs(conn.row_factory, sqlite3.Row)
            self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 30000)
            journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0].lower()
            self.assertEqual(journal_mode, "wal")
            conn.close()
        finally:
            if os.path.exists(db_path):
                os.remove(db_path)

    def test_initialize_database_syncs_questions_and_merges_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            seed_path = Path(directory) / "seed.db"
            database_path = Path(directory) / "data.db"

            seed = sqlite3.connect(seed_path)
            seed.executescript(
                """
                CREATE TABLE questions (Key INTEGER PRIMARY KEY, value TEXT);
                INSERT INTO questions VALUES (1, 'latest');
                CREATE INDEX question_value ON questions (value);
                CREATE TABLE added (value TEXT);
                INSERT INTO added VALUES ('seed row');
                CREATE VIEW added_view AS SELECT value FROM added;
                """
            )
            seed.close()

            database = sqlite3.connect(database_path)
            database.executescript(
                """
                CREATE TABLE questions (Key INTEGER PRIMARY KEY, value TEXT);
                INSERT INTO questions VALUES (1, 'stale');
                CREATE TABLE metar (value TEXT);
                INSERT INTO metar VALUES ('keep');
                """
            )
            database.close()

            db.initialize_database(seed_path=seed_path, database_path=database_path)
            db.initialize_database(seed_path=seed_path, database_path=database_path)

            database = sqlite3.connect(database_path)
            self.assertEqual(database.execute("SELECT * FROM questions").fetchall(), [(1, "latest")])
            self.assertEqual(database.execute("SELECT * FROM metar").fetchall(), [("keep",)])
            self.assertEqual(database.execute("SELECT * FROM added_view").fetchall(), [("seed row",)])
            self.assertIsNotNone(
                database.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = 'question_value'"
                ).fetchone()
            )
            database.close()


if __name__ == "__main__":
    unittest.main()
