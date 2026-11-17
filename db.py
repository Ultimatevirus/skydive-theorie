"""Shared SQLite connections and initialization from versioned seed data."""

import json
import os
import sqlite3
import time
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_LOCAL_DB = BASE_DIR / "data.db"
PRODUCTION_DB = Path("/data/data.db")
DEFAULT_QUESTION_SEED = BASE_DIR / "seed" / "questions.json"
DEFAULT_METAR_SEED = BASE_DIR / "seed" / "metar_reference.json"

QUESTION_FIELDS = (
    "Key", "level", "topic", "subtopic", "question", "true_answer",
    "false_answers", "is_active", "language",
)
IMAGE_FIELDS = (
    "image_key", "image_full_name", "image_local_source",
    "image_original_source", "start_date", "end_date",
)
SOURCE_FIELDS = ("source", "start_date", "end_date")
WEATHER_FIELDS = ("METAR_abreviation", "NL_definition", "EN_definition")
ABBREVIATION_FIELDS = (
    "METAR_abreviation", "category", "NL_definition", "EN_definition", "example",
)


def get_db_path():
    """Resolve a stable SQLite database path for local and container use."""
    configured_path = os.getenv("DB_PATH")
    if configured_path:
        if configured_path.startswith("/") and os.name == "nt":
            return configured_path
        db_path = Path(configured_path)
    elif PRODUCTION_DB.exists() or PRODUCTION_DB.parent.exists():
        db_path = PRODUCTION_DB
    else:
        db_path = DEFAULT_LOCAL_DB

    db_path.parent.mkdir(parents=True, exist_ok=True)
    return str(db_path)


def connect_db(path=None, row_factory=None, configure_journal=False):
    """Open a configured SQLite connection with consistent busy handling."""
    conn = sqlite3.connect(path or get_db_path(), timeout=30)
    try:
        if row_factory is not None:
            conn.row_factory = row_factory
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        if configure_journal:
            deadline = time.monotonic() + 30
            while True:
                try:
                    mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
                    if mode.lower() != "wal":
                        raise sqlite3.OperationalError(f"Could not enable WAL mode: {mode}")
                    break
                except sqlite3.OperationalError as exc:
                    error_code = getattr(exc, "sqlite_errorcode", 0) & 0xFF
                    if (
                        error_code not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
                        or time.monotonic() >= deadline
                    ):
                        raise
                    time.sleep(0.05)
        conn.execute("PRAGMA synchronous=NORMAL")
    except Exception:
        conn.close()
        raise
    return conn


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON field: {key}")
        result[key] = value
    return result


def _load_json(path):
    with Path(path).open(encoding="utf-8") as seed:
        return json.load(seed, object_pairs_hook=_unique_object)


def _validate_record(record, fields, label, required=(), integers=()):
    if not isinstance(record, dict) or set(record) != set(fields):
        raise ValueError(f"{label} must contain exactly: {', '.join(fields)}")
    for field in fields:
        value = record[field]
        if field in required and (value is None or value == ""):
            raise ValueError(f"{label}.{field} is required")
        if field in integers:
            if value is not None and type(value) is not int:
                raise ValueError(f"{label}.{field} must be an integer or null")
        elif value is not None and not isinstance(value, str):
            raise ValueError(f"{label}.{field} must be a string or null")


def _validate_seed(question_seed, metar_seed):
    if not isinstance(question_seed, dict) or set(question_seed) != {
        "questions", "source_material"
    }:
        raise ValueError("Question seed must contain questions and source_material")
    if not all(isinstance(question_seed[name], list) for name in question_seed):
        raise ValueError("Question seed collections must be lists")

    sources = {}
    for source in question_seed["source_material"]:
        _validate_record(source, SOURCE_FIELDS, "source_material", required=("source",))
        key = source["source"]
        if key in sources:
            raise ValueError(f"Duplicate source_material: {key}")
        sources[key] = source

    images = {}
    question_ids = set()
    for question in question_seed["questions"]:
        fields = QUESTION_FIELDS + ("image", "source_material")
        if not isinstance(question, dict) or set(question) != set(fields):
            raise ValueError(f"Question must contain exactly: {', '.join(fields)}")
        values = {field: question[field] for field in QUESTION_FIELDS}
        _validate_record(
            values, QUESTION_FIELDS, "question", required=("Key",),
            integers=("Key", "is_active"),
        )
        key = question["Key"]
        if key <= 0 or key in question_ids:
            raise ValueError(f"Invalid or duplicate question Key: {key}")
        question_ids.add(key)

        image = question["image"]
        if image is not None:
            _validate_record(
                image, IMAGE_FIELDS, "image",
                required=("image_key", "image_full_name"),
            )
            image_key = image["image_key"]
            if image_key in images and images[image_key] != image:
                raise ValueError(f"Conflicting image metadata: {image_key}")
            images[image_key] = image
        source = question["source_material"]
        if source is not None:
            _validate_record(source, SOURCE_FIELDS, "question.source_material", required=("source",))
            if sources.get(source["source"]) != source:
                raise ValueError(f"Missing or conflicting source metadata: {source['source']}")

    if not isinstance(metar_seed, dict) or set(metar_seed) != {
        "metar_weather_codes", "metar_abbreviations"
    }:
        raise ValueError("METAR seed must contain weather codes and abbreviations")
    for name, fields in (
        ("metar_weather_codes", WEATHER_FIELDS),
        ("metar_abbreviations", ABBREVIATION_FIELDS),
    ):
        records = metar_seed[name]
        if not isinstance(records, list):
            raise ValueError(f"{name} must be a list")
        keys = set()
        for record in records:
            required = fields[:-1] if name == "metar_abbreviations" else fields
            _validate_record(record, fields, name, required=required)
            if name == "metar_abbreviations" and record["example"] is None:
                raise ValueError("metar_abbreviations.example must not be null")
            key = record["METAR_abreviation"]
            if key in keys:
                raise ValueError(f"Duplicate {name} key: {key}")
            keys.add(key)
    return list(sources.values()), list(images.values())


def _quote_identifier(identifier):
    return '"{}"'.format(identifier.replace('"', '""'))


def _insert_records(conn, table, fields, records, ignore=False):
    columns = ", ".join(_quote_identifier(field) for field in fields)
    placeholders = ", ".join("?" for _ in fields)
    action = "INSERT OR IGNORE" if ignore else "INSERT"
    conn.executemany(
        f"{action} INTO {_quote_identifier(table)} ({columns}) VALUES ({placeholders})",
        ([record[field] for field in fields] for record in records),
    )


def _replace_question_tables(conn, question_seed, sources, images):
    tables = ("questions", "image_source", "source_material")
    preserved_views = list(conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'view' AND sql IS NOT NULL"
    ))
    preserved_objects = list(conn.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE type IN ('index', 'trigger') AND tbl_name IN (?, ?, ?) "
        "AND sql IS NOT NULL AND name != 'question_schema'",
        tables,
    ))
    for name, _ in preserved_views:
        conn.execute(f"DROP VIEW {_quote_identifier(name)}")
    for table in tables:
        conn.execute(f"DROP TABLE IF EXISTS {_quote_identifier(table)}")

    conn.execute("""
        CREATE TABLE source_material (
            source TEXT NOT NULL PRIMARY KEY, start_date TEXT, end_date TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE image_source (
            image_key TEXT PRIMARY KEY, image_full_name TEXT NOT NULL,
            image_local_source TEXT, image_original_source TEXT,
            start_date TEXT, end_date TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE questions (
            "Key" INTEGER PRIMARY KEY AUTOINCREMENT, level TEXT, topic TEXT,
            subtopic TEXT, question TEXT, true_answer TEXT, false_answers TEXT,
            image_key TEXT REFERENCES image_source(image_key),
            is_active INTEGER DEFAULT 1,
            language TEXT DEFAULT 'NL', source TEXT REFERENCES source_material(source)
        )
    """)
    _insert_records(conn, "source_material", SOURCE_FIELDS, sources)
    _insert_records(conn, "image_source", IMAGE_FIELDS, images)
    rows = []
    for question in question_seed["questions"]:
        row = {field: question[field] for field in QUESTION_FIELDS}
        row["image_key"] = question["image"]["image_key"] if question["image"] else None
        row["source"] = (
            question["source_material"]["source"] if question["source_material"] else None
        )
        rows.append(row)
    _insert_records(conn, "questions", QUESTION_FIELDS + ("image_key", "source"), rows)
    conn.execute("""
        CREATE INDEX question_schema ON questions
        ("Key" ASC, level ASC, topic DESC, subtopic DESC, is_active ASC, language DESC)
    """)
    for _, _, sql in preserved_objects:
        conn.execute(sql)
    for _, sql in preserved_views:
        conn.execute(sql)
    conn.execute("""
        CREATE VIEW IF NOT EXISTS v_question_amount_by_topic AS
        SELECT level, topic, COUNT(question) FROM questions GROUP BY level, topic
    """)
    conn.execute("""
        CREATE VIEW IF NOT EXISTS v_question_amount_by_topic_and_subtopic AS
        SELECT level, topic, subtopic, COUNT(question)
        FROM questions GROUP BY level, topic, subtopic
    """)


def _initialize_metar_reference(conn, seed):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS metar_weather_codes (
            METAR_abreviation TEXT PRIMARY KEY, NL_definition TEXT NOT NULL,
            EN_definition TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS metar_abbreviations (
            METAR_abreviation TEXT PRIMARY KEY, category TEXT NOT NULL,
            NL_definition TEXT NOT NULL, EN_definition TEXT NOT NULL,
            example TEXT NOT NULL DEFAULT ''
        )
    """)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(metar_abbreviations)")}
    if "example" not in columns:
        conn.execute("ALTER TABLE metar_abbreviations ADD COLUMN example TEXT NOT NULL DEFAULT ''")
    _insert_records(conn, "metar_weather_codes", WEATHER_FIELDS, seed["metar_weather_codes"], ignore=True)
    _insert_records(conn, "metar_abbreviations", ABBREVIATION_FIELDS, seed["metar_abbreviations"], ignore=True)


def initialize_database(seed_path=None, database_path=None, metar_seed_path=None):
    """Synchronize question content and initialize reference data atomically."""
    question_seed = _load_json(seed_path or DEFAULT_QUESTION_SEED)
    metar_seed = _load_json(metar_seed_path or DEFAULT_METAR_SEED)
    sources, images = _validate_seed(question_seed, metar_seed)
    conn = connect_db(path=database_path, configure_journal=True)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("PRAGMA defer_foreign_keys=ON")
        _replace_question_tables(conn, question_seed, sources, images)
        _initialize_metar_reference(conn, metar_seed)
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise sqlite3.IntegrityError(f"Foreign key violations after initialization: {violations}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    initialize_database()
