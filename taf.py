"""KNMI TAF retrieval, parsing, and answer grading."""

from datetime import datetime, timezone
import logging
import os
import random
import re
import time
from threading import Event
from urllib.parse import quote, urlencode
from urllib.request import urlopen

from metar import (
    CLAIM_TIMEOUT,
    DEFAULT_OPEN_DATA_URL,
    DUTCH_AIRPORTS,
    MetarError,
    OPTION_COUNT,
    _build_question,
    _connect_db,
    _fetch_payload,
    _near_numbers,
    _request,
    _similar_text,
    _split_example,
    _standalone_codes,
    build_questions as build_metar_questions,
    grade_answers,
    load_abbreviations,
    load_weather_definitions,
    new_question_seed,
    parse_metar,
)

__all__ = [
    "DUTCH_AIRPORTS",
    "TafError",
    "build_abbreviation_questions",
    "build_questions",
    "get_daily_taf",
    "grade_answers",
    "load_abbreviations",
    "load_weather_definitions",
    "new_question_seed",
    "parse_taf",
    "refresh_daily_tafs",
    "taf_refresh_loop",
]

TAF_DATASET = os.getenv("KNMI_TAF_DATASET", "taf")
TAF_VERSION = os.getenv("KNMI_TAF_VERSION", "1.0")
ABBREVIATION_QUESTION_COUNT = 10
CHANGE_CODES = ("BECMG", "TEMPO", "FM", "PROB30", "PROB40")
COMPOUND_CHANGES = ("PROB30 TEMPO", "PROB40 TEMPO")
METAR_ONLY_ABBREVIATIONS = {
    "METAR", "SPECI", "AUTO", "NOSIG", "RMK", "RVR", "QNH", "RE", "NDV", "NCD", "M", "AT", "TL", "P",
}
TAF_EXTRA_ABBREVIATIONS = {
    "TAF": ("report", "Terminal Aerodrome Forecast (luchthavenverwachting)", "Terminal Aerodrome Forecast"),
    "PROB30": ("trend", "Kans van 30% op de beschreven situatie", "30% probability of the described conditions"),
    "PROB40": ("trend", "Kans van 40% op de beschreven situatie", "40% probability of the described conditions"),
    "NSW": ("weather", "Geen significant weer meer", "No significant weather"),
}
_TAF_BASE = "TAF EHAM 121100Z 1212/1318 24010KT 9999 SCT025"
TAF_EXAMPLES = {
    "TAF": "[[TAF]] EHAM 121100Z 1212/1318 24010KT 9999 SCT025",
    "BECMG": f"{_TAF_BASE} [[BECMG]] 1214/1216 27015G25KT",
    "TEMPO": f"{_TAF_BASE} [[TEMPO]] 1214/1218 4000 SHRA BKN012CB",
    "FM": f"{_TAF_BASE} [[FM]]121600 30012KT 9999 BKN020",
    "PROB30": f"{_TAF_BASE} [[PROB30]] TEMPO 1315/1318 TSRA BKN015CB",
    "PROB40": f"{_TAF_BASE} [[PROB40]] 1318/1322 4000 -SHRA",
    "NSW": f"{_TAF_BASE} TEMPO 1214/1218 4000 RA BECMG 1218/1220 9999 [[NSW]]",
    "CAVOK": "TAF EHAM 121100Z 1212/1318 24010KT [[CAVOK]]",
    "COR": "TAF [[COR]] EHAM 121100Z 1212/1318 24010KT 9999 SCT025",
}
_CHANGE_START = re.compile(r"BECMG|TEMPO|FM\d{6}|PROB[34]0")
_PERIOD = re.compile(r"(\d{2})(\d{2})/(\d{2})(\d{2})")
_TEMPERATURE_GROUP = re.compile(r"T[XN]M?\d{2}/\d{4}Z")
_CACHE = {}
logger = logging.getLogger(__name__)


class TafError(MetarError):
    """Raised when the daily TAF cannot be retrieved or decoded."""


def _ensure_taf_tables(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS taf (
            datetime TEXT NOT NULL,
            airport TEXT NOT NULL,
            taf TEXT NOT NULL,
            PRIMARY KEY (datetime, airport)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS taf_fetch_claim (
            datetime TEXT NOT NULL,
            airport TEXT NOT NULL,
            claimed_at REAL NOT NULL,
            PRIMARY KEY (datetime, airport)
        )
        """
    )


def _extract_taf(content, code):
    """Find the TAF for an airport in a downloaded file; returns None when it is absent."""
    text = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else str(content)
    match = re.search(
        rf"(?:TAF\s+(?:(?:AMD|COR|RTD)\s+)*)?{code}\s+\d{{6}}Z\s+\d{{4}}/\d{{4}}.*?(?==|<|-->|$)",
        text,
        re.DOTALL | re.IGNORECASE,
    )
    if not match:
        return None
    report = " ".join(match.group(0).split()).upper()
    return report if report.startswith("TAF") else f"TAF {report}"


def _get_open_data_taf(code, day, api_key, opener, wait_for_rate_limit=True):
    base_url = os.getenv("KNMI_OPEN_DATA_URL", DEFAULT_OPEN_DATA_URL).rstrip("/")
    path = f"{base_url}/v1/datasets/{quote(TAF_DATASET, safe='')}/versions/{quote(TAF_VERSION, safe='')}/files"
    query = urlencode({
        "maxKeys": 1000,
        "orderBy": "created",
        "sorting": "desc",
        "begin": f"{day.isoformat()}T00:00:00Z",
        "end": f"{day.isoformat()}T23:59:59Z",
    })
    try:
        listing = _fetch_payload(f"{path}?{query}", api_key, opener, wait_for_rate_limit)
        files = [
            item for item in (listing.get("files", []) if isinstance(listing, dict) else [])
            if isinstance(item, dict) and item.get("filename")
        ]
        named = [item for item in files if code in str(item["filename"]).upper()]
        for item in (named or files):
            download = _fetch_payload(
                f"{path}/{quote(item['filename'], safe='')}/url", api_key, opener,
                wait_for_rate_limit,
            )
            url = download.get("temporaryDownloadUrl") if isinstance(download, dict) else None
            if not url:
                continue
            report = _extract_taf(
                _request(url, "", opener, "text/plain, application/xml", wait_for_rate_limit),
                code,
            )
            if report:
                return report
    except TafError:
        raise
    except MetarError as exc:
        raise TafError("De TAF-service is tijdelijk niet beschikbaar.") from exc
    raise TafError("KNMI heeft vandaag geen TAF voor deze luchthaven.")


def _absolute_hours(day, hour):
    return int(day) * 24 + int(hour)


def parse_taf(report):
    """Parse a TAF into header values, base conditions and change groups."""
    raw = " ".join(str(report or "").replace("=", " ").upper().split())
    tokens = raw.split()
    parsed = {
        "raw": raw,
        "airport": None,
        "issued_time": None,
        "issued_day": None,
        "valid_from": None,
        "valid_to": None,
        "valid_period": None,
        "valid_hours": None,
        "changes": [],
        "lines": [raw],
    }
    index = 0
    if tokens[index:index + 1] == ["TAF"]:
        index += 1
    while tokens[index:index + 1] and tokens[index] in ("AMD", "COR", "RTD"):
        index += 1
    if tokens[index:index + 1] and re.fullmatch(r"[A-Z]{4}", tokens[index]):
        parsed["airport"] = tokens[index]
        index += 1
    issued = re.fullmatch(r"(\d{2})([01]\d|2[0-3])([0-5]\d)Z", tokens[index]) if index < len(tokens) else None
    if issued:
        parsed["issued_day"] = int(issued.group(1))
        parsed["issued_time"] = f"{issued.group(2)}:{issued.group(3)}"
        index += 1
    period = _PERIOD.fullmatch(tokens[index]) if index < len(tokens) else None
    if period:
        from_day, from_hour, to_day, to_hour = (int(value) for value in period.groups())
        parsed["valid_from"] = (from_day, from_hour)
        parsed["valid_to"] = (to_day, to_hour)
        parsed["valid_period"] = tokens[index]
        hours = (_absolute_hours(to_day, to_hour) - _absolute_hours(from_day, from_hour)) % (31 * 24)
        parsed["valid_hours"] = hours or None
        index += 1

    base_tokens = []
    groups = []
    for token in tokens[index:]:
        if _CHANGE_START.fullmatch(token) and not (token == "TEMPO" and groups and len(groups[-1]) == 1 and groups[-1][0].startswith("PROB")):
            groups.append([token])
        elif groups:
            groups[-1].append(token)
        else:
            base_tokens.append(token)

    parsed.update({
        key: value for key, value in parse_metar(" ".join(_conditions(base_tokens))).items()
        if key not in ("raw", "issued_time", "temperature", "dew_point", "qnh", "wind_direction_variation")
    })
    header = tokens[:index]
    parsed["lines"] = [" ".join(header + base_tokens)]
    for group in groups:
        change_type = group[0]
        rest = group[1:]
        if change_type.startswith("PROB") and rest[:1] == ["TEMPO"]:
            change_type = f"{change_type} TEMPO"
            rest = rest[1:]
        period_text = None
        if change_type.startswith("FM"):
            period_text, change_type = change_type, "FM"
        elif rest and _PERIOD.fullmatch(rest[0]):
            period_text, rest = rest[0], rest[1:]
        conditions = parse_metar(" ".join(_conditions(rest)))
        parsed["changes"].append({
            "type": change_type,
            "period": period_text,
            "raw": " ".join(_conditions(group)),
            "nsw": "NSW" in rest,
            "wind_direction": conditions["wind_direction"],
            "wind_speed": conditions["wind_speed"],
            "wind_gust": conditions["wind_gust"],
            "visibility": conditions["visibility"],
            "weather": conditions["weather"],
            "clouds": conditions["clouds"],
        })
        parsed["lines"].append(" ".join(group))
    return parsed


def _conditions(tokens):
    return [token for token in tokens if not _TEMPERATURE_GROUP.fullmatch(token)]


def _build_result(report, code, day):
    result = parse_taf(report)
    result["airport"] = code
    result["airport_name"] = DUTCH_AIRPORTS[code]
    result["date"] = day.isoformat()
    return result


def _get_cached_taf(day, code):
    conn = _connect_db()
    try:
        _ensure_taf_tables(conn)
        row = conn.execute(
            "SELECT taf FROM taf WHERE datetime = ? AND airport = ?",
            (day.isoformat(), code),
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def _claim_taf_fetch(day, code, wait=True):
    """Return True when this caller may fetch, False when a cached TAF exists, None when busy."""
    while True:
        conn = _connect_db()
        try:
            _ensure_taf_tables(conn)
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute(
                "SELECT 1 FROM taf WHERE datetime = ? AND airport = ?", (day.isoformat(), code)
            ).fetchone():
                conn.commit()
                return False
            now = time.time()
            conn.execute(
                "DELETE FROM taf_fetch_claim WHERE datetime = ? AND airport = ? AND claimed_at < ?",
                (day.isoformat(), code, now - CLAIM_TIMEOUT),
            )
            claimed = conn.execute(
                "INSERT OR IGNORE INTO taf_fetch_claim (datetime, airport, claimed_at) VALUES (?, ?, ?)",
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


def _release_taf_fetch(day, code, report=None):
    conn = _connect_db()
    try:
        _ensure_taf_tables(conn)
        if report is not None:
            conn.execute(
                "INSERT OR REPLACE INTO taf (datetime, airport, taf) VALUES (?, ?, ?)",
                (day.isoformat(), code, report),
            )
        conn.execute(
            "DELETE FROM taf_fetch_claim WHERE datetime = ? AND airport = ?",
            (day.isoformat(), code),
        )
        conn.commit()
    finally:
        conn.close()


def get_daily_taf(airport, today=None, opener=urlopen, wait_for_rate_limit=True, cache_only=False):
    code = str(airport or "").strip().upper()
    if code not in DUTCH_AIRPORTS:
        raise TafError("Kies een Nederlandse luchthaven uit de lijst.")
    day = today or datetime.now(timezone.utc).date()
    cache_key = (day.isoformat(), code)
    if cache_key in _CACHE:
        return _CACHE[cache_key]

    def cached_result(report):
        result = _build_result(report, code, day)
        _CACHE[cache_key] = result
        return result

    report = _get_cached_taf(day, code)
    if report is not None:
        return cached_result(report)
    if cache_only:
        raise TafError("De TAF wordt nog opgehaald. Probeer het over een minuut opnieuw.")
    claimed = _claim_taf_fetch(day, code, wait=wait_for_rate_limit)
    if claimed is not True:
        report = _get_cached_taf(day, code)
        if report is not None:
            return cached_result(report)
        raise TafError(
            "De TAF-ophaling is onverwacht gestopt." if claimed is False
            else "Er wordt al een TAF opgehaald. Probeer het zo opnieuw."
        )
    try:
        api_key = os.getenv("KNMI_API_KEY", "").strip()
        if not api_key:
            raise TafError("KNMI_API_KEY is niet ingesteld.")
        report = _get_open_data_taf(code, day, api_key, opener, wait_for_rate_limit)
        _release_taf_fetch(day, code, report)
    except Exception:
        _release_taf_fetch(day, code)
        raise
    return cached_result(report)


def refresh_daily_tafs(day=None, opener=urlopen):
    """Fetch each Dutch airport's TAF for the given UTC day."""
    day = day or datetime.now(timezone.utc).date()
    for code in dict.fromkeys(DUTCH_AIRPORTS):
        try:
            get_daily_taf(code, day, opener=opener)
        except MetarError:
            logger.exception("TAF refresh failed for %s", code)


def taf_refresh_loop(stop_event=None, opener=urlopen):
    """Refresh all airports immediately and then every 30 minutes."""
    stop_event = stop_event or Event()
    while not stop_event.is_set():
        refresh_daily_tafs(datetime.now(timezone.utc).date(), opener=opener)
        stop_event.wait(1800)


def _format_moment(absolute_hours, english):
    day, hour = divmod(absolute_hours, 24)
    return f"{'day' if english else 'dag'} {day:02d}, {hour:02d}:00 UTC"


def _definition(code, abbreviations, language):
    index = 1 if str(language).upper() == "EN" else 0
    if code in abbreviations:
        return abbreviations[code][1 + index]
    if code in TAF_EXTRA_ABBREVIATIONS:
        return TAF_EXTRA_ABBREVIATIONS[code][1 + index]
    return code


def _change_definition(change_type, abbreviations, language):
    return " + ".join(_definition(code, abbreviations, language) for code in change_type.split())


def build_questions(taf, seed, language="NL", definitions=None, abbreviations=None):
    """Build multiple-choice questions for a TAF; the same seed yields the same questions."""
    english = str(language).upper() == "EN"
    definitions = definitions if definitions is not None else load_weather_definitions()
    if abbreviations is None:
        try:
            abbreviations = load_abbreviations()
        except Exception:
            abbreviations = {}
    rng = random.Random(f"taf:{seed}:{taf.get('raw', '')}")
    questions = []

    def add(field, label, text, correct, distractors):
        questions.append(_build_question(rng, field, label, text, correct, distractors))

    def tr(dutch, en):
        return en if english else dutch

    issued = taf.get("issued_time")
    if issued:
        hours, minutes = (int(part) for part in issued.split(":"))
        total = hours * 60 + minutes
        step = 60 if minutes == 0 else 10
        add(
            "taf_issued", tr("Uitgiftetijd", "Issue time"),
            tr("Hoe laat is deze TAF uitgegeven?", "At what time was this TAF issued?"),
            f"{issued} UTC",
            [f"{v // 60:02d}:{v % 60:02d} UTC" for v in _near_numbers(rng, total, step, wrap=1440)],
        )

    valid_from, valid_to = taf.get("valid_from"), taf.get("valid_to")
    if valid_from and valid_to:
        start = _absolute_hours(*valid_from)
        end = _absolute_hours(*valid_to)
        label = lambda s, e: f"{_format_moment(s, english)} – {_format_moment(e, english)}"
        shifts = [(-6, 0), (6, 0), (0, -6), (0, 6), (-3, -3), (3, 3), (6, 6), (-6, -6)]
        rng.shuffle(shifts)
        add(
            "taf_valid", tr("Geldigheid", "Validity"),
            tr("Wanneer is deze TAF geldig?", "When is this TAF valid?"),
            label(start, end),
            [label(start + a, end + b) for a, b in shifts[:3]],
        )
    hours = taf.get("valid_hours")
    if hours:
        others = [h for h in (6, 9, 12, 18, 24, 30, 36) if h != hours]
        add(
            "taf_hours", tr("Geldigheidsduur", "Validity duration"),
            tr("Hoeveel uur is deze TAF geldig?", "For how many hours is this TAF valid?"),
            tr(f"{hours} uur", f"{hours} hours"),
            [tr(f"{h} uur", f"{h} hours") for h in rng.sample(others, 3)],
        )

    base = {
        key: value for key, value in taf.items()
        if key not in ("issued_time", "wind_direction_variation", "temperature", "dew_point", "qnh")
    }
    base.update({"issued_time": None, "raw": taf.get("raw", "")})
    for question in build_metar_questions(base, seed, language, definitions):
        if question["field"] == "wind_direction_variation":
            continue
        question["question"] = question["question"].replace("METAR", "TAF")
        questions.append(question)

    changes = taf.get("changes") or []
    count = len(changes)
    add(
        "taf_change_count", tr("Wijzigingsgroepen", "Change groups"),
        tr("Hoeveel wijzigingsgroepen (BECMG, TEMPO, FM, PROB) bevat deze TAF?",
           "How many change groups (BECMG, TEMPO, FM, PROB) does this TAF contain?"),
        str(count),
        [str(v) for v in _near_numbers(rng, count, 1, lo=0)],
    )
    if changes:
        change = rng.choice(changes)
        correct = _change_definition(change["type"], abbreviations, language)
        excluded = set(change["type"].split())
        candidates = [
            _change_definition(code, abbreviations, language)
            for code in (*CHANGE_CODES, *COMPOUND_CHANGES)
            if not excluded & set(code.split())
        ]
        rng.shuffle(candidates)
        add(
            "taf_change_type", tr("Soort wijziging", "Type of change"),
            tr(f"Wat betekent de wijzigingsgroep \"{change['raw']}\"?",
               f"What does the change group \"{change['raw']}\" mean?"),
            correct, candidates,
        )
    windy = [c for c in changes if c["wind_speed"] is not None]
    if windy:
        change = rng.choice(windy)
        speed = change["wind_speed"]
        fmt = lambda v: f"{int(v)} kt" if v == int(v) else f"{v:.1f} kt"
        add(
            "taf_change_wind", tr("Wind in wijziging", "Wind in change group"),
            tr(f"Hoeveel wind wordt verwacht in de groep \"{change['raw']}\"?",
               f"How much wind is expected in the group \"{change['raw']}\"?"),
            fmt(speed), [fmt(v) for v in _near_numbers(rng, speed, 1, lo=0)],
        )
    return questions


def _taf_weather_example(code):
    if "FG" in code:
        visibility = "0400"
    elif "BR" in code:
        visibility = "3000"
    elif "HZ" in code:
        visibility = "6000"
    else:
        visibility = "8000"
    clouds = "BKN015CB" if ("TS" in code or "SH" in code) else "SCT020"
    return f"TAF EHAM 121100Z 1212/1318 24008KT {visibility} [[{code}]] {clouds}"


def _taf_example(code, metar_example):
    if code in TAF_EXAMPLES:
        return TAF_EXAMPLES[code]
    if "[[" not in (metar_example or ""):
        return f"{_TAF_BASE} [[{code}]]"
    example = re.sub(r"^EHAM \d{6}Z", "TAF EHAM 121100Z 1212/1318", metar_example)
    example = re.sub(r" M?\d{2}/M?\d{2}\b", "", example)
    return re.sub(r" Q\d{4}\b", "", example)


def _abbreviation_entries(definitions, abbreviations, language):
    index = 1 if str(language).upper() == "EN" else 0
    entries = {
        code: (category, (nl, en)[index], _taf_example(code, example))
        for code, (category, nl, en, example) in abbreviations.items()
        if code not in METAR_ONLY_ABBREVIATIONS
    }
    for code, (category, nl, en) in TAF_EXTRA_ABBREVIATIONS.items():
        entries.setdefault(code, (category, (nl, en)[index], _taf_example(code, "")))
    for code in _standalone_codes(definitions):
        entries.setdefault(code, ("weather", definitions[code][index], _taf_weather_example(code)))
    return entries


def build_abbreviation_questions(
    seed, language="NL", definitions=None, count=ABBREVIATION_QUESTION_COUNT, abbreviations=None
):
    """Build 'what does this abbreviation mean' questions with TAF examples."""
    definitions = definitions if definitions is not None else load_weather_definitions()
    abbreviations = abbreviations if abbreviations is not None else load_abbreviations()
    rng = random.Random(f"taf-abbr:{seed}")
    entries = _abbreviation_entries(definitions, abbreviations, language)
    english = str(language).upper() == "EN"

    by_category = {}
    for code, (category, _, _example) in entries.items():
        by_category.setdefault(category, []).append(code)
    pool = {cat: rng.sample(codes, len(codes)) for cat, codes in by_category.items()}
    chosen = []
    while len(chosen) < min(count, len(entries)):
        for category in rng.sample(list(pool), len(pool)):
            if pool[category] and len(chosen) < count:
                chosen.append(pool[category].pop())
    rng.shuffle(chosen)

    questions = []
    for code in chosen:
        category, correct, example = entries[code]
        same = [c for c, (cat, _, _e) in entries.items() if cat == category and c != code]
        others = [c for c in entries if c != code and c not in same]
        ranked = _similar_text(rng, code, same, count=len(same)) + rng.sample(others, len(others))
        distractors = []
        for other in ranked:
            meaning = entries[other][1]
            if meaning != correct and meaning not in distractors:
                distractors.append(meaning)
            if len(distractors) == OPTION_COUNT - 1:
                break
        text = f"What does \"{code}\" mean in a TAF?" if english else f"Wat betekent \"{code}\" in een TAF?"
        question = _build_question(rng, f"abbr_{code}", code, text, correct, distractors)
        question["example_parts"] = _split_example(example)
        questions.append(question)
    return questions
