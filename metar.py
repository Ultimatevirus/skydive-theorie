"""KNMI METAR retrieval, parsing, and answer grading."""

from datetime import date, datetime, timezone
import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


DUTCH_AIRPORTS = {
    "EHAM": "Amsterdam Schiphol",
    "EHRD": "Rotterdam The Hague",
    "EHGG": "Groningen Eelde",
    "EHEH": "Eindhoven",
    "EHLE": "Lelystad",
    "EHWO": "Woensdrecht",
    "EHSE": "Seppe",
    "EHST": "Stadskanaal",
    "EHBD": "Budel",
    "EHDR": "Drachten",
    "EHHV": "Hilversum",
    "EHHO": "Hoogeveen",
    "EHAM": "Amsterdam Schiphol",
    "EHLW": "Leeuwarden Air Base",
    "EHTW": "Twente Air Base",
    "EHGR": "Gilze-Rijen Air Base",
    "EHVK": "Volkel Air Base",
    "EHDP": "De Peel Air Base",
    "EHMC": "De Kooy Air Base",
    "EHKD": "Deelen Air Base",
    "EHDL": "Valkenburg",
}

# The URL can be changed when the KNMI product endpoint changes. It must contain {airport}.
DEFAULT_METAR_URL = os.getenv(
    "KNMI_METAR_URL", "https://api.dataportal.nl/v1/knmi/metar/{airport}"
)
_CACHE = {}


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


def _fetch_payload(url, api_key, opener=urlopen):
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
        headers["X-API-Key"] = api_key
    request = Request(url, headers=headers)
    try:
        with opener(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        raise MetarError("De METAR-service is tijdelijk niet beschikbaar.") from exc


def _extract_report(payload):
    report = _first_value(payload, ("metar", "raw", "raw_text", "report", "text"))
    if isinstance(report, str) and report.strip():
        return report.strip()
    raise MetarError("KNMI gaf geen geldige METAR terug.")


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
        visibility = re.fullmatch(r"(\d{4})", token)
        if visibility:
            parsed["visibility"] = float(visibility.group(1))
            continue
        if token == "CAVOK":
            parsed["visibility"] = 10000.0
            parsed["clouds"] = "CAVOK"
            continue
        cloud = re.fullmatch(r"(FEW|SCT|BKN|OVC|VV)(\d{3}|///)(?:CB|TCU)?", token)
        if cloud:
            cloud_groups.append(f"{cloud.group(1)} {cloud.group(2)}")
            continue
        weather_pattern = r"[+-]?(?:MI|BC|PR|DR|BL|SH|TS|FZ)?(?:DZ|RA|SN|SG|IC|PL|GR|GS|UP|BR|FG|FU|VA|DU|SA|HZ|PO|SQ|FC|SS|DS)"
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


def get_daily_metar(airport, today=None, opener=urlopen):
    code = str(airport or "").strip().upper()
    if code not in DUTCH_AIRPORTS:
        raise MetarError("Kies een Nederlandse luchthaven uit de lijst.")
    day = today or datetime.now(timezone.utc).date()
    cache_key = (day.isoformat(), code)
    if cache_key in _CACHE:
        return _CACHE[cache_key]
    template = os.getenv("KNMI_METAR_URL", DEFAULT_METAR_URL)
    url = template.format(airport=quote(code), date=day.isoformat())
    report = _extract_report(_fetch_payload(url, os.getenv("KNMI_API_KEY", ""), opener))
    result = parse_metar(report)
    result["airport"] = code
    result["airport_name"] = DUTCH_AIRPORTS[code]
    result["date"] = day.isoformat()
    _CACHE[cache_key] = result
    return result


def _submitted_number(value):
    try:
        return float(str(value).replace(",", ".").strip())
    except (TypeError, ValueError):
        return None


def grade_answers(metar, submitted):
    results = []
    fields = (
        ("wind_direction", "Windrichting", 0),
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
        elif expected is None:
            correct = not answer
        else:
            value = _submitted_number(answer)
            correct = value is not None and abs(value - expected) <= tolerance
        results.append({"field": field, "label": label, "answer": answer or "-", "expected": expected if expected is not None else "-", "correct": correct})
    for field, label in (("weather", "Weer"), ("clouds", "Bewolking")):
        answer = str(submitted.get(field, "")).strip().upper()
        expected = str(metar.get(field) or "").upper()
        results.append({"field": field, "label": label, "answer": answer or "-", "expected": expected or "-", "correct": answer == expected})
    return results
