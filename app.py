import hmac
import logging
import os
import random
import re
import secrets
import smtplib
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from threading import Thread
from urllib.parse import urlsplit

from werkzeug.middleware.proxy_fix import ProxyFix

from flask import (
    Flask,
    abort,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from db import BASE_DIR, connect_db, get_db_path as shared_get_db_path
from metar import DUTCH_AIRPORTS, MetarError, build_abbreviation_questions, build_questions, get_daily_metar, grade_answers, load_weather_definitions, new_question_seed, parse_metar
from system_status import get_service_statuses
import taf as taf_module
from taf import TafError, get_daily_taf
from quiz import (
    build_option_list,
    build_question_groups,
    calculate_grade,
    display_answer_value,
    distribute_question_counts,
    format_elapsed,
    is_passed,
    normalize_answer_value,
    normalize_language,
    normalize_selected_topics,
    topic_passed,
)
from translations import TRANSLATIONS

app = Flask(__name__)
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 31536000
logger = logging.getLogger(__name__)
is_production = os.getenv("APP_ENV", "development").lower() == "production"
# Behind Traefik the real client address is the last X-Forwarded-For entry; trust only that many proxy hops.
trusted_proxy_count = int(os.getenv("TRUSTED_PROXY_COUNT", "1" if is_production else "0"))
if trusted_proxy_count > 0:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=trusted_proxy_count, x_proto=trusted_proxy_count)
secret_key = os.getenv("SECRET_KEY")
if is_production and not secret_key:
    raise RuntimeError("SECRET_KEY must be set when APP_ENV=production")
app.secret_key = secret_key or "local-development-only-change-me"
if is_production:
    app.config.update(
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
    )

if (
    is_production
    and os.getenv("ACCESS_GATE_ENABLED", "1").lower() not in {"0", "false", "no"}
    and not os.getenv("ACCESS_CODE")
):
    raise RuntimeError("ACCESS_CODE must be set when APP_ENV=production and ACCESS_GATE_ENABLED is on")

ACCESS_TOKEN_LIFETIME = timedelta(days=int(os.getenv("ACCESS_TOKEN_LIFETIME_DAYS", "7")))
app.config["PERMANENT_SESSION_LIFETIME"] = ACCESS_TOKEN_LIFETIME
MAX_PRACTICE_ATTEMPTS = 5
GATE_EXEMPT_ENDPOINTS = {"access_gate", "healthz", "it_systems_status", "favicon", "robots_txt", "sitemap_xml", "static", "gdpr"}
_missing_access_code_warned = False


def _prune_practice_attempts(attempts):
    while len(attempts) > MAX_PRACTICE_ATTEMPTS:
        oldest = min(
            attempts,
            key=lambda attempt_id: (
                attempts[attempt_id].get("created_at", 0)
                if isinstance(attempts[attempt_id], dict)
                else 0
            ),
        )
        attempts.pop(oldest)


def _get_practice_attempts(key):
    """Load a bounded set of report snapshots from the client-side session."""
    stored = session.get(key)
    changed = not isinstance(stored, dict)
    attempts = stored if not changed else {}
    original_count = len(attempts)
    _prune_practice_attempts(attempts)
    changed = changed or len(attempts) != original_count
    if changed:
        session[key] = attempts
    return attempts


def _remember_practice_attempt(attempts, attempt_id, state):
    state["created_at"] = time.time()
    attempts.pop(attempt_id, None)
    attempts[attempt_id] = state
    _prune_practice_attempts(attempts)


def ensure_csrf_token():
    """Return the current session CSRF token, creating one when absent."""
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def csrf_token():
    """Expose the current CSRF token to templates."""
    return ensure_csrf_token()


def validate_csrf():
    """Check whether the submitted CSRF token matches the session token."""
    expected = session.get("csrf_token")
    provided = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
    return bool(expected and provided) and hmac.compare_digest(provided, expected)


@app.template_global()
def asset_url(filename):
    """Build a static asset URL with a mtime query string, busting the 1-year static cache on changes."""
    try:
        mtime = int(os.path.getmtime(os.path.join(app.static_folder, filename)))
    except OSError:
        mtime = 0
    return url_for("static", filename=filename) + f"?v={mtime}"


def access_gate_enabled():
    """Return whether the access gate should be enforced right now."""
    global _missing_access_code_warned
    flag_on = os.getenv("ACCESS_GATE_ENABLED", "1").lower() not in {"0", "false", "no"}
    if not flag_on:
        return False
    if not os.getenv("ACCESS_CODE"):
        if not _missing_access_code_warned:
            logger.warning("ACCESS_GATE_ENABLED is on but ACCESS_CODE is not set; access gate disabled")
            _missing_access_code_warned = True
        return False
    return True


def is_access_granted():
    """Check whether the current session holds a still-valid access token."""
    granted_at = session.get("access_granted_at")
    if not granted_at:
        return False
    return (time.time() - granted_at) < ACCESS_TOKEN_LIFETIME.total_seconds()


def is_safe_redirect_target(target):
    """Only allow same-origin relative paths as a post-login redirect target."""
    return bool(target) and target.startswith("/") and not target.startswith("//")


@app.before_request
def enforce_access_gate():
    """Redirect unauthenticated visitors to the access gate while it is enabled."""
    if request.endpoint == "healthz":
        return None
    ensure_csrf_token()
    if not access_gate_enabled():
        return None
    if request.endpoint is None or request.endpoint in GATE_EXEMPT_ENDPOINTS:
        return None
    if is_access_granted():
        return None
    return redirect(url_for("access_gate", next=request.full_path))


ALLOWED_LEVELS = ("A", "B")
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")
CONTACT_SUBJECTS = (
    "Vraag over de website",
    "Suggestie",
    "Foutmelding",
    "Privacy / AVG",
    "Anders",
)


def translate(text, language="NL", **values):
    """Translate a UI string and interpolate any dynamic values."""
    translated = TRANSLATIONS.get(normalize_language(language), {}).get(text, text)
    return translated.format(**values) if values else translated


@app.before_request
def enforce_csrf():
    """Reject unsafe requests that do not include a valid CSRF token."""
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and not validate_csrf():
        return translate("Je formulier is verlopen. Vernieuw de pagina en probeer het opnieuw.", session.get("language")), 400
    return None


@app.after_request
def add_security_headers(response):
    """Attach baseline security headers to every response."""
    response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    return response


@app.after_request
def add_conditional_etag(response):
    """Let browsers revalidate HTML pages and receive 304 instead of the full body."""
    if (
        request.method == "GET"
        and response.status_code == 200
        and response.mimetype == "text/html"
        and not response.direct_passthrough
    ):
        response.headers.setdefault("Cache-Control", "private, no-cache")
        response.add_etag()
        response.make_conditional(request)
    return response


@app.context_processor
def inject_language_context():
    language = normalize_language(session.get("language", "NL"))
    return {
        "language": language,
        "csrf_token": csrf_token,
        "t": lambda text, **values: translate(text, language, **values),
    }


_language_column_cache = {}


def _database_signature():
    """Identify the current database file so cached schema facts go stale when it is replaced."""
    path = get_db_path()
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return path, stat.st_mtime_ns, stat.st_size


def questions_have_language_column(conn):
    """Check whether this database has the optional question language column (cached per database file)."""
    signature = _database_signature()
    if signature is not None and signature in _language_column_cache:
        return _language_column_cache[signature]
    columns = conn.execute("PRAGMA table_info(questions)").fetchall()
    has_column = any(column[1] == "language" for column in columns)
    if signature is not None:
        _language_column_cache.clear()
        _language_column_cache[signature] = has_column
    return has_column


def language_filter(conn, language, prefix=""):
    """Return a query fragment and parameter for compatible question databases."""
    if questions_have_language_column(conn):
        return f" AND {prefix}language = ?", [normalize_language(language)]
    return "", []


def get_db_path():
    """Expose the shared database path resolver for tests and callers."""
    return shared_get_db_path()


def get_db_connection():
    """Create and return a SQLite database connection for the quiz data."""
    return connect_db(path=get_db_path(), row_factory=sqlite3.Row)


def _question_query_schema(conn):
    """Return the question SELECT and join details supported by this database."""
    question_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(questions)").fetchall()
    }
    has_image_sources = (
        "image_key" in question_columns
        and conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'image_source'"
        ).fetchone()
        is not None
    )
    if has_image_sources:
        return (
            "questions.rowid AS id, questions.*, "
            "image_source.image_local_source AS image_local_source, "
            "image_source.image_original_source AS image_original_source",
            "questions LEFT JOIN image_source "
            "ON image_source.image_key = questions.image_key",
            "questions.",
        )
    return "rowid AS id, *", "questions", ""


def get_available_topics(level, language="NL"):
    """Return all active topics for the selected exam level."""
    conn = get_db_connection()
    language_clause, language_params = language_filter(conn, language)
    rows = conn.execute(
        f"""
        SELECT DISTINCT topic
        FROM questions
        WHERE level = ? AND is_active = 1{language_clause}
        ORDER BY topic ASC
        """,
        (level, *language_params),
    ).fetchall()
    conn.close()
    return [row["topic"] for row in rows]


def get_topic_counts_for_level(level, amount, selected_topics=None, language="NL"):
    """Return per-topic, per-subtopic counts and the total selected questions."""
    selected_topics = normalize_selected_topics(selected_topics)
    conn = get_db_connection()
    language_clause, language_params = language_filter(conn, language)

    if selected_topics:
        placeholders = ", ".join("?" for _ in selected_topics)
        topic_rows = conn.execute(
            f"""
            SELECT topic, subtopic, COUNT(*) AS c
            FROM questions
            WHERE level = ? AND topic IN ({placeholders}) AND is_active = 1{language_clause}
            GROUP BY topic, subtopic
            ORDER BY topic, subtopic
            """,
            (level, *selected_topics, *language_params),
        ).fetchall()
    else:
        topic_rows = conn.execute(
            f"""
            SELECT topic, subtopic, COUNT(*) AS c
            FROM questions
            WHERE level = ? AND is_active = 1{language_clause}
            GROUP BY topic, subtopic
            ORDER BY topic, subtopic
            """,
            (level, *language_params),
        ).fetchall()
    conn.close()

    available_by_topic = {}
    for row in topic_rows:
        topic = row["topic"]
        subtopic = row["subtopic"]
        available_by_topic.setdefault(topic, {})
        available_by_topic[topic][subtopic] = row["c"]

    return distribute_question_counts(available_by_topic, amount)


def fetch_questions(level, amount, selected_topics=None, language="NL"):
    """Fetch a random set of active questions for a level and optional topic filter."""
    selected_topics = normalize_selected_topics(selected_topics)
    topic_counts, selected_total = get_topic_counts_for_level(
        level, amount, selected_topics, language=language
    )

    if selected_total == 0:
        return []

    conn = get_db_connection()
    language_clause, language_params = language_filter(
        conn, language, prefix="questions."
    )
    select_columns, question_source, column_prefix = _question_query_schema(conn)
    topic_names = list(topic_counts)
    placeholders = ", ".join("?" for _ in topic_names)
    rows = conn.execute(
        f"""
        SELECT {select_columns}
        FROM {question_source}
        WHERE {column_prefix}level = ? AND {column_prefix}topic IN ({placeholders})
            AND {column_prefix}is_active = 1{language_clause}
        """,
        (level, *topic_names, *language_params),
    ).fetchall()

    conn.close()

    rows_by_subtopic = {}
    for row in rows:
        key = (row["topic"], row["subtopic"])
        rows_by_subtopic.setdefault(key, []).append(row)

    selected_rows = []
    for topic, subtopic_counts in topic_counts.items():
        for subtopic, count in subtopic_counts.items():
            if count <= 0:
                continue
            candidates = rows_by_subtopic.get((topic, subtopic), [])
            selected_rows.extend(random.sample(candidates, min(count, len(candidates))))

    random.shuffle(selected_rows)
    return selected_rows


def fetch_questions_by_ids(level, question_ids, language="NL"):
    """Fetch active questions by ID for a specific level."""
    normalized_ids = [
        int(question_id) for question_id in question_ids if str(question_id).strip()
    ]
    if not normalized_ids:
        return []

    placeholders = ", ".join("?" for _ in normalized_ids)
    conn = get_db_connection()
    language_clause, language_params = language_filter(
        conn, language, prefix="questions."
    )
    select_columns, question_source, column_prefix = _question_query_schema(conn)
    rows = conn.execute(
        f"""
        SELECT {select_columns}
        FROM {question_source}
        WHERE {column_prefix}level = ? AND {column_prefix}rowid IN ({placeholders})
            AND {column_prefix}is_active = 1{language_clause}
        """,
        (level, *normalized_ids, *language_params),
    ).fetchall()
    conn.close()

    row_map = {int(row["id"]): row for row in rows}
    return [
        row_map[question_id]
        for question_id in normalized_ids
        if question_id in row_map
    ]


def get_row_value(row, key, default=None):
    """Safely return a value from a row dictionary or default if missing."""
    if row is None:
        return default
    try:
        return row[key]
    except (KeyError, TypeError, IndexError):
        return default


def render_question(row, level, language="NL"):
    """Build the data structure used by the exam templates."""
    option_map = build_option_list(
        row["false_answers"], row["true_answer"], level=level, language=language
    )

    image_source = get_row_value(row, "image_original_source")
    if image_source is not None:
        image_source = str(image_source).strip() or None
    local_image_source = get_row_value(row, "image_local_source")
    if not local_image_source:
        local_image_source = get_row_value(
            row, "image_key", get_row_value(row, "image_link")
        )

    return {
        "id": row["id"],
        "question": row["question"],
        "topic": row["topic"],
        "options": option_map,
        "is_yes_no": level == "A",
        "image_url": get_question_image_url(local_image_source),
        "image_original_source": image_source,
        "image_original_source_url": get_http_url(image_source),
    }


def normalize_level(level):
    """Return a validated level or None."""
    normalized = str(level).strip().upper()
    if normalized in ALLOWED_LEVELS:
        return normalized
    return None


def start_exam(level, question_amount, selected_topics=None):
    """Set the selected level, question count, and retained topic filters."""
    session["level"] = str(level).strip().upper()
    session["question_amount"] = int(question_amount)
    session["selected_topics"] = normalize_selected_topics(selected_topics)
    session["started_at"] = time.time()
    session["exam_question_ids"] = []


@app.route("/favicon.svg")
@app.route("/favicon.ico")
def favicon():
    """Serve a high-contrast favicon for the browser tab."""
    icon_path = os.path.join(app.root_path, "static", "branding", "skydive-theorie-favicon.svg")
    response = send_file(icon_path, mimetype="image/svg+xml")
    response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return response


@app.route("/healthz")
def healthz():
    return {"status": "ok"}, 200


@app.route("/it-systemen")
def it_systems_status():
    states = get_service_statuses()
    services = [
        {
            "name": "SMTP",
            "description": "E-mailverbinding voor het contactformulier.",
            **states.get("smtp", {"status": "unknown", "checked_at": None}),
        },
        {
            "name": "KNMI Notification Service (MQTT)",
            "description": "Meldingen voor nieuwe KNMI METAR-bestanden.",
            **states.get("knmi_mqtt", {"status": "unknown", "checked_at": None}),
        },
        {
            "name": "KNMI Open Data API",
            "description": "Ophalen van METAR-gegevens uit KNMI Open Data.",
            **states.get("knmi_opendata", {"status": "unknown", "checked_at": None}),
        },
    ]
    for service in services:
        checked_at = service["checked_at"]
        service["checked_at_iso"] = (
            datetime.fromtimestamp(checked_at, timezone.utc).isoformat(timespec="minutes")
            if checked_at is not None
            else None
        )
        service["checked_at_display"] = (
            datetime.fromtimestamp(checked_at, timezone.utc).strftime("%d-%m-%Y %H:%M UTC")
            if checked_at is not None
            else None
        )
        activity_at = service.get("last_activity_at")
        service["last_activity_iso"] = (
            datetime.fromtimestamp(activity_at, timezone.utc).isoformat(timespec="minutes")
            if activity_at is not None
            else None
        )
        service["last_activity_display"] = (
            datetime.fromtimestamp(activity_at, timezone.utc).strftime("%d-%m-%Y %H:%M UTC")
            if activity_at is not None
            else None
        )

    statuses = {service["status"] for service in services}
    if statuses == {"operational"}:
        overall_status = "operational"
        overall_label = "Alle systemen operationeel"
    elif "outage" in statuses:
        overall_status = "outage"
        overall_label = "Er is een storing bij een of meer diensten"
    elif "not_configured" in statuses:
        overall_status = "unknown"
        overall_label = "Niet alle diensten zijn geconfigureerd"
    else:
        overall_status = "unknown"
        overall_label = "Status wordt gecontroleerd"

    response = app.make_response(
        render_template(
            "it_systems.html",
            services=services,
            overall_status=overall_status,
            overall_label=overall_label,
        )
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/robots.txt")
def robots_txt():
    """Serve crawler instructions without triggering the access gate."""
    return send_file("robots.txt", mimetype="text/plain")


@app.route("/sitemap.xml")
def sitemap_xml():
    """Serve the sitemap without triggering the access gate."""
    return send_file("sitemap.xml", mimetype="application/xml")


@app.route("/language", methods=["POST"])
def set_language():
    """Persist the selected UI and question language, then return to the page."""
    language = normalize_language(request.form.get("language"))
    if language != normalize_language(session.get("language", "NL")):
        session.pop("selected_topics", None)
    session["language"] = language
    session.pop("exam_question_ids", None)
    target = request.referrer or url_for("index")
    if not target.startswith(request.host_url):
        target = url_for("index")
    return redirect(target)


GATE_MAX_FAILURES = 5
GATE_FAILURE_WINDOW = 60
_gate_table_ready = set()


def _ensure_gate_failure_table(conn):
    """Create the shared failed-attempt table once per database path."""
    path = get_db_path()
    if path in _gate_table_ready and os.path.exists(path):
        return
    conn.execute("CREATE TABLE IF NOT EXISTS gate_failure (ip TEXT NOT NULL, failed_at REAL NOT NULL)")
    conn.execute("CREATE INDEX IF NOT EXISTS gate_failure_ip ON gate_failure (ip, failed_at)")
    conn.commit()
    _gate_table_ready.add(path)


def _gate_attempts_exhausted(ip_address):
    """Report whether an IP made too many wrong code attempts recently, shared across all workers and replicas."""
    cutoff = time.time() - GATE_FAILURE_WINDOW
    conn = get_db_connection()
    try:
        _ensure_gate_failure_table(conn)
        conn.execute("DELETE FROM gate_failure WHERE failed_at < ?", (cutoff,))
        conn.commit()
        count = conn.execute(
            "SELECT COUNT(*) FROM gate_failure WHERE ip = ? AND failed_at >= ?",
            (ip_address, cutoff),
        ).fetchone()[0]
        return count >= GATE_MAX_FAILURES
    finally:
        conn.close()


def _record_gate_failure(ip_address):
    conn = get_db_connection()
    try:
        _ensure_gate_failure_table(conn)
        conn.execute("INSERT INTO gate_failure (ip, failed_at) VALUES (?, ?)", (ip_address, time.time()))
        conn.commit()
    finally:
        conn.close()


@app.route("/access-gate", methods=["GET", "POST"])
def access_gate():
    """Show the under-construction code gate and verify submitted access codes."""
    next_target = request.values.get("next", "")
    if not is_safe_redirect_target(next_target):
        next_target = url_for("index")

    if request.method == "POST":
        client_ip = request.remote_addr or ""
        if _gate_attempts_exhausted(client_ip):
            return render_template(
                "gate.html",
                next=next_target,
                error=translate("Te veel pogingen. Probeer het over een minuut opnieuw.", session.get("language")),
            ), 429

        submitted_code = request.form.get("code", "")
        expected_code = os.getenv("ACCESS_CODE") or ""
        if expected_code and hmac.compare_digest(submitted_code, expected_code):
            session["access_granted_at"] = time.time()
            session.permanent = True
            return redirect(next_target)

        logger.warning("Rejected access gate code attempt from %s", request.remote_addr)
        _record_gate_failure(client_ip)
        return render_template(
            "gate.html",
            next=next_target,
            error=translate("De ingevoerde code is onjuist. Probeer het opnieuw.", session.get("language")),
        ), 401

    return render_template("gate.html", next=next_target, error=None)


@app.route("/")
def index():
    """Render the home page."""
    return render_template("index.html")


@app.route("/practice")
def practice():
    """Render the first practice selection step: choose the brevet level."""
    return render_template(
        "practice.html",
        stage="level",
        question_text="Welk brevet wil je voor oefenen?",
        level=None,
        error=None,
    )


@app.route("/practice/select", methods=["POST"])
def practice_select():
    """Store the chosen brevet and continue to the mode selection step."""
    level = normalize_level(request.form.get("level", ""))
    if level is None:
        return translate("Ongeldige keuze. Kies A of B.", session.get("language")), 400

    return redirect(url_for("practice_mode", level=level))


@app.route("/practice/mode/<level>", methods=["GET", "POST"])
def practice_mode(level):
    """Choose whether to create a full practice exam or do free practice."""
    normalized_level = normalize_level(level)
    if normalized_level is None:
        return translate("Ongeldige keuze. Kies A of B.", session.get("language")), 400

    if request.method == "POST":
        practice_type = request.form.get("practice_type", "").strip().lower()
        if practice_type == "exam":
            start_exam(normalized_level, 40)
            return redirect(url_for("exam"))
        if practice_type == "free":
            return redirect(url_for("practice_free", level=normalized_level))
        return translate("Ongeldige keuze. Kies een oefenmodus.", session.get("language")), 400

    return render_template(
        "practice.html",
        stage="mode",
        level=normalized_level,
        question_text="Wil je vrij oefenen, of een oefenexamen maken?",
        error=None,
    )


@app.route("/practice/free/<level>", methods=["GET", "POST"])
def practice_free(level):
    """Prompt for a custom question count and start free practice."""
    normalized_level = normalize_level(level)
    if normalized_level is None:
        return translate("Ongeldige keuze. Kies A of B.", session.get("language")), 400

    language = normalize_language(session.get("language", "NL"))
    available_topics = get_available_topics(normalized_level, language)

    if request.method == "POST":
        try:
            question_amount = int(request.form.get("question_amount", "1"))
        except ValueError:
            question_amount = 1

        selected_topics = normalize_selected_topics(request.form.getlist("selected_topics"))
        if not selected_topics:
            selected_topics = available_topics

        if not 1 <= question_amount <= 40:
            return render_template(
                "practice.html",
                stage="free",
                level=normalized_level,
                question_text="Hoeveel vragen wil je laden?",
                error="Kies een getal tussen 1 en 40.",
                available_topics=available_topics,
                selected_topics=selected_topics,
            )

        if not available_topics:
            return render_template(
                "practice.html",
                stage="free",
                level=normalized_level,
                question_text="Hoeveel vragen wil je laden?",
                error="Er zijn nog geen vragen beschikbaar voor dit brevet.",
                available_topics=[],
                selected_topics=[],
            )

        start_exam(normalized_level, question_amount, selected_topics)
        return redirect(url_for("exam"))

    return render_template(
        "practice.html",
        stage="free",
        level=normalized_level,
        question_text="Hoeveel vragen wil je laden?",
        error=None,
        available_topics=available_topics,
        selected_topics=available_topics,
    )


@app.route("/leren")
def learn():
    """Render the learning page."""
    return render_template("learn.html")


def metar_refresh_enabled():
    """Request threads only read cached METARs when the KNMI listener fills the cache."""
    return os.getenv("KNMI_METAR_REFRESH", "1").lower() not in {"0", "false", "no"}


@app.route("/metar")
def metar_choice():
    """Let the user choose between parsing reports and practising abbreviations."""
    return render_template("metar_choice.html", practice_mode=None)


@app.route("/metar/keuze/<practice_mode>")
def metar_type_choice(practice_mode):
    """Let the user choose whether to practise METARs or TAFs."""
    if practice_mode not in {"ontleden", "afkortingen"}:
        abort(404)
    return render_template("metar_choice.html", practice_mode=practice_mode)


@app.route("/metar/afkortingen", methods=["GET", "POST"])
def metar_abbreviations():
    """Multiple-choice practice for METAR abbreviations."""
    checking = request.method == "POST" and request.form.get("action") == "check"
    seed = str(request.form.get("seed", "")).strip()
    if not (checking and re.fullmatch(r"[0-9a-f]{16}", seed)):
        seed = new_question_seed()
        checking = False

    language = normalize_language(session.get("language", "NL"))
    questions = build_abbreviation_questions(seed, language, load_weather_definitions())
    result = grade_answers(questions, request.form) if checking else None
    return render_template(
        "metar_abbreviations.html",
        questions=questions,
        result=result,
        seed=seed,
    )


@app.route("/metar/ontleden", methods=["GET", "POST"])
def metar_practice():
    """Show and grade the daily METAR exercise for a Dutch airport."""
    checking = request.method == "POST" and request.form.get("action") == "check"
    attempt_id = request.form.get("attempt_id") or session.get("metar_attempt_id")
    attempts = _get_practice_attempts("metar_attempts")
    attempt_state = attempts.get(attempt_id) if attempt_id else None

    # Grade against the airport the questions were generated for; the dropdown may
    # hold a stale value (e.g. restored by the browser) from a previous exercise.
    if checking:
        selected_airport = str(
            (attempt_state and attempt_state.get("airport"))
            or request.form.get("question_airport")
            or session.get("metar_airport")
            or request.form.get("airport")
            or request.args.get("airport")
            or "EHAM"
        ).strip().upper()
    else:
        selected_airport = str(
            request.form.get("airport")
            or request.args.get("airport")
            or session.get("metar_airport")
            or "EHAM"
        ).strip().upper()
    error = None
    result = None
    questions = []
    attempt_id = attempt_id or uuid.uuid4().hex

    if checking and (not attempt_state or not attempt_state.get("raw")):
        error = "Deze oefening is verlopen. Laad opnieuw om verder te oefenen."
        metar = None
    elif selected_airport not in DUTCH_AIRPORTS:
        error = "Kies een Nederlandse luchthaven uit de lijst."
        metar = None
    elif checking:
        metar = parse_metar(attempt_state["raw"])
        metar.update({
            "airport": selected_airport,
            "airport_name": DUTCH_AIRPORTS[selected_airport],
            "date": attempt_state.get("date"),
        })
    else:
        try:
            metar = get_daily_metar(
                selected_airport,
                wait_for_rate_limit=False,
                cache_only=metar_refresh_enabled(),
            )
            session["metar_airport"] = selected_airport
        except MetarError as exc:
            metar = None
            error = str(exc)

    if metar and not error:
        language = normalize_language(session.get("language", "NL"))
        definitions = load_weather_definitions()
        if checking:
            if attempt_state:
                seed = attempt_state.get("seed") or session.pop("metar_seed", None) or new_question_seed()
            else:
                seed = session.pop("metar_seed", None) or new_question_seed()
        else:
            seed = new_question_seed()
            session["metar_seed"] = seed
            session["metar_attempt_id"] = attempt_id
            _remember_practice_attempt(attempts, attempt_id, {
                "airport": selected_airport,
                "seed": seed,
                "raw": metar["raw"],
                "date": metar.get("date"),
            })
            session["metar_attempts"] = attempts
        questions = build_questions(metar, seed, language, definitions)
        if checking:
            result = grade_answers(questions, request.form)

    return render_template(
        "metar.html",
        airports=DUTCH_AIRPORTS,
        airport=selected_airport,
        metar=metar,
        questions=questions,
        submitted=request.form if result else {},
        result=result,
        error=error,
        attempt_id=attempt_id,
    )


def taf_refresh_enabled():
    """Request threads only read cached TAFs when the background refresher fills the cache."""
    return os.getenv("KNMI_TAF_REFRESH", "1").lower() not in {"0", "false", "no"}


@app.route("/taf/afkortingen", methods=["GET", "POST"])
def taf_abbreviations():
    """Multiple-choice practice for TAF abbreviations."""
    checking = request.method == "POST" and request.form.get("action") == "check"
    seed = str(request.form.get("seed", "")).strip()
    if not (checking and re.fullmatch(r"[0-9a-f]{16}", seed)):
        seed = new_question_seed()
        checking = False

    language = normalize_language(session.get("language", "NL"))
    questions = taf_module.build_abbreviation_questions(seed, language, load_weather_definitions())
    result = grade_answers(questions, request.form) if checking else None
    return render_template(
        "taf_abbreviations.html",
        questions=questions,
        result=result,
        seed=seed,
    )


@app.route("/taf/ontleden", methods=["GET", "POST"])
def taf_practice():
    """Show and grade the daily TAF exercise for a Dutch airport."""
    checking = request.method == "POST" and request.form.get("action") == "check"
    attempt_id = request.form.get("attempt_id") or session.get("taf_attempt_id")
    attempts = _get_practice_attempts("taf_attempts")
    attempt_state = attempts.get(attempt_id) if attempt_id else None

    if checking:
        selected_airport = str(
            (attempt_state and attempt_state.get("airport"))
            or request.form.get("question_airport")
            or session.get("taf_airport")
            or request.form.get("airport")
            or request.args.get("airport")
            or "EHAM"
        ).strip().upper()
    else:
        selected_airport = str(
            request.form.get("airport")
            or request.args.get("airport")
            or session.get("taf_airport")
            or "EHAM"
        ).strip().upper()
    error = None
    result = None
    questions = []
    taf = None
    attempt_id = attempt_id or uuid.uuid4().hex

    if checking and (not attempt_state or not attempt_state.get("raw")):
        error = "Deze oefening is verlopen. Laad opnieuw om verder te oefenen."
    elif selected_airport not in DUTCH_AIRPORTS:
        error = "Kies een Nederlandse luchthaven uit de lijst."
    elif checking:
        taf = taf_module.parse_taf(attempt_state["raw"])
        taf.update({
            "airport": selected_airport,
            "airport_name": DUTCH_AIRPORTS[selected_airport],
            "date": attempt_state.get("date"),
        })
    else:
        try:
            taf = get_daily_taf(
                selected_airport,
                wait_for_rate_limit=False,
                cache_only=taf_refresh_enabled(),
            )
            session["taf_airport"] = selected_airport
        except TafError as exc:
            error = str(exc)

    if taf and not error:
        language = normalize_language(session.get("language", "NL"))
        definitions = load_weather_definitions()
        if checking:
            seed = (attempt_state or {}).get("seed") or session.pop("taf_seed", None) or new_question_seed()
        else:
            seed = new_question_seed()
            session["taf_seed"] = seed
            session["taf_attempt_id"] = attempt_id
            _remember_practice_attempt(attempts, attempt_id, {
                "airport": selected_airport,
                "seed": seed,
                "raw": taf["raw"],
                "date": taf.get("date"),
            })
            session["taf_attempts"] = attempts
        questions = taf_module.build_questions(taf, seed, language, definitions)
        if checking:
            result = grade_answers(questions, request.form)

    return render_template(
        "taf.html",
        airports=DUTCH_AIRPORTS,
        airport=selected_airport,
        taf=taf,
        questions=questions,
        submitted=request.form if result else {},
        result=result,
        error=error,
        attempt_id=attempt_id,
    )


def send_contact_message(sender_email, subject, message):
    """Send a contact message using SMTP settings supplied through the environment."""
    smtp_host = os.getenv("SMTP_HOST")
    recipient = os.getenv("CONTACT_EMAIL")
    if not smtp_host or not recipient:
        raise RuntimeError("SMTP_HOST and CONTACT_EMAIL must be configured")

    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_username = os.getenv("SMTP_USERNAME")
    smtp_password = os.getenv("SMTP_PASSWORD")
    sender = os.getenv("SMTP_FROM_EMAIL") or smtp_username or recipient
    use_ssl = os.getenv("SMTP_USE_SSL", "0").lower() in {"1", "true", "yes"}
    use_tls = os.getenv("SMTP_USE_TLS", "1").lower() in {"1", "true", "yes"}

    email = EmailMessage()
    email["From"] = sender
    email["To"] = recipient
    email["Reply-To"] = sender_email
    email["Subject"] = f"Contactformulier: {subject}"
    email.set_content(f"Afzender: {sender_email}\nOnderwerp: {subject}\n\n{message}")

    smtp_class = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
    with smtp_class(smtp_host, smtp_port, timeout=20) as smtp:
        if use_tls and not use_ssl:
            smtp.starttls()
        if smtp_username:
            smtp.login(smtp_username, smtp_password or "")
        smtp.send_message(email)


def start_contact_message_delivery(sender_email, subject, message):
    """Deliver a contact message outside the request so SMTP cannot block the page."""
    def deliver():
        try:
            send_contact_message(
                sender_email=sender_email,
                subject=subject,
                message=message,
            )
        except (OSError, smtplib.SMTPException, ValueError, RuntimeError):
            logger.exception("Unable to send contact message")

    Thread(target=deliver, name="contact-message-delivery", daemon=True).start()


_contact_table_ready = set()


def _ensure_contact_usage_table(conn):
    """Create the contact usage table once per database path instead of on every request."""
    path = get_db_path()
    if path in _contact_table_ready and os.path.exists(path):
        return
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS contact_form_usage (
            date TEXT,
            ip TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS contact_form_usage_ip_date
        ON contact_form_usage (ip, date)
        """
    )
    conn.commit()
    _contact_table_ready.add(path)


def reserve_contact_form_usage(ip_address):
    """Reserve one contact message for an IP address within the daily limit."""
    conn = get_db_connection()
    try:
        _ensure_contact_usage_table(conn)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "DELETE FROM contact_form_usage WHERE date < datetime('now', '-30 days')"
        )
        usage_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM contact_form_usage
            WHERE ip = ? AND date >= datetime('now', '-24 hours')
            """,
            (ip_address,),
        ).fetchone()[0]
        if usage_count >= 3:
            conn.commit()
            return False

        conn.execute(
            "INSERT INTO contact_form_usage (date, ip) VALUES (datetime('now'), ?)",
            (ip_address,),
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.route("/contact", methods=["GET", "POST"])
def contact():
    """Render and handle the contact form."""
    form_data = {"email": "", "subject": "", "message": ""}
    error = None

    if request.method == "POST":
        form_data = {
            "email": request.form.get("email", "").strip(),
            "subject": request.form.get("subject", "").strip(),
            "message": request.form.get("message", "").strip(),
        }
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", form_data["email"]):
            error = "Vul een geldig e-mailadres in."
        elif form_data["subject"] not in CONTACT_SUBJECTS:
            error = "Kies een onderwerp uit de lijst."
        elif not form_data["message"]:
            error = "Vul een bericht in."
        elif not reserve_contact_form_usage(request.remote_addr or ""):
            error = "Je hebt de limiet van 3 berichten per dag bereikt. Probeer het morgen opnieuw."
        else:
            start_contact_message_delivery(
                sender_email=form_data["email"],
                subject=form_data["subject"],
                message=form_data["message"],
            )
            return redirect(url_for("contact", sent="1"))

    return render_template(
        "contact.html",
        contact_subjects=CONTACT_SUBJECTS,
        form_data=form_data,
        error=error,
        sent=request.args.get("sent") == "1",
    )


@app.route("/gdpr")
def gdpr():
    """Show the EU GDPR notice for server and website access logs."""
    return render_template("gdpr.html")


@app.route("/start", methods=["POST"])
def start():
    """Validate the selected exam settings and redirect to the exam page."""
    level = normalize_level(request.form.get("level", ""))
    try:
        question_amount = int(request.form.get("question_amount", "1"))
    except ValueError:
        question_amount = 1

    if level is None:
        return translate("Ongeldige keuze. Kies A of B.", session.get("language")), 400

    if not 1 <= question_amount <= 40:
        return translate("Kies een getal tussen 1 en 40.", session.get("language")), 400

    start_exam(level, question_amount)
    return redirect(url_for("exam"))


@app.route("/exam", methods=["GET", "POST"])
def exam():
    """Render exam questions, handle submissions, and show the results."""
    level = session.get("level")
    amount = session.get("question_amount", 1)
    selected_topics = session.get("selected_topics")
    language = normalize_language(session.get("language", "NL"))

    if not level:
        return redirect(url_for("index"))

    if request.method == "POST":
        stored_ids = session.get("exam_question_ids", [])
        questions = (
            fetch_questions_by_ids(level, stored_ids, language=language)
            if stored_ids
            else fetch_questions(
                level, amount, selected_topics=selected_topics, language=language
            )
        )
    else:
        questions = fetch_questions(
            level, amount, selected_topics=selected_topics, language=language
        )

    if request.method == "GET" and questions:
        session["exam_question_ids"] = [row["id"] for row in questions]

    if request.method == "POST":
        started_at = session.get("started_at")
        elapsed_seconds = time.time() - started_at if started_at else 0

        score = 0
        topic_scores = {}
        results = []

        for row in questions:
            topic = row["topic"]
            if topic not in topic_scores:
                topic_scores[topic] = {"correct": 0, "total": 0}

            correct_answer = normalize_answer_value(row["true_answer"])

            submitted_value = request.form.get(f"answer_{row['id']}", "")
            selected_value = normalize_answer_value(submitted_value)

            is_unanswered = not str(submitted_value).strip()
            is_correct = not is_unanswered and selected_value == correct_answer
            if is_correct:
                score += 1
                topic_scores[topic]["correct"] += 1

            topic_scores[topic]["total"] += 1
            image_original_source = get_row_value(
                row, "image_original_source"
            )
            if image_original_source is not None:
                image_original_source = str(image_original_source).strip() or None
            image_local_source = (
                get_row_value(row, "image_local_source")
                or get_row_value(row, "image_key", get_row_value(row, "image_link"))
            )

            results.append(
                {
                    "question": row["question"],
                    "topic": topic,
                    "selected": display_answer_value(submitted_value, language),
                    "correct": display_answer_value(row["true_answer"], language),
                    "is_correct": is_correct,
                    "is_unanswered": is_unanswered,
                    "source": get_row_value(row, "source"),
                    "image_original_source": image_original_source,
                    "image_original_source_url": get_http_url(
                        image_original_source
                    ),
                    "image_url": get_question_image_url(image_local_source),
                }
            )

        sorted_topic_scores = {
            topic: {
                **stats,
                "passed": topic_passed(stats["correct"], stats["total"]),
                "percent": (
                    round((stats["correct"] / stats["total"] * 100), 1)
                    if stats["total"]
                    else 0
                ),
            }
            for topic, stats in sorted(topic_scores.items())
        }

        total_questions = len(questions)
        overall_pass = is_passed(score, total_questions) and all(
            detail["passed"] for detail in sorted_topic_scores.values()
        )

        final_grade = calculate_grade(score, total_questions)

        result_context = {
            "score": score,
            "total": total_questions,
            "results": results,
            "elapsed_time": format_elapsed(elapsed_seconds),
            "topic_scores": sorted_topic_scores,
            "grade": final_grade,
            "passed": overall_pass,
        }
        return render_template("result.html", **result_context)

    rendered_questions = [render_question(row, level, language) for row in questions]
    exam_context = {
        "questions": rendered_questions,
        "question_groups": build_question_groups(rendered_questions),
        "total_questions": len(rendered_questions),
        "started_at": session.get("started_at", time.time()),
        "level": level,
    }
    return render_template("exam.html", **exam_context)


def get_question_image_url(image_link):
    """Convert a stored image path into a static file URL when possible."""
    if image_link is None:
        return None

    image_path = str(image_link).strip()
    if not image_path:
        return None

    normalized = image_path.replace("\\", "/")
    lower = normalized.lower()

    if not lower.endswith(IMAGE_EXTENSIONS):
        return None

    if normalized.startswith(("http://", "https://")):
        return normalized

    if normalized.startswith("/"):
        if normalized.startswith("/static/"):
            return normalized
        return asset_url(normalized.lstrip("/"))

    if normalized.startswith("static/"):
        return asset_url(normalized.replace("static/", "", 1))

    if normalized.startswith("./"):
        return asset_url(normalized.lstrip("./"))

    if normalized.startswith("images/") or normalized.startswith("question_images/"):
        return asset_url(normalized)

    return None


def get_http_url(value):
    """Return an HTTP(S) URL for safe linking, or None for plain source text."""
    if not value:
        return None
    try:
        parsed = urlsplit(str(value).strip())
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return None
    return str(value).strip()


# purely for local development, gunicorn will be used in production
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
