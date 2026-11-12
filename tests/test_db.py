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
            conn = db.connect_db(
                path=db_path,
                row_factory=sqlite3.Row,
                configure_journal=True,
            )
            self.assertIs(conn.row_factory, sqlite3.Row)
            self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 30000)
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0].lower()
            self.assertEqual(journal_mode, "wal")
            self.assertEqual(conn.execute("PRAGMA synchronous").fetchone()[0], 1)
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
            self.assertEqual(
                database.execute("SELECT Key, value FROM questions").fetchall(),
                [(1, "latest")],
            )
            self.assertEqual(database.execute("SELECT * FROM metar").fetchall(), [("keep",)])
            self.assertEqual(database.execute("SELECT * FROM added_view").fetchall(), [("seed row",)])
            self.assertIsNotNone(
                database.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = 'question_value'"
                ).fetchone()
            )
            database.close()

    def test_initialize_database_syncs_question_source_foreign_key(self):
        with tempfile.TemporaryDirectory() as directory:
            seed_path = Path(directory) / "seed.db"
            database_path = Path(directory) / "data.db"

            seed = sqlite3.connect(seed_path)
            seed.executescript(
                """
                CREATE TABLE questions (
                    Key INTEGER PRIMARY KEY,
                    source TEXT REFERENCES source_material(source)
                );
                INSERT INTO questions VALUES (1, 'manual');
                CREATE TABLE source_material (
                    source TEXT PRIMARY KEY,
                    start_date TEXT,
                    end_date TEXT
                );
                INSERT INTO source_material VALUES ('manual', NULL, NULL);
                """
            )
            seed.close()

            database = sqlite3.connect(database_path)
            database.executescript(
                """
                CREATE TABLE questions (Key INTEGER PRIMARY KEY);
                INSERT INTO questions VALUES (1);
                """
            )
            database.close()

            db.initialize_database(seed_path=seed_path, database_path=database_path)
            db.initialize_database(seed_path=seed_path, database_path=database_path)

            database = db.connect_db(path=database_path)
            try:
                self.assertEqual(
                    database.execute("SELECT source FROM questions").fetchall(),
                    [("manual",)],
                )
                foreign_key = database.execute(
                    "PRAGMA foreign_key_list(questions)"
                ).fetchone()
                self.assertEqual(
                    (foreign_key[2], foreign_key[3], foreign_key[4]),
                    ("source_material", "source", "source"),
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    database.execute(
                        "INSERT INTO questions VALUES (2, 'unknown')"
                    )
                database.execute("INSERT INTO questions VALUES (2, 'manual')")
            finally:
                database.close()

    def test_initialize_database_adds_missing_question_source_column(self):
        with tempfile.TemporaryDirectory() as directory:
            seed_path = Path(directory) / "seed.db"
            database_path = Path(directory) / "data.db"

            seed = sqlite3.connect(seed_path)
            seed.executescript(
                """
                CREATE TABLE questions (Key INTEGER PRIMARY KEY, question TEXT);
                INSERT INTO questions VALUES (1, 'Question');
                CREATE TABLE source_material (
                    source TEXT PRIMARY KEY,
                    start_date TEXT,
                    end_date TEXT
                );
                INSERT INTO source_material VALUES ('manual', NULL, NULL);
                """
            )
            seed.close()

            db.initialize_database(seed_path=seed_path, database_path=database_path)
            db.initialize_database(seed_path=seed_path, database_path=database_path)

            database = db.connect_db(path=database_path)
            try:
                columns = {
                    row[1] for row in database.execute("PRAGMA table_info(questions)")
                }
                self.assertIn("source", columns)
                database.execute(
                    "INSERT INTO questions (Key, question, source) VALUES (?, ?, ?)",
                    (2, "Question with source", "manual"),
                )
            finally:
                database.close()

    def test_initialize_database_migrates_image_keys_and_syncs_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            seed_path = Path(directory) / "seed.db"
            database_path = Path(directory) / "data.db"

            seed = sqlite3.connect(seed_path)
            seed.executescript(
                """
                CREATE TABLE image_source (
                    image_key TEXT PRIMARY KEY,
                    image_full_name TEXT NOT NULL,
                    image_local_source TEXT,
                    image_original_source TEXT,
                    start_date TEXT,
                    end_date TEXT
                );
                INSERT INTO image_source VALUES (
                    '/question_images/example.jpg',
                    'Example image',
                    '/question_images/example.jpg',
                    'https://source.example/image',
                    '2024-01-01',
                    NULL
                );
                CREATE TABLE questions (
                    Key INTEGER PRIMARY KEY,
                    question TEXT,
                    image_key TEXT REFERENCES image_source(image_key)
                );
                INSERT INTO questions VALUES (
                    1,
                    'Question',
                    '/question_images/example.jpg'
                );
                CREATE VIEW question_count AS
                    SELECT COUNT(*) AS total FROM questions;
                """
            )
            seed.close()

            database = sqlite3.connect(database_path)
            database.executescript(
                """
                CREATE TABLE image_source (
                    image_key TEXT PRIMARY KEY,
                    image_full_name TEXT NOT NULL,
                    image_local_source TEXT,
                    image_original_source TEXT,
                    start_date TEXT,
                    end_date TEXT
                );
                INSERT INTO image_source VALUES (
                    'stale',
                    'Stale image',
                    NULL,
                    NULL,
                    NULL,
                    NULL
                );
                CREATE TABLE questions (Key INTEGER PRIMARY KEY, image_link TEXT);
                INSERT INTO questions VALUES (1, '/question_images/old.jpg');
                CREATE VIEW question_count AS
                    SELECT COUNT(*) AS total FROM questions;
                """
            )
            database.close()

            db.initialize_database(seed_path=seed_path, database_path=database_path)
            db.initialize_database(seed_path=seed_path, database_path=database_path)

            database = db.connect_db(path=database_path)
            try:
                question_columns = {
                    row[1] for row in database.execute("PRAGMA table_info(questions)")
                }
                self.assertIn("image_key", question_columns)
                self.assertNotIn("image_link", question_columns)
                self.assertEqual(
                    database.execute(
                        "SELECT image_key FROM questions WHERE Key = 1"
                    ).fetchone()[0],
                    "/question_images/example.jpg",
                )
                self.assertEqual(
                    database.execute(
                        "SELECT image_key, image_full_name, image_local_source, "
                        "image_original_source, start_date, end_date "
                        "FROM image_source"
                    ).fetchall(),
                    [
                        (
                            "/question_images/example.jpg",
                            "Example image",
                            "/question_images/example.jpg",
                            "https://source.example/image",
                            "2024-01-01",
                            None,
                        )
                    ],
                )
                self.assertEqual(
                    database.execute("SELECT total FROM question_count").fetchone()[0],
                    1,
                )
                foreign_key = database.execute(
                    "PRAGMA foreign_key_list(questions)"
                ).fetchone()
                self.assertEqual(
                    (foreign_key[2], foreign_key[3], foreign_key[4]),
                    ("image_source", "image_key", "image_key"),
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    database.execute(
                        "INSERT INTO questions (Key, question, image_key) "
                        "VALUES (2, 'Invalid image', 'missing')"
                    )
            finally:
                database.close()


if __name__ == "__main__":
    unittest.main()
