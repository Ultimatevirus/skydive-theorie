"""Shared SQLite path and connection helpers."""

import os
import sqlite3
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_LOCAL_DB = BASE_DIR / "data.db"
PRODUCTION_DB = Path("/data/data.db")


def _quote_identifier(identifier):
    return '"{}"'.format(str(identifier).replace('"', '""'))


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
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _schema_objects(conn, database):
    return {
        (row[0], row[1]): {"table": row[2], "sql": row[3]}
        for row in conn.execute(
            f"SELECT type, name, tbl_name, sql FROM {database}.sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' AND sql IS NOT NULL"
        )
    }


def _drop_object(conn, object_type, name):
    conn.execute(
        f"DROP {object_type.upper()} {_quote_identifier(name)}"
    )


def _copy_table_rows(conn, table_name, source_database="seed"):
    quoted_table = _quote_identifier(table_name)
    columns = [
        row[1]
        for row in conn.execute(
            f"PRAGMA {source_database}.table_info({quoted_table})"
        )
    ]
    quoted_columns = ", ".join(_quote_identifier(column) for column in columns)
    placeholders = ", ".join("?" for _ in columns)
    rows = conn.execute(
        f"SELECT {quoted_columns} FROM {source_database}.{quoted_table}"
    )
    conn.executemany(
        f"INSERT INTO {quoted_table} ({quoted_columns}) VALUES ({placeholders})",
        rows,
    )


def initialize_database(seed_path=None, database_path=None):
    """Merge the image seed database into the persistent runtime database."""
    seed_path = Path(seed_path or DEFAULT_LOCAL_DB)
    if not seed_path.exists():
        raise FileNotFoundError(f"Database seed not found: {seed_path}")
    target_path = Path(database_path or get_db_path())
    if target_path.resolve() == seed_path.resolve():
        return

    conn = connect_db(path=target_path)
    try:
        conn.execute("ATTACH DATABASE ? AS seed", (str(seed_path),))
        seed_objects = _schema_objects(conn, "seed")
        target_objects = _schema_objects(conn, "main")
        replaced_tables = set()

        conn.execute("BEGIN IMMEDIATE")

        for (object_type, name), seed_object in seed_objects.items():
            if object_type != "table":
                continue

            sql = seed_object["sql"]
            existing_object = target_objects.get((object_type, name))
            existing_sql = existing_object["sql"] if existing_object else None
            replace_table = name == "questions" or (
                existing_sql is not None and existing_sql != sql
            )
            if replace_table and existing_sql is not None:
                replaced_tables.add(name)
                for (existing_type, existing_name), existing_object in list(target_objects.items()):
                    if existing_type == "index" and existing_name.startswith("sqlite_autoindex"):
                        continue
                    if existing_type == "index":
                        if existing_object["table"] == name:
                            _drop_object(conn, existing_type, existing_name)
                _drop_object(conn, "table", name)
            if replace_table or existing_sql is None:
                conn.execute(sql)
                _copy_table_rows(conn, name)

        for (object_type, name), seed_object in seed_objects.items():
            if object_type == "table":
                continue
            sql = seed_object["sql"]
            existing_object = target_objects.get((object_type, name))
            existing_sql = existing_object["sql"] if existing_object else None
            table_was_replaced = (
                object_type == "index"
                and seed_object["table"] in replaced_tables
            )
            if existing_sql == sql and not table_was_replaced:
                continue
            if existing_sql is not None:
                _drop_object(conn, object_type, name)
            conn.execute(sql)

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    initialize_database()
