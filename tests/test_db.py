import os
import sqlite3
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
