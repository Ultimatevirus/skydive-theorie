import copy
import json
import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import db
from question_repository import QuestionRepository


class DatabaseHelperTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "data.db"
        self.seed_path = Path(self.directory.name) / "questions.json"
        self.seed = json.loads(db.DEFAULT_QUESTION_SEED.read_text(encoding="utf-8"))
        self.write_seed()

    def write_seed(self):
        self.seed_path.write_text(json.dumps(self.seed, ensure_ascii=False), encoding="utf-8")

    def initialize(self):
        db.initialize_database(seed_path=self.seed_path, database_path=self.path)

    def test_get_db_path_prefers_configured_path(self):
        with patch.dict(os.environ, {"DB_PATH": "/tmp/custom-skydive.db"}, clear=False):
            self.assertEqual(db.get_db_path(), "/tmp/custom-skydive.db")

    def test_connect_db_applies_row_factory_and_sqlite_pragmas(self):
        conn = db.connect_db(path=self.path, row_factory=sqlite3.Row, configure_journal=True)
        self.addCleanup(conn.close)
        self.assertIs(conn.row_factory, sqlite3.Row)
        self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 30000)
        self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
        self.assertEqual(conn.execute("PRAGMA synchronous").fetchone()[0], 1)

    def test_connect_db_honors_custom_busy_timeout(self):
        conn = db.connect_db(path=self.path, timeout=1)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 1000)

    def test_fresh_database_matches_all_seed_fields_and_relationships(self):
        self.initialize()
        conn = db.connect_db(path=self.path, row_factory=sqlite3.Row)
        self.addCleanup(conn.close)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(questions)")}
        self.assertNotIn("question_is_expression", columns)
        self.assertNotIn("false_is_expression", columns)
        self.assertTrue(all(
            "question_is_expression" not in question
            and "false_is_expression" not in question
            for question in self.seed["questions"]
        ))
        rows = conn.execute("SELECT * FROM questions ORDER BY Key").fetchall()
        self.assertEqual(len(rows), len(self.seed["questions"]))
        for row, question in zip(rows, self.seed["questions"]):
            for field in db.QUESTION_FIELDS:
                self.assertEqual(row[field], question[field])
            self.assertEqual(
                row["image_key"], question["image"]["image_key"] if question["image"] else None
            )
            self.assertEqual(
                row["source"],
                question["source_material"]["source"] if question["source_material"] else None,
            )
        sources = [dict(row) for row in conn.execute("SELECT * FROM source_material ORDER BY source")]
        self.assertEqual(sources, self.seed["source_material"])
        images = {
            question["image"]["image_key"]: question["image"]
            for question in self.seed["questions"] if question["image"]
        }
        self.assertEqual(
            {row["image_key"]: dict(row) for row in conn.execute("SELECT * FROM image_source")},
            images,
        )
        metar_seed = json.loads(db.DEFAULT_METAR_SEED.read_text(encoding="utf-8"))
        for table in ("metar_weather_codes", "metar_abbreviations"):
            self.assertEqual(
                [dict(row) for row in conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid')],
                metar_seed[table],
            )
        self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(len(conn.execute("PRAGMA foreign_key_list(questions)").fetchall()), 2)

    def test_repeated_sync_applies_updates_and_deletes_preserving_runtime_data(self):
        self.initialize()
        conn = db.connect_db(path=self.path)
        conn.execute("CREATE TABLE runtime_data (value TEXT)")
        conn.execute("INSERT INTO runtime_data VALUES ('preserved')")
        conn.execute("CREATE VIEW custom_question_count AS SELECT COUNT(*) AS n FROM questions")
        conn.execute("CREATE INDEX custom_question_topic ON questions(topic)")
        conn.execute("UPDATE metar_weather_codes SET NL_definition='local' WHERE METAR_abreviation='RA'")
        conn.execute("DELETE FROM metar_weather_codes WHERE METAR_abreviation='SN'")
        conn.commit()
        conn.close()

        removed = self.seed["questions"].pop()
        updated = self.seed["questions"][0]
        updated["question"] = "Updated question"
        new_question = copy.deepcopy(updated)
        new_question["Key"] = max(q["Key"] for q in self.seed["questions"]) + 100
        self.seed["questions"].append(new_question)
        source = self.seed["source_material"][0]
        source["end_date"] = "2099-01-01"
        for question in self.seed["questions"]:
            if question["source_material"] and question["source_material"]["source"] == source["source"]:
                question["source_material"] = copy.deepcopy(source)
        image = next(q["image"] for q in self.seed["questions"] if q["image"])
        image_key = image["image_key"]
        for question in self.seed["questions"]:
            if question["image"] and question["image"]["image_key"] == image_key:
                question["image"]["image_full_name"] = "Updated image"
        self.write_seed()
        self.initialize()
        self.initialize()

        conn = db.connect_db(path=self.path)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SELECT value FROM runtime_data").fetchone(), ("preserved",))
        self.assertEqual(conn.execute("SELECT question FROM questions WHERE Key=?", (updated["Key"],)).fetchone(), ("Updated question",))
        self.assertIsNone(conn.execute("SELECT 1 FROM questions WHERE Key=?", (removed["Key"],)).fetchone())
        self.assertIsNotNone(conn.execute("SELECT 1 FROM questions WHERE Key=?", (new_question["Key"],)).fetchone())
        self.assertEqual(conn.execute("SELECT end_date FROM source_material WHERE source=?", (source["source"],)).fetchone(), ("2099-01-01",))
        self.assertEqual(conn.execute("SELECT image_full_name FROM image_source WHERE image_key=?", (image_key,)).fetchone(), ("Updated image",))
        self.assertEqual(conn.execute("SELECT n FROM custom_question_count").fetchone()[0], len(self.seed["questions"]))
        self.assertIsNotNone(conn.execute("SELECT 1 FROM sqlite_master WHERE name='custom_question_topic'").fetchone())
        self.assertEqual(conn.execute("SELECT NL_definition FROM metar_weather_codes WHERE METAR_abreviation='RA'").fetchone(), ("local",))
        self.assertIsNotNone(conn.execute("SELECT 1 FROM metar_weather_codes WHERE METAR_abreviation='SN'").fetchone())

    def test_migrates_older_questions_and_preserves_existing_reference_rows(self):
        conn = sqlite3.connect(self.path)
        conn.executescript("""
            CREATE TABLE questions (Key INTEGER PRIMARY KEY, image_link TEXT);
            INSERT INTO questions VALUES (1, 'old');
            CREATE VIEW custom_view AS SELECT COUNT(*) AS n FROM questions;
            CREATE TABLE metar (datetime TEXT, airport TEXT, metar TEXT);
            INSERT INTO metar VALUES ('today', 'EHAM', 'saved');
            CREATE TABLE metar_abbreviations (
                METAR_abreviation TEXT PRIMARY KEY, category TEXT NOT NULL,
                NL_definition TEXT NOT NULL, EN_definition TEXT NOT NULL
            );
            INSERT INTO metar_abbreviations VALUES ('TAF', 'custom', 'local', 'local');
        """)
        conn.close()
        self.initialize()
        conn = db.connect_db(path=self.path)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SELECT * FROM metar").fetchone(), ("today", "EHAM", "saved"))
        self.assertEqual(conn.execute("SELECT category, example FROM metar_abbreviations WHERE METAR_abreviation='TAF'").fetchone(), ("custom", ""))
        self.assertEqual(conn.execute("SELECT n FROM custom_view").fetchone()[0], len(self.seed["questions"]))
        columns = {row[1] for row in conn.execute("PRAGMA table_info(questions)")}
        self.assertIn("image_key", columns)
        self.assertIn("source", columns)
        self.assertNotIn("image_link", columns)

    def test_invalid_seed_and_failed_sync_leave_database_unchanged(self):
        self.initialize()
        conn = db.connect_db(path=self.path)
        before = conn.execute("SELECT * FROM questions ORDER BY Key").fetchall()
        conn.close()
        self.seed["questions"].append(copy.deepcopy(self.seed["questions"][0]))
        self.write_seed()
        with self.assertRaisesRegex(ValueError, "duplicate question"):
            self.initialize()
        self.seed["questions"].pop()
        self.seed["questions"][0]["question"] = "Should roll back"
        self.write_seed()
        with patch.object(db, "_initialize_metar_reference", side_effect=sqlite3.OperationalError("test failure")):
            with self.assertRaisesRegex(sqlite3.OperationalError, "test failure"):
                self.initialize()
        conn = db.connect_db(path=self.path)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SELECT * FROM questions ORDER BY Key").fetchall(), before)
        self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_conflicting_nested_metadata_is_rejected(self):
        question = next(q for q in self.seed["questions"] if q["source_material"])
        question["source_material"]["end_date"] = "conflicting"
        self.write_seed()
        with self.assertRaisesRegex(ValueError, "conflicting source metadata"):
            self.initialize()
        self.assertFalse(self.path.exists())

    def test_concurrent_initializers_serialize(self):
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(self.initialize) for _ in range(2)]
            for future in futures:
                future.result()
        conn = db.connect_db(path=self.path)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0], len(self.seed["questions"]))
        self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_question_queries_do_not_read_seed_after_initialization(self):
        self.initialize()
        repository = QuestionRepository(
            lambda: db.connect_db(path=self.path, row_factory=sqlite3.Row),
            lambda: str(self.path),
        )
        with patch.object(db, "_load_json", side_effect=AssertionError("Unexpected seed read")):
            self.assertTrue(repository.fetch_questions("A", 5))


if __name__ == "__main__":
    unittest.main()
