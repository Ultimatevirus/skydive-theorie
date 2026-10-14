"""KNMI METAR retrieval, parsing, and answer grading."""

from datetime import date, datetime, timezone
from difflib import SequenceMatcher
import json
import logging
import os
import random
import re
import time
from threading import Event, Lock
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from db import connect_db, get_db_path


DUTCH_AIRPORTS = {
    "EHAM": "Amsterdam Schiphol",
    "EHRD": "Rotterdam The Hague",
    "EHGG": "Groningen Eelde",
    "EHLE": "Lelystad",
}

DEFAULT_OPEN_DATA_URL = "https://api.dataplatform.knmi.nl/open-data"
METAR_DATASET = os.getenv("KNMI_METAR_DATASET", "metar")
METAR_VERSION = os.getenv("KNMI_METAR_VERSION", "1.0")
_CACHE = {}
_WEATHER_DEFINITIONS_CACHE = {}
_ABBREVIATIONS_CACHE = {}
_WEATHER_DEFINITIONS_LOCK = Lock()
_API_REQUEST_LOCK = Lock()
_LAST_API_REQUEST = None
_API_REQUEST_INTERVAL = 1.0
API_CALL_LIMIT = 20
API_CALL_WINDOW = 60
CLAIM_TIMEOUT = 600
logger = logging.getLogger(__name__)


class MetarError(RuntimeError):
    """Raised when the daily METAR cannot be retrieved or decoded."""


def airport_name(code):
    return DUTCH_AIRPORTS.get(str(code or "").strip().upper())


def _first_value(payload, keys):
    if isinstance(payload, dict):
        for key in keys:
            if payload.get(key) not in (None, ""):
                return payload[key]
        for value in payload.values():
            found = _first_value(value, keys)
            if found not in (None, ""):
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _first_value(value, keys)
            if found not in (None, ""):
                return found
    return None


def _request(url, api_key, opener, accept):
    global _LAST_API_REQUEST
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = api_key
    headers["Accept"] = accept
    request = Request(url, headers=headers)
    try:
        with _API_REQUEST_LOCK:
            if _LAST_API_REQUEST is not None:
                delay = _API_REQUEST_INTERVAL - (time.monotonic() - _LAST_API_REQUEST)
                if delay > 0:
                    time.sleep(delay)
            try:
                with opener(request, timeout=10) as response:
                    return response.read()
            finally:
                _LAST_API_REQUEST = time.monotonic()
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        raise MetarError("De METAR-service is tijdelijk niet beschikbaar.") from exc


def _fetch_payload(url, api_key, opener=urlopen):
    try:
        return json.loads(_request(url, api_key, opener, "application/json").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MetarError("KNMI gaf geen geldige API-respons terug.") from exc


def _extract_report(payload):
    report = _first_value(payload, ("metar", "raw", "raw_text", "report", "text"))
    if isinstance(report, str) and report.strip():
        report = report.strip()
        return _extract_xml_report(report.encode("utf-8"))
    raise MetarError("KNMI gaf geen geldige METAR terug.")


def _extract_xml_report(content):
    text = content.decode("utf-8", errors="replace").strip()
    if not text:
        raise MetarError("KNMI gaf geen geldige METAR terug.")
    comment = re.search(
        r"<!--\s*(?:METAR\s+)?([A-Z]{4}\s+\d{6}Z\b.*?)\s*-->",
        text,
        re.DOTALL | re.IGNORECASE,
    )
    if comment:
        return comment.group(1).strip()
    xml_start = text.find("<")
    if xml_start == -1:
        return text
    text = text[xml_start:]
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        raise MetarError("KNMI gaf geen geldige METAR terug.") from exc
    values = []
    for element in root.iter():
        if element.text and element.text.strip():
            values.append(element.text.strip())
        values.extend(value.strip() for value in element.attrib.values() if value.strip())
    preferred_tags = {"raw_text", "raw", "metar", "report", "description", "value", "text"}
    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1].lower()
        if tag in preferred_tags and element.text and element.text.strip():
            candidate = element.text.strip()
            if not candidate.startswith("<"):
                return candidate
    for value in values:
        match = re.search(r"\b(?:METAR\s+)?[A-Z]{4}\s+\d{6}Z\b.*", value, re.DOTALL)
        if match:
            return match.group(0).strip()
    raise MetarError("KNMI gaf geen leesbare METAR terug.")


def _normalise_report(report):
    text = str(report or "").strip()
    if text.lstrip().startswith("<"):
        return _extract_xml_report(text.encode("utf-8"))
    return text


def _get_open_data_metar(code, day, api_key, opener):
    base_url = os.getenv("KNMI_OPEN_DATA_URL", DEFAULT_OPEN_DATA_URL).rstrip("/")
    path = f"{base_url}/v1/datasets/{quote(METAR_DATASET, safe='')}/versions/{quote(METAR_VERSION, safe='')}/files"
    query = urlencode({
        "maxKeys": 1000,
        "orderBy": "created",
        "sorting": "desc",
        "begin": f"{day.isoformat()}T00:00:00Z",
        "end": f"{day.isoformat()}T23:59:59Z",
    })
    listing = _fetch_payload(f"{path}?{query}", api_key, opener)
    files = listing.get("files", []) if isinstance(listing, dict) else []
    candidates = [
        item for item in files
        if isinstance(item, dict) and code in str(item.get("filename", "")).upper()
    ]
    if not candidates:
        raise MetarError("KNMI heeft vandaag geen METAR voor deze luchthaven.")
    filename = candidates[0].get("filename")
    if not filename:
        raise MetarError("KNMI gaf geen geldig METAR-bestand terug.")
    download = _fetch_payload(f"{path}/{quote(filename, safe='')}/url", api_key, opener)
    temporary_url = download.get("temporaryDownloadUrl") if isinstance(download, dict) else None
    if not temporary_url:
        raise MetarError("KNMI gaf geen downloadlink voor de METAR terug.")
    return _extract_xml_report(_request(temporary_url, "", opener, "text/plain, application/xml"))


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_metar(report):
    """Parse common ICAO METAR groups into values used by the exercise."""
    tokens = report.upper().split()
    parsed = {
        "raw": report,
        "issued_time": None,
        "wind_direction": None,
        "wind_direction_variation": None,
        "wind_speed": None,
        "wind_gust": None,
        "visibility": None,
        "weather": "Geen significant weer",
        "clouds": "Geen bewolking gemeld",
        "temperature": None,
        "dew_point": None,
        "qnh": None,
    }
    cloud_groups = []
    weather_groups = []
    for token in tokens:
        issued = re.fullmatch(r"\d{2}([01]\d|2[0-3])([0-5]\d)Z", token)
        if issued:
            parsed["issued_time"] = f"{issued.group(1)}:{issued.group(2)}"
            continue
        wind = re.fullmatch(r"(\d{3}|VRB)(\d{2})(G(\d{2}))?(KT|MPS|KMH)", token)
        if wind:
            parsed["wind_direction"] = wind.group(1)
            speed = float(wind.group(2))
            gust = _number(wind.group(4))
            factor = {"KT": 1, "MPS": 1.94384, "KMH": 0.539957}[wind.group(5)]
            parsed["wind_speed"] = round(speed * factor, 1)
            parsed["wind_gust"] = round(gust * factor, 1) if gust is not None else None
            continue
        wind_variation = re.fullmatch(r"(\d{3})V(\d{3})", token)
        if wind_variation:
            parsed["wind_direction_variation"] = (
                f"{wind_variation.group(1)}-{wind_variation.group(2)}"
            )
            continue
        visibility = re.fullmatch(r"(\d{4})", token)
        if visibility:
            parsed["visibility"] = float(visibility.group(1))
            continue
        if token == "CAVOK":
            parsed["visibility"] = 10000.0
            parsed["clouds"] = "CAVOK"
            continue
        cloud = re.fullmatch(r"(FEW|SCT|BKN|OVC|VV)(\d{3}|///)(?:CB|TCU)?(?:///)?", token)
        if cloud:
            cloud_groups.append(f"{cloud.group(1)} {cloud.group(2)}")
            continue
        weather_pattern = r"[+-]?(?:(?:MI|BC|PR|DR|BL|SH|TS|FZ)?(?:DZ|RA|SN|SG|IC|PL|GR|GS|UP|BR|FG|FU|VA|DU|SA|HZ|PO|SQ|FC|SS|DS))+"
        if re.fullmatch(weather_pattern, token):
            weather_groups.append(token)
            continue
        temperature = re.fullmatch(r"(M?\d{2})/(M?\d{2}|//)", token)
        if temperature:
            parsed["temperature"] = _signed_number(temperature.group(1))
            parsed["dew_point"] = _signed_number(temperature.group(2))
            continue
        qnh = re.fullmatch(r"Q(\d{4})", token)
        if qnh:
            parsed["qnh"] = float(qnh.group(1))
    if weather_groups:
        parsed["weather"] = " ".join(weather_groups)
    if cloud_groups:
        parsed["clouds"] = ", ".join(cloud_groups)
    return parsed


def _signed_number(value):
    if value in (None, "//"):
        return None
    return -float(value[1:]) if value.startswith("M") else float(value)


def _connect_db():
    """Open a connection with the shared SQLite busy handling."""
    return connect_db(path=get_db_path())


def _ensure_metar_tables(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS metar (
            datetime TEXT NOT NULL,
            airport TEXT NOT NULL,
            metar TEXT NOT NULL,
            PRIMARY KEY (datetime, airport)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS metar_fetch_claim (
            datetime TEXT NOT NULL,
            airport TEXT NOT NULL,
            claimed_at REAL NOT NULL,
            PRIMARY KEY (datetime, airport)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS metar_api_call (
            called_at REAL NOT NULL
        )
        """
    )


def _wait_for_api_slot(wait=True):
    while True:
        now = time.time()
        conn = _connect_db()
        try:
            _ensure_metar_tables(conn)
            conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "DELETE FROM metar_api_call WHERE called_at <= ?",
                (now - API_CALL_WINDOW,),
            )
            count = conn.execute("SELECT COUNT(*) FROM metar_api_call").fetchone()[0]
            if count < API_CALL_LIMIT:
                conn.execute("INSERT INTO metar_api_call (called_at) VALUES (?)", (now,))
                conn.commit()
                return True
            oldest = conn.execute("SELECT MIN(called_at) FROM metar_api_call").fetchone()[0]
            conn.commit()
        finally:
            conn.close()
        if not wait:
            return False
        time.sleep(max(0.01, oldest + API_CALL_WINDOW - time.time()))


def _claim_metar_fetch(day, code, wait=True):
    while True:
        conn = _connect_db()
        try:
            _ensure_metar_tables(conn)
            conn.execute("BEGIN IMMEDIATE")
            cached = conn.execute(
                "SELECT metar FROM metar WHERE datetime = ? AND airport = ?",
                (day.isoformat(), code),
            ).fetchone()
            if cached:
                conn.commit()
                return False
            now = time.time()
            conn.execute(
                "DELETE FROM metar_fetch_claim WHERE datetime = ? AND airport = ? AND claimed_at < ?",
                (day.isoformat(), code, now - CLAIM_TIMEOUT),
            )
            claimed = conn.execute(
                "INSERT OR IGNORE INTO metar_fetch_claim (datetime, airport, claimed_at) VALUES (?, ?, ?)",
                (day.isoformat(), code, now),
            ).rowcount == 1
            conn.commit()
            if claimed:
                return True
        finally:
            conn.close()
        if not wait:
            return None
        time.sleep(0.2)


def _release_metar_fetch(day, code, report=None):
    conn = _connect_db()
    try:
        _ensure_metar_tables(conn)
        if report is not None:
            conn.execute(
                "INSERT OR REPLACE INTO metar (datetime, airport, metar) VALUES (?, ?, ?)",
                (day.isoformat(), code, report),
            )
        conn.execute(
            "DELETE FROM metar_fetch_claim WHERE datetime = ? AND airport = ?",
            (day.isoformat(), code),
        )
        conn.commit()
    finally:
        conn.close()


def _get_cached_metar(day, code):
    conn = _connect_db()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS metar (
                datetime TEXT NOT NULL,
                airport TEXT NOT NULL,
                metar TEXT NOT NULL,
                PRIMARY KEY (datetime, airport)
            )
            """
        )
        row = conn.execute(
            "SELECT metar FROM metar WHERE datetime = ? AND airport = ?",
            (day.isoformat(), code),
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def _store_metar(day, code, report):
    conn = _connect_db()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS metar (
                datetime TEXT NOT NULL,
                airport TEXT NOT NULL,
                metar TEXT NOT NULL,
                PRIMARY KEY (datetime, airport)
            )
            """
        )
        values = (day.isoformat(), code, report)
        updated = conn.execute(
            "UPDATE metar SET metar = ? WHERE datetime = ? AND airport = ?",
            (report, day.isoformat(), code),
        )
        if updated.rowcount == 0:
            conn.execute(
                "INSERT INTO metar (datetime, airport, metar) VALUES (?, ?, ?)",
                values,
            )
        conn.commit()
    finally:
        conn.close()


def _build_result(report, code, day):
    result = parse_metar(report)
    result["airport"] = code
    result["airport_name"] = DUTCH_AIRPORTS[code]
    result["date"] = day.isoformat()
    return result


def get_daily_metar(airport, today=None, opener=urlopen, wait_for_rate_limit=True, cache_only=False):
    code = str(airport or "").strip().upper()
    if code not in DUTCH_AIRPORTS:
        raise MetarError("Kies een Nederlandse luchthaven uit de lijst.")
    day = today or datetime.now(timezone.utc).date()
    cache_key = (day.isoformat(), code)
    if cache_key in _CACHE:
        return _CACHE[cache_key]
    report = _get_cached_metar(day, code)
    if report is None and cache_only:
        raise MetarError("De METAR wordt nog opgehaald. Probeer het over een minuut opnieuw.")
    if report is not None:
        normalised_report = _normalise_report(report)
        if normalised_report != report:
            _store_metar(day, code, normalised_report)
        report = normalised_report
        result = _build_result(report, code, day)
        _CACHE[cache_key] = result
        return result
    claimed = _claim_metar_fetch(day, code, wait=wait_for_rate_limit)
    if claimed is False:
        report = _get_cached_metar(day, code)
        if report is None:
            raise MetarError("De METAR-ophaling is onverwacht gestopt.")
        normalised_report = _normalise_report(report)
        if normalised_report != report:
            _store_metar(day, code, normalised_report)
        result = _build_result(normalised_report, code, day)
        _CACHE[cache_key] = result
        return result
    if claimed is None:
        report = _get_cached_metar(day, code)
        if report is not None:
            normalised_report = _normalise_report(report)
            if normalised_report != report:
                _store_metar(day, code, normalised_report)
            result = _build_result(normalised_report, code, day)
            _CACHE[cache_key] = result
            return result
        raise MetarError("Er wordt al een METAR opgehaald. Probeer het zo opnieuw.")
    api_key = os.getenv("KNMI_API_KEY", "").strip()
    try:
        legacy_template = os.getenv("KNMI_METAR_URL", "")
        if legacy_template and "{airport}" in legacy_template:
            url = legacy_template.format(airport=quote(code), date=day.isoformat())
            report = _extract_report(_fetch_payload(url, api_key, opener))
        else:
            if not api_key:
                raise MetarError("KNMI_API_KEY is niet ingesteld.")
            if not _wait_for_api_slot(wait_for_rate_limit):
                raise MetarError("De METAR-service is tijdelijk niet beschikbaar.")
            report = _get_open_data_metar(code, day, api_key, opener)
        _release_metar_fetch(day, code, report)
    except Exception:
        _release_metar_fetch(day, code)
        raise
    result = _build_result(report, code, day)
    _CACHE[cache_key] = result
    return result


def refresh_daily_metars(day=None, opener=urlopen):
    """Fetch each unique Dutch airport's METAR for the given UTC day."""
    day = day or datetime.now(timezone.utc).date()
    for code in dict.fromkeys(DUTCH_AIRPORTS):
        try:
            get_daily_metar(code, day, opener=opener)
        except MetarError:
            logger.exception("METAR refresh failed for %s", code)


def metar_refresh_loop(stop_event=None, opener=urlopen):
    """Refresh all airports immediately and then every 30 minutes."""
    stop_event = stop_event or Event()
    while not stop_event.is_set():
        day = datetime.now(timezone.utc).date()
        refresh_daily_metars(day, opener=opener)
        stop_event.wait(1800)


NO_GUSTS = "Geen windstoten"
NO_VARIATION = "Geen variatie"
NO_WEATHER = "Geen significant weer"
NO_CLOUDS = "Geen bewolking gemeld"
OPTION_COUNT = 4
CLOUD_AMOUNTS = ("FEW", "SCT", "BKN", "OVC")
WEATHER_DESCRIPTORS = {"MI", "BC", "PR", "DR", "BL", "SH", "FZ"}


def new_question_seed():
    return os.urandom(8).hex()


def _fmt_number(value):
    value = float(value)
    return str(int(value)) if value == int(value) else f"{value:.1f}"


def _near_numbers(rng, value, step, lo=None, hi=None, wrap=None, count=3):
    """Pick `count` distinct numbers close to `value`, spaced in multiples of `step`."""
    candidates = []
    for k in range(1, 9):
        for sign in (-1, 1):
            candidate = value + sign * k * step
            if wrap:
                candidate %= wrap
            if candidate == value or candidate in candidates:
                continue
            if (lo is not None and candidate < lo) or (hi is not None and candidate > hi):
                continue
            candidates.append(candidate)
    near = candidates[: count + 2]
    rng.shuffle(near)
    return near[:count]


def _similar_text(rng, correct, pool, count=3):
    """Pick `count` pool entries, favouring those that look most like `correct`."""
    ranked = sorted(
        (item for item in dict.fromkeys(pool) if item != correct),
        key=lambda item: -(SequenceMatcher(None, correct, item).ratio() + rng.random() * 0.3),
    )
    return ranked[:count]


def _mutate_clouds(rng, groups):
    mutated = list(groups)
    for _ in range(rng.choice((1, 1, 2))):
        index = rng.randrange(len(mutated))
        amount, height = mutated[index].split()
        if height.isdigit() and rng.random() < 0.6:
            new_height = max(1, int(height) + rng.choice((-3, -2, -1, 1, 2, 3)) * 5)
            mutated[index] = f"{amount} {new_height:03d}"
        else:
            options = [a for a in CLOUD_AMOUNTS if a != amount]
            mutated[index] = f"{rng.choice(options)} {height}"
    return ", ".join(mutated)


def _cloud_options(rng, clouds):
    if clouds in ("CAVOK", NO_CLOUDS) or not clouds:
        pool = [f"{a} {h:03d}" for a in CLOUD_AMOUNTS for h in (15, 20, 25, 30, 40)]
        return rng.sample(pool, 3)
    groups = clouds.split(", ")
    distractors = []
    for _ in range(40):
        candidate = _mutate_clouds(rng, groups)
        if candidate != clouds and candidate not in distractors:
            distractors.append(candidate)
        if len(distractors) == 3:
            break
    return distractors


def load_weather_definitions(path=None):
    """Read {abbreviation: (NL, EN)} from the metar_weather_codes table."""
    database_path = os.path.abspath(path or get_db_path())
    stat = os.stat(database_path)
    cache_key = (database_path, stat.st_mtime_ns, stat.st_size)
    with _WEATHER_DEFINITIONS_LOCK:
        if cache_key in _WEATHER_DEFINITIONS_CACHE:
            return _WEATHER_DEFINITIONS_CACHE[cache_key]

        conn = connect_db(path=database_path)
        try:
            rows = conn.execute(
                "SELECT METAR_abreviation, NL_definition, EN_definition FROM metar_weather_codes"
            ).fetchall()
        finally:
            conn.close()
        definitions = {row[0]: (row[1], row[2]) for row in rows}
        _WEATHER_DEFINITIONS_CACHE.clear()
        _WEATHER_DEFINITIONS_CACHE[cache_key] = definitions
        return definitions


def _describe_group(group, definitions, language):
    """Explain one weather group, composing it from its parts when it has no row of its own."""
    index = 1 if str(language).upper() == "EN" else 0
    if group in definitions:
        return definitions[group][index]
    parts = []
    rest = group
    if rest[:1] in ("-", "+") and rest[:1] in definitions:
        parts.append(definitions[rest[:1]][index])
        rest = rest[1:]
    for position in range(0, len(rest), 2):
        code = rest[position:position + 2]
        parts.append(definitions[code][index] if code in definitions else code)
    return " ".join(parts)


def _standalone_codes(definitions):
    """Codes that can be reported on their own (not intensity or descriptor prefixes)."""
    return sorted(
        code for code in definitions
        if len(code) > 1 and code not in WEATHER_DESCRIPTORS
    )


def _weather_options(rng, weather, definitions, language):
    """Return (correct meaning, wrong meanings) for the reported weather groups."""
    describe = lambda groups: "; ".join(_describe_group(g, definitions, language) for g in groups)
    pool = _standalone_codes(definitions)
    if weather == NO_WEATHER:
        return NO_WEATHER, [describe([code]) for code in rng.sample(pool, 3)]

    groups = weather.split()
    correct = describe(groups)
    candidates = []
    for index, group in enumerate(groups):
        for alt in _similar_text(rng, group, [c for c in pool if c not in groups], count=4):
            candidates.append(describe(groups[:index] + [alt] + groups[index + 1:]))
    if len(groups) > 1:
        rng.shuffle(candidates)
    candidates += [describe([code]) for code in rng.sample(pool, min(len(pool), 6))]
    return correct, [c for c in dict.fromkeys(candidates) if c != correct]


def _build_question(rng, field, label, text, correct, distractors):
    distractors = [d for d in dict.fromkeys(distractors) if d != correct][: OPTION_COUNT - 1]
    options = distractors + [correct]
    rng.shuffle(options)
    return {"field": field, "label": label, "question": text, "options": options, "correct": correct}


def build_questions(metar, seed, language="NL", definitions=None):
    """Build multiple-choice questions for a METAR; the same seed yields the same questions."""
    definitions = definitions if definitions is not None else load_weather_definitions()
    rng = random.Random(f"{seed}:{metar.get('raw', '')}")
    questions = []

    def add(field, label, text, correct, distractors):
        questions.append(_build_question(rng, field, label, text, correct, distractors))

    issued = metar.get("issued_time")
    if issued:
        hours, minutes = (int(part) for part in issued.split(":"))
        total = hours * 60 + minutes
        add(
            "issued_time", "Uitgiftetijd", "Hoe laat is deze METAR uitgegeven?",
            f"{issued} UTC",
            [f"{v // 60:02d}:{v % 60:02d} UTC" for v in _near_numbers(rng, total, 5, wrap=1440)],
        )

    direction = metar.get("wind_direction")
    if direction is not None:
        if direction == "VRB":
            correct = "Variabel (VRB)"
            others = [f"{d:03d}°" for d in rng.sample(range(10, 361, 10), 3)]
        else:
            degrees = int(direction)
            correct = f"{degrees:03d}°"
            others = [f"{(v - 1) % 360 + 1:03d}°" for v in _near_numbers(rng, degrees, 10, wrap=360)]
        add("wind_direction", "Windrichting", "Uit welke richting waait de wind op de grond?", correct, others)

    speed = metar.get("wind_speed")
    if speed is not None:
        add(
            "wind_speed", "Windsnelheid", "Hoeveel wind is er op de grond?",
            f"{_fmt_number(speed)} kt",
            [f"{_fmt_number(v)} kt" for v in _near_numbers(rng, speed, 1, lo=0)],
        )
        gust = metar.get("wind_gust")
        if gust is None:
            others = [f"{_fmt_number(speed + rng.randint(5, 15))} kt" for _ in range(8)]
            add("wind_gust", "Windstoot", "Wat is de maximale windstoot?", NO_GUSTS, others)
        else:
            add(
                "wind_gust", "Windstoot", "Wat is de maximale windstoot?",
                f"{_fmt_number(gust)} kt",
                [f"{_fmt_number(v)} kt" for v in _near_numbers(rng, gust, 1, lo=speed + 1)] + [NO_GUSTS],
            )

    if direction is not None:
        variation = metar.get("wind_direction_variation")
        text = "Varieert de windrichting? Zo ja, tussen welke richtingen?"
        if variation is None:
            others = []
            for _ in range(8):
                low = rng.randrange(10, 361, 10)
                others.append(f"{low:03d}°–{(low + rng.choice((40, 50, 60, 70)) - 1) % 360 + 1:03d}°")
            add("wind_direction_variation", "Variatie windrichting", text, NO_VARIATION, others)
        else:
            low, high = (int(part) for part in variation.split("-"))
            shift = lambda v: (v - 1) % 360 + 1
            correct = f"{low:03d}°–{high:03d}°"
            others = [
                f"{shift(low + a):03d}°–{shift(high + b):03d}°"
                for a, b in ((10, 10), (-10, -10), (10, 0), (0, -10), (-20, 0), (0, 20), (20, 20), (-10, 10))
            ]
            rng.shuffle(others)
            add("wind_direction_variation", "Variatie windrichting", text, correct, others[:2] + [NO_VARIATION])

    visibility = metar.get("visibility")
    if visibility is not None:
        if visibility >= 9999:
            correct = "10 km of meer"
            others = [f"{v} m" for v in rng.sample((9000, 8000, 7000, 6000, 5000), 3)]
        else:
            correct = f"{int(visibility)} m"
            step = max(100, round(visibility / 10, -2))
            others = [f"{int(v)} m" for v in _near_numbers(rng, visibility, step, lo=100, hi=9000)]
        add("visibility", "Zicht", "Wat is het zicht?", correct, others)

    for field, label, text in (
        ("temperature", "Temperatuur", "Wat is de temperatuur?"),
        ("dew_point", "Dauwpunt", "Wat is het dauwpunt?"),
    ):
        value = metar.get(field)
        if value is not None:
            add(field, label, text, f"{_fmt_number(value)} °C",
                [f"{_fmt_number(v)} °C" for v in _near_numbers(rng, value, 1)])

    qnh = metar.get("qnh")
    if qnh is not None:
        add("qnh", "QNH", "Wat is de QNH?", f"{_fmt_number(qnh)} hPa",
            [f"{_fmt_number(v)} hPa" for v in _near_numbers(rng, qnh, 1)])

    weather = metar.get("weather") or NO_WEATHER
    correct, others = _weather_options(rng, weather, definitions, language)
    if weather == NO_WEATHER:
        text = "Welk significant weer wordt gemeld?"
    elif str(language).upper() == "EN":
        text = f"What does the weather group \"{weather}\" in this METAR mean?"
    else:
        text = f"Wat betekent het weer \"{weather}\" in deze METAR?"
    add("weather", "Weer", text, correct, others)

    clouds = metar.get("clouds") or NO_CLOUDS
    add("clouds", "Bewolking", "Welke bewolking wordt gemeld?", clouds, _cloud_options(rng, clouds))
    return questions


def grade_answers(questions, submitted):
    results = []
    for question in questions:
        answer = str(submitted.get(f"q_{question['field']}", "")).strip()
        results.append({
            "field": question["field"],
            "label": question["label"],
            "question": question["question"],
            "answer": answer or "-",
            "expected": question["correct"],
            "is_unanswered": not answer,
            "correct": answer == question["correct"],
        })
    return results


ABBREVIATION_QUESTION_COUNT = 10

def load_abbreviations(path=None):
    """Read {abbreviation: (category, NL, EN, example)} from the metar_abbreviations table."""
    database_path = os.path.abspath(path or get_db_path())
    stat = os.stat(database_path)
    cache_key = (database_path, stat.st_mtime_ns, stat.st_size)
    with _WEATHER_DEFINITIONS_LOCK:
        if cache_key in _ABBREVIATIONS_CACHE:
            return _ABBREVIATIONS_CACHE[cache_key]

        conn = connect_db(path=database_path)
        try:
            rows = conn.execute(
                "SELECT METAR_abreviation, category, NL_definition, EN_definition, example FROM metar_abbreviations"
            ).fetchall()
        finally:
            conn.close()
        abbreviations = {row[0]: (row[1], row[2], row[3], row[4]) for row in rows}
        _ABBREVIATIONS_CACHE.clear()
        _ABBREVIATIONS_CACHE[cache_key] = abbreviations
        return abbreviations


def _abbreviation_entries(definitions, abbreviations, language):
    """Return {code: (category, meaning, example)} combining database abbreviations and weather codes."""
    index = 1 if str(language).upper() == "EN" else 0
    entries = {
        code: (category, (nl, en)[index], example)
        for code, (category, nl, en, example) in abbreviations.items()
    }
    for code in _standalone_codes(definitions):
        if code not in entries:
            entries[code] = ("weather", definitions[code][index], _weather_example(code))
    return entries


def _weather_example(code):
    """Build an example METAR that reports the given weather code."""
    if "FG" in code:
        visibility = "0400"
    elif "BR" in code:
        visibility = "3000"
    elif "HZ" in code:
        visibility = "6000"
    else:
        visibility = "8000"
    clouds = "BKN015CB" if ("TS" in code or "SH" in code) else "SCT020"
    return f"EHAM 121250Z 24008KT {visibility} [[{code}]] {clouds} 12/09 Q1013"


def _split_example(example):
    """Split an example on its [[highlighted]] token into (before, token, after)."""
    before, _, rest = example.partition("[[")
    token, _, after = rest.partition("]]")
    return before, token, after


def build_abbreviation_questions(
    seed, language="NL", definitions=None, count=ABBREVIATION_QUESTION_COUNT, abbreviations=None
):
    """Build 'what does this abbreviation mean' questions; the same seed yields the same questions."""
    definitions = definitions if definitions is not None else load_weather_definitions()
    abbreviations = abbreviations if abbreviations is not None else load_abbreviations()
    rng = random.Random(f"abbr:{seed}")
    entries = _abbreviation_entries(definitions, abbreviations, language)
    english = str(language).upper() == "EN"

    # Mix categories so a set is not made of weather codes only.
    by_category = {}
    for code, (category, _, _example) in entries.items():
        by_category.setdefault(category, []).append(code)
    chosen = []
    pool = {cat: rng.sample(codes, len(codes)) for cat, codes in by_category.items()}
    while len(chosen) < min(count, len(entries)):
        for category in rng.sample(list(pool), len(pool)):
            if pool[category] and len(chosen) < count:
                chosen.append(pool[category].pop())
    rng.shuffle(chosen)

    questions = []
    for code in chosen:
        category, correct, example = entries[code]
        same = [c for c, (cat, _, _example) in entries.items() if cat == category and c != code]
        others = [c for c in entries if c != code and c not in same]
        ranked = _similar_text(rng, code, same, count=len(same)) + rng.sample(others, len(others))
        distractors = []
        for other in ranked:
            meaning = entries[other][1]
            if meaning != correct and meaning not in distractors:
                distractors.append(meaning)
            if len(distractors) == OPTION_COUNT - 1:
                break
        text = f"What does \"{code}\" mean in a METAR?" if english else f"Wat betekent \"{code}\" in een METAR?"
        question = _build_question(rng, f"abbr_{code}", code, text, correct, distractors)
        question["example_parts"] = _split_example(example)
        questions.append(question)
    return questions
