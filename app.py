import hmac
import logging
import os
import random
import re
import secrets
import smtplib
import sqlite3
import time
from datetime import timedelta
from email.message import EmailMessage
from threading import Thread

from flask import (
    Flask,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from db import BASE_DIR, connect_db, get_db_path as shared_get_db_path
from metar import DUTCH_AIRPORTS, MetarError, get_daily_metar, grade_answers
from quiz import (
    build_option_list,
    build_question_groups,
    calculate_grade,
    display_answer_value,
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
GATE_EXEMPT_ENDPOINTS = {"access_gate", "healthz", "favicon", "robots_txt", "sitemap_xml", "static", "gdpr"}
_missing_access_code_warned = False


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
    ensure_csrf_token()
    if not access_gate_enabled():
        return None
    if request.endpoint is None or request.endpoint in GATE_EXEMPT_ENDPOINTS:
        return None
    if is_access_granted():
        return None
    return redirect(url_for("access_gate", next=request.full_path))


ALLOWED_LEVELS = ("A", "B")
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png")
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


@app.context_processor
def inject_language_context():
    language = normalize_language(session.get("language", "NL"))
    return {
        "language": language,
        "csrf_token": csrf_token,
        "t": lambda text, **values: translate(text, language, **values),
    }


def questions_have_language_column(conn):
    """Check whether this database has the optional question language column."""
    columns = conn.execute("PRAGMA table_info(questions)").fetchall()
    return any(column[1] == "language" for column in columns)


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

    if not topic_rows:
        return {}, 0

    available_by_topic = {}
    for row in topic_rows:
        topic = row["topic"]
        subtopic = row["subtopic"]
        available_by_topic.setdefault(topic, {})
        available_by_topic[topic][subtopic] = row["c"]

    topic_names = sorted(available_by_topic)
    total_available = sum(
        sum(subtopic_counts.values())
        for subtopic_counts in available_by_topic.values()
    )
    selected_total = min(amount, total_available)

    if selected_total <= 0:
        empty_counts = {
            topic: {subtopic: 0 for subtopic in sorted(subtopics)}
            for topic, subtopics in available_by_topic.items()
        }
        return empty_counts, 0

    counts = {
        topic: {subtopic: 0 for subtopic in sorted(subtopics)}
        for topic, subtopics in available_by_topic.items()
    }

    topic_targets = {topic: 0 for topic in topic_names}
    base = selected_total // len(topic_names)
    remainder = selected_total % len(topic_names)
    for index, topic in enumerate(topic_names):
        topic_total = sum(available_by_topic[topic].values())
        topic_targets[topic] = min(
            base + (1 if index < remainder else 0),
            topic_total,
        )

    used = sum(topic_targets.values())
    if used < selected_total:
        for topic in topic_names:
            free_space = sum(available_by_topic[topic].values()) - sum(
                counts[topic].values()
            )
            if free_space > 0:
                extra = min(selected_total - used, free_space)
                topic_targets[topic] += extra
                used += extra
                if used >= selected_total:
                    break

    for topic in topic_names:
        target = topic_targets.get(topic, 0)
        if target <= 0:
            continue

        subtopics = sorted(available_by_topic[topic])
        subtopic_base = target // len(subtopics)
        subtopic_remainder = target % len(subtopics)

        for index, subtopic in enumerate(subtopics):
            available = available_by_topic[topic].get(subtopic, 0)
            counts[topic][subtopic] = min(
                subtopic_base + (1 if index < subtopic_remainder else 0),
                available,
            )

        topic_used = sum(counts[topic].values())
        if topic_used < target:
            for subtopic in subtopics:
                free_space = (
                    available_by_topic[topic].get(subtopic, 0)
                    - counts[topic].get(subtopic, 0)
                )
                if free_space > 0:
                    extra = min(target - topic_used, free_space)
                    counts[topic][subtopic] += extra
                    topic_used += extra
                    if topic_used >= target:
                        break

    return counts, selected_total


def fetch_questions(level, amount, selected_topics=None, language="NL"):
    """Fetch a random set of active questions for a level and optional topic filter."""
    selected_topics = normalize_selected_topics(selected_topics)
    topic_counts, selected_total = get_topic_counts_for_level(
        level, amount, selected_topics, language=language
    )

    if selected_total == 0:
        return []

    conn = get_db_connection()
    language_clause, language_params = language_filter(conn, language)
    topic_names = list(topic_counts)
    placeholders = ", ".join("?" for _ in topic_names)
    rows = conn.execute(
        f"""
        SELECT rowid AS id, *
        FROM questions
        WHERE level = ? AND topic IN ({placeholders}) AND is_active = 1{language_clause}
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
    language_clause, language_params = language_filter(conn, language)
    rows = conn.execute(
        f"""
        SELECT rowid AS id, *
        FROM questions
        WHERE level = ? AND rowid IN ({placeholders}) AND is_active = 1{language_clause}
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

    return {
        "id": row["id"],
        "question": row["question"],
        "topic": row["topic"],
        "options": option_map,
        "is_yes_no": level == "A",
        "image_url": get_question_image_url(get_row_value(row, "image_link")),
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
    icon_path = os.path.join(app.root_path, "static", "icons", "plane-favicon.svg")
    response = send_file(icon_path, mimetype="image/svg+xml")
    response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return response


@app.route("/healthz")
def healthz():
    return {"status": "ok"}, 200


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
    session["language"] = normalize_language(request.form.get("language"))
    session.pop("exam_question_ids", None)
    target = request.referrer or url_for("index")
    if not target.startswith(request.host_url):
        target = url_for("index")
    return redirect(target)


@app.route("/access-gate", methods=["GET", "POST"])
def access_gate():
    """Show the under-construction code gate and verify submitted access codes."""
    next_target = request.values.get("next", "")
    if not is_safe_redirect_target(next_target):
        next_target = url_for("index")

    if request.method == "POST":
        submitted_code = request.form.get("code", "")
        expected_code = os.getenv("ACCESS_CODE") or ""
        if expected_code and hmac.compare_digest(submitted_code, expected_code):
            session["access_granted_at"] = time.time()
            session.permanent = True
            return redirect(next_target)

        logger.warning("Rejected access gate code attempt from %s", request.remote_addr)
        time.sleep(1)
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


@app.route("/metar", methods=["GET", "POST"])
def metar_practice():
    """Show and grade the daily METAR exercise for a Dutch airport."""
    selected_airport = str(
        request.form.get("airport") or request.args.get("airport") or session.get("metar_airport") or "EHAM"
    ).strip().upper()
    error = None
    result = None

    if selected_airport not in DUTCH_AIRPORTS:
        error = "Kies een Nederlandse luchthaven uit de lijst."
        metar = None
    else:
        try:
            metar = get_daily_metar(selected_airport, wait_for_rate_limit=False)
            session["metar_airport"] = selected_airport
        except MetarError as exc:
            metar = None
            error = str(exc)

    if request.method == "POST" and request.form.get("action") == "check" and metar and not error:
        result = grade_answers(metar, request.form)

    return render_template(
        "metar.html",
        airports=DUTCH_AIRPORTS,
        airport=selected_airport,
        metar=metar,
        result=result,
        error=error,
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


def reserve_contact_form_usage(ip_address):
    """Reserve one contact message for an IP address within the daily limit."""
    conn = get_db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
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
        conn.execute(
            "DELETE FROM contact_form_usage WHERE date < datetime('now', '-30 days')"
        )
        conn.commit()
        usage_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM contact_form_usage
            WHERE ip = ? AND date >= datetime('now', '-24 hours')
            """,
            (ip_address,),
        ).fetchone()[0]
        if usage_count >= 3:
            return False

        conn.execute("BEGIN IMMEDIATE")
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
        rendered_questions = [render_question(row, level, language) for row in questions]
        submitted_ids = {
            key.split("_", 1)[1]
            for key, value in request.form.items()
            if key.startswith("answer_") and str(value).strip()
        }

        missing_answers = [
            str(row["id"]) for row in questions if str(row["id"]) not in submitted_ids
        ]

        if missing_answers:
            exam_context = {
                "questions": rendered_questions,
                "question_groups": build_question_groups(rendered_questions),
                "total_questions": len(rendered_questions),
                "started_at": session.get("started_at", time.time()),
                "level": level,
                "error": (
                    "Niet alle vragen zijn ingevuld. Kies voor elke vraag een "
                    "antwoord voordat je verder gaat."
                ),
            }
            return render_template("exam.html", **exam_context)

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
            option_map = build_option_list(
                row["false_answers"],
                row["true_answer"],
                level=level,
                language=language,
            )

            submitted_value = request.form.get(f"answer_{row['id']}", "")
            selected_value = normalize_answer_value(submitted_value)

            is_correct = selected_value == correct_answer
            if is_correct:
                score += 1
                topic_scores[topic]["correct"] += 1

            topic_scores[topic]["total"] += 1

            results.append(
                {
                    "question": row["question"],
                    "topic": topic,
                    "selected": display_answer_value(submitted_value, language),
                    "correct": display_answer_value(row["true_answer"], language),
                    "is_correct": is_correct,
                    "options": option_map,
                    "image_url": get_question_image_url(
                        get_row_value(row, "image_link")
                    ),
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
        return url_for("static", filename=normalized.lstrip("/"))

    if normalized.startswith("static/"):
        return url_for("static", filename=normalized.replace("static/", "", 1))

    if normalized.startswith("./"):
        return url_for("static", filename=normalized.lstrip("./"))

    if normalized.startswith("images/") or normalized.startswith("question_images/"):
        return url_for("static", filename=normalized)

    return None


# purely for local development, gunicorn will be used in production
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
