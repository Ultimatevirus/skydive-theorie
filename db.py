"""Shared SQLite path and connection helpers."""

import os
import sqlite3
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_LOCAL_DB = BASE_DIR / "data.db"
PRODUCTION_DB = Path("/data/data.db")


def get_db_path():
    """Resolve a stable SQLite database path for local and container use."""
    configured_path = os.getenv("DB_PATH")
    if configured_path:
        if configured_path.startswith("/") and os.name == "nt":
            return configured_path
        db_path = Path(configured_path)
    elif PRODUCTION_DB.exists():
        db_path = PRODUCTION_DB
    elif PRODUCTION_DB.parent.exists():
        db_path = PRODUCTION_DB
        if DEFAULT_LOCAL_DB.exists() and not PRODUCTION_DB.exists():
            try:
                PRODUCTION_DB.write_bytes(DEFAULT_LOCAL_DB.read_bytes())
            except OSError:
                pass
    else:
        db_path = DEFAULT_LOCAL_DB

    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return str(db_path)


def connect_db(path=None, row_factory=None):
    """Open a configured SQLite connection with consistent busy handling."""
    conn = sqlite3.connect(path or get_db_path(), timeout=30)
    if row_factory is not None:
        conn.row_factory = row_factory
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn
