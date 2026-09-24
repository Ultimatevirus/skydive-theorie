"""KNMI METAR retrieval, parsing, and answer grading."""

from datetime import date, datetime, timezone
import json
import logging
import os
import re
import time
from threading import Event
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
API_CALL_LIMIT = 3
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
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = api_key
    headers["Accept"] = accept
    request = Request(url, headers=headers)
    try:
        with opener(request, timeout=10) as response:
            return response.read()
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


def get_daily_metar(airport, today=None, opener=urlopen, wait_for_rate_limit=True):
    code = str(airport or "").strip().upper()
    if code not in DUTCH_AIRPORTS:
        raise MetarError("Kies een Nederlandse luchthaven uit de lijst.")
    day = today or datetime.now(timezone.utc).date()
    cache_key = (day.isoformat(), code)
    if cache_key in _CACHE:
        return _CACHE[cache_key]
    report = _get_cached_metar(day, code)
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


def _submitted_number(value):
    try:
        return float(str(value).replace(",", ".").strip())
    except (TypeError, ValueError):
        return None


def grade_answers(metar, submitted):
    results = []
    fields = (
        ("wind_direction", "Windrichting", 0),
        ("wind_direction_variation", "Variatie windrichting", 0),
        ("wind_speed", "Windsnelheid", 2),
        ("wind_gust", "Windstoot", 2),
        ("visibility", "Zicht", 100),
        ("temperature", "Temperatuur", 1),
        ("dew_point", "Dauwpunt", 1),
        ("qnh", "QNH", 2),
    )
    for field, label, tolerance in fields:
        expected = metar.get(field)
        answer = str(submitted.get(field, "")).strip()
        if field == "wind_direction":
            correct = expected is not None and answer.upper() == str(expected).upper()
        elif field == "wind_direction_variation":
            normalised_answer = answer.upper().replace("V", "-")
            correct = (
                not answer if expected is None else normalised_answer == str(expected).upper()
            )
        elif expected is None:
            correct = not answer
        else:
            value = _submitted_number(answer)
            correct = value is not None and abs(value - expected) <= tolerance
        results.append({"field": field, "label": label, "answer": answer or "-", "expected": expected if expected is not None else "-", "correct": correct})
    for field, label in (("weather", "Weer"), ("clouds", "Bewolking")):
        answer = str(submitted.get(field, "")).strip().upper()
        expected = str(metar.get(field) or "").upper()
        correct = answer == expected
        if field == "weather" and not answer and expected == "GEEN SIGNIFICANT WEER":
            correct = True
        results.append({"field": field, "label": label, "answer": answer or "-", "expected": expected or "-", "correct": correct})
    return results
