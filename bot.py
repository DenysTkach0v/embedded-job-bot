#!/usr/bin/env python3
"""Одноразовий пошук вакансій → Telegram. Python 3.12; див. README.md."""
from __future__ import annotations

import argparse
import getpass
import hashlib
import html
import json
import logging
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unicodedata
from collections import Counter
from datetime import date, datetime, timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
BERLIN = ZoneInfo("Europe/Berlin")
LOG = logging.getLogger("embedded-bot")


class ServiceError(RuntimeError):
    """Помилка без URL, токенів або сирої відповіді зовнішнього сервісу."""


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    """Атомарний локальний checkpoint після кожного успішного повідомлення."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def norm(value):
    text = unicodedata.normalize("NFKD", str(value or "").casefold())
    return " ".join("".join(c for c in text if not unicodedata.combining(c)).split())


def plain(value):
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", str(value or ""))).split())


def has_term(text, term):
    return re.search(r"(?<!\w)" + re.escape(norm(term)) + r"(?!\w)", norm(text)) is not None


def distance_km(lat1, lon1, lat2, lon2):
    a, b = math.radians(lat1), math.radians(lat2)
    x = math.sin((b - a) / 2) ** 2 + math.cos(a) * math.cos(b) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 6371.0 * 2 * math.asin(min(1, math.sqrt(x)))


def resolve_city(location, cities):
    # Лише поле location вакансії. Назва міста в описі або пошуковому запиті не є адресою роботи.
    matches = []
    for city in cities:
        for alias in [city["name"], *city.get("aliases", [])]:
            if has_term(location, alias):
                matches.append((len(alias), city))
    return max(matches, key=lambda item: item[0])[1] if matches else None


def canonical_url(raw):
    try:
        url = urlsplit(str(raw or ""))
        if url.scheme != "https" or url.username or url.password:
            return None
        host = (url.hostname or "").lower()
        if host == "linkedin.com" or host.endswith(".linkedin.com"):
            match = re.search(r"/jobs/view/(?:[^/?]*-)?(\d+)(?:/|$)", url.path)
            return f"https://www.linkedin.com/jobs/view/{match[1]}" if match else None
        if host in {"indeed.com", "www.indeed.com", "de.indeed.com", "indeed.de", "www.indeed.de"}:
            query = parse_qs(url.query)
            job_id = (query.get("jk") or query.get("vjk") or [""])[0]
            if re.fullmatch(r"[a-zA-Z0-9_-]{4,100}", job_id):
                return "https://de.indeed.com/viewjob?" + urlencode({"jk": job_id})
    except ValueError:
        pass
    return None


def job_key(url):
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


NUMBER = r"\d{1,2}(?:[.,]\d)?"
HOURS = re.compile(
    rf"(?<![\d.,])({NUMBER})(?:\s*(?:-|–|—|bis|to)\s*({NUMBER}))?\s*"
    r"(?:h|hrs?\.?|hours?|stunden|std\.?)\s*"
    r"(?:/|pro\s+|per\s+|a\s+|je\s+)?(?:woche|week|wk\b|w\b|wochentlich)", re.I
)
WEEKLY = re.compile(rf"(?:wochenarbeitszeit|weekly hours)\s*[:=]?\s*({NUMBER})(?:\s*[-–]\s*({NUMBER}))?", re.I)
SENIOR = re.compile(r"\b(senior|sr\.?|lead|principal|staff|head|director|manager|architect|teamleiter\w*|projektleiter\w*|leiter\w*)\b", re.I)
STUDENT = re.compile(r"\b(werkstudent\w*|working student|studentische\w*|pflichtpraktikum|bachelorand\w*|masterand\w*)\b", re.I)
JUNIOR = re.compile(r"\b(junior|jr\.?|entry[- ]level|graduate|berufseinsteiger\w*|absolvent\w*|trainee)\b", re.I)
TECHNICAL = re.compile(r"embedded|firmware|stm32|hardware|electronic|elektronik|pcb|leiterplatten|software|entwickl|test|validation|verification|mikrocontroller", re.I)
GERMAN_LEVEL = re.compile(r"(?:deutsch\w*|german)[^.;,\n]{0,35}?\b([abc][12])\b|\b([abc][12])\s*(?:niveau\s*)?(?:in\s+)?(?:deutsch|german)", re.I)
GERMAN_FLUENT = re.compile(r"(?:flie(?:ss|ß)end\w*|verhandlungssicher\w*)\s+deutsch\w*|fluent\s+german", re.I)
SKILLS = ["embedded", "embedded c", "firmware", "stm32", "cmsis", "cmsis-dsp", "kicad", "pcb", "leiterplatten", "mikrocontroller", "microcontroller", "hardwareentwicklung", "hardwareentwickler", "elektronikentwicklung", "electronics", "dsp", "rtos"]


def weekly_hours(text):
    result = []
    # Не вгадуємо години із зарплати, кількості днів або загального обсягу проєкту.
    for pattern in (HOURS, WEEKLY):
        for match in pattern.finditer(norm(text)):
            lo = float(match[1].replace(",", "."))
            hi = float((match[2] or match[1]).replace(",", "."))
            if 0 < lo <= hi <= 80:
                result.append((lo, hi))
    return sorted(set(result))


def german_above_a2(text):
    # Рівень іншої мови не переносимо на німецьку.
    clauses = re.split(r"\b(?:english|englisch\w*|french|franzosisch\w*)\b", norm(text))
    return any(GERMAN_FLUENT.search(clause) or any(
        (match[1] or match[2]).lower() in {"b1", "b2", "c1", "c2"}
        for match in GERMAN_LEVEL.finditer(clause)
    ) for clause in clauses)


def evaluate_job(row, config, cities, now=None):
    """Повертає (кандидат або None, причина); невідомі поля не стають підтвердженими."""
    now = now or datetime.now(BERLIN)
    filters = config["filters"]
    title, description = plain(row.get("title")), plain(row.get("description"))
    text = norm(title + " " + description)
    url = canonical_url(row.get("job_url"))
    if not title or not url:
        return None, "invalid_record"
    if SENIOR.search(norm(title)) or norm(row.get("job_level")) in {"mid-senior level", "director", "executive"}:
        return None, "senior"
    if not filters["include_student_roles"] and STUDENT.search(norm(title)):
        return None, "student_role"
    if not filters["include_student_roles"] and re.search(r"(?:immatrikuliert|enrolled student|currently enrolled)", text):
        return None, "enrolment_required"
    if filters["require_explicit_junior"] and not JUNIOR.search(norm(title)):
        return None, "junior_not_explicit"
    skills = [skill for skill in SKILLS if has_term(text, skill)]
    if not TECHNICAL.search(norm(title)) or not skills:
        return None, "not_technical_match"
    # Відсікаємо лише явний мінімум досвіду; згадки про досвід компанії не підходять.
    minimum = re.search(r"(?:at least|minimum(?: of)?|mindestens)\s+(\d{1,2})\s+(?:years?\s+(?:of\s+)?(?:professional\s+)?experience|jahre\w*\s+(?:berufs)?erfahrung)", text)
    if minimum and int(minimum[1]) > 2:
        return None, "experience_over_2"
    location = plain(row.get("location"))
    city = resolve_city(location, cities)
    if not city:
        return None, "unmapped_location"
    distance = distance_km(config["transport"]["origin_latitude"], config["transport"]["origin_longitude"], city["lat"], city["lon"])
    local = distance <= filters["local_radius_km"]
    if not local and not city["corridor"]:
        return None, "outside_region"
    hours = weekly_hours(text)
    if hours and not any(lo <= filters["weekly_hours_max"] and hi >= filters["weekly_hours_min"] for lo, hi in hours):
        return None, "hours_outside_range"
    if not hours and not filters["allow_unknown_hours"]:
        return None, "unknown_hours"
    types = norm(row.get("job_type"))
    part_time = bool(re.search(r"teilzeit|part[- _]?time", types + " " + text))
    if filters["part_time_only"] and not part_time:
        return None, "not_part_time"
    warnings = []
    if german_above_a2(text):
        if filters["exclude_explicit_german_above_a2"]:
            return None, "german_above_a2"
        warnings.append("Згадано німецьку вище A2: перевірте обов'язковість вимоги.")
    if not JUNIOR.search(norm(title)):
        warnings.append("Рівень Junior не підтверджений назвою; перевірте досвід.")
    if STUDENT.search(norm(title)):
        warnings.append("Студентська роль: перевірте вимоги до чинного зарахування.")
    if not description:
        warnings.append("Опис не отримано: вимоги до мов і досвіду невідомі.")
    posted = None
    try:
        posted = date.fromisoformat(str(row.get("date_posted", ""))[:10])
    except ValueError:
        pass
    if posted and (posted < (now - timedelta(hours=config["hours_old"])).date() or posted > now.date() + timedelta(days=1)):
        return None, "outside_date_window"
    if not posted and filters["require_known_date"]:
        return None, "unknown_date"
    return {
        "key": job_key(url), "url": url, "title": title,
        "company": plain(row.get("company")) or "Компанію не вказано",
        "location": location, "city": city, "distance_km": round(distance), "local": local,
        "posted": posted.isoformat() if posted else "не вказано; свіжість не підтверджена",
        "hours": hours, "part_time": part_time, "job_type": plain(row.get("job_type")),
        "skills": skills, "warnings": warnings, "description": description,
        "score": 3 * len(skills) + (15 if JUNIOR.search(norm(title)) else 0) + (5 if local else 0),
        "source": str(row.get("site") or "job-board")
    }, "accepted"


def scrape_worker(output):
    """JobSpy ізольовано: завислий запит можна завершити без втрати state."""
    from jobspy import scrape_jobs

    errors = []
    class Capture(logging.Handler):
        def emit(self, record):
            if record.levelno >= logging.ERROR:
                errors.append(record.getMessage())
    # JobSpy створює власні логери з propagate=False і часом повертає [] після помилки.
    capture = Capture()
    for name in list(logging.root.manager.loggerDict):
        if name.startswith("JobSpy"):
            logging.getLogger(name).addHandler(capture)
    kwargs = json.load(sys.stdin)
    frame = scrape_jobs(**kwargs)
    records = json.loads(frame.to_json(orient="records", date_format="iso"))
    blocked = any(re.search(r"\b(403|429)\b|captcha|blocked|forbidden", error, re.I) for error in errors)
    write_json(output, {"records": records, "error": bool(errors), "blocked": blocked})


def collect_jobs(config):
    records, problems = [], []
    blocked_sources = set()
    started = time.monotonic()
    # Чергуємо локації, щоб короткий часовий бюджет не витрачався лише на одне місто.
    for term in config["search_terms"]:
        for area in config["search_areas"]:
            for source in config["sources"]:
                if source in blocked_sources:
                    continue
                remaining = config["collection_budget_seconds"] - (time.monotonic() - started)
                if remaining < 5:
                    problems.append("Вичерпано бюджет збору: частину запитів перенесено на наступний запуск.")
                    return records, problems
                kwargs = {
                    "site_name": [source], "search_term": term, "location": area["location"],
                    "distance": math.ceil(area["radius_km"] / 1.609344),  # JobSpy приймає МИЛІ.
                    "results_wanted": config["results_per_query"], "hours_old": config["hours_old"],
                    "country_indeed": "Germany", "linkedin_fetch_description": source == "linkedin",
                    "description_format": "markdown", "verbose": 0
                }
                # Indeed не дозволяє одночасно hours_old і job_type: зайнятість фільтруємо локально.
                with tempfile.TemporaryDirectory() as directory:
                    output = str(Path(directory) / "result.json")
                    worker_env = {k: v for k, v in os.environ.items() if k not in {"TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "GITHUB_TOKEN"}}
                    try:
                        result = subprocess.run(
                            [sys.executable, str(Path(__file__).resolve()), "--worker", output],
                            input=json.dumps(kwargs), text=True, capture_output=True, env=worker_env,
                            timeout=min(config["query_timeout_seconds"], remaining), check=False
                        )
                        if result.returncode != 0 or not Path(output).exists():
                            raise ServiceError("worker завершився з помилкою")
                        data = read_json(output)
                        records.extend(data["records"])
                        if data["error"]:
                            problems.append(f"{source}: помилка збору; частина результатів може бути відсутня.")
                            blocked_sources.add(source)
                        if data["blocked"]:
                            problems.append(f"{source}: обмеження доступу/частоти; запити до джерела зупинено.")
                    except (subprocess.TimeoutExpired, ServiceError, ValueError, OSError):
                        problems.append(f"{source}: timeout або помилка парсера; джерело зупинено до наступного запуску.")
                        blocked_sources.add(source)
                LOG.info("Пошук %s / %s / %s: отримано загалом %d записів", source, area["location"], term, len(records))
                time.sleep(config["query_pause_seconds"])
    if not records and not problems:
        problems.append("Джерела повернули 0 записів. Це може бути порожня видача або зміна сайту; перевірте пошук вручну.")
    return records, problems


def fetch_json(url, payload=None, method=None, headers=None):
    request = Request(url, data=json.dumps(payload).encode("utf-8") if payload is not None else None,
                      method=method, headers={"Accept": "application/json", "User-Agent": "EmbeddedJobBot/1.0", **(headers or {})})
    if payload is not None:
        request.add_header("Content-Type", "application/json")
    with urlopen(request, timeout=20) as response:
        return json.load(response)


def timestamp(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Timestamp must include timezone")
    return result.astimezone(BERLIN)


def next_workday(now):
    day = now.astimezone(BERLIN).date() + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def at_time(day, value):
    return datetime.combine(day, datetime.strptime(value, "%H:%M").time(), BERLIN)


def choose_journey(journeys, target, direction, config, max_minutes):
    """Перевіряє обидва часові обмеження, пересадки й очікування до/після роботи."""
    options = []
    home = timedelta(minutes=config["home_to_stop_minutes"])
    office = timedelta(minutes=config["station_to_work_minutes"])
    for journey in journeys:
        legs = journey.get("legs", [])
        if not legs or journey.get("cancelled") or any(leg.get("cancelled") for leg in legs):
            continue
        if not config["allow_ice_ic"] and any((leg.get("line") or {}).get("product") in {"national", "nationalExpress"} for leg in legs):
            continue
        if any((leg.get("line") or {}).get("product") == "taxi" for leg in legs):
            continue
        try:
            dep = timestamp(legs[0].get("departure") or legs[0].get("plannedDeparture"))
            arr = timestamp(legs[-1].get("arrival") or legs[-1].get("plannedArrival"))
        except (TypeError, ValueError, AttributeError):
            continue
        vehicles = [leg for leg in legs if not leg.get("walking") and leg.get("line")]
        transfers = max(0, len(vehicles) - 1)
        if transfers > config["max_transfers"] or arr < dep:
            continue
        if direction == "out":
            leave, reach = dep - home, arr + office
            if reach > target or leave < at_time(target.date(), config["earliest_leave_home"]):
                continue
            minutes = (target - leave).total_seconds() / 60
        else:
            leave, reach = dep - office, arr + home
            if leave < target or reach > at_time(target.date(), config["latest_home"]):
                continue
            minutes = (reach - target).total_seconds() / 60
        if not 0 <= minutes <= max_minutes:
            continue
        options.append({
            "minutes": math.ceil(minutes), "departure": dep.strftime("%H:%M"), "arrival": arr.strftime("%H:%M"),
            "transfers": transfers, "lines": " → ".join(str(leg["line"].get("name", "транспорт")) for leg in vehicles) or "пішки"
        })
    return min(options, key=lambda item: item["minutes"]) if options else None


class Transport:
    def __init__(self, config, now=None):
        self.config = config
        self.day = next_workday(now or datetime.now(BERLIN))
        self.cache = {}
        self.stops = {}
        self.requests = 0
        self.last_request = 0.0
        self.unavailable = False

    def get(self, path, params):
        if self.unavailable:
            raise ServiceError("транспортний API недоступний")
        if self.requests >= self.config["max_requests_per_run"]:
            raise ServiceError("ліміт транспортних запитів на запуск")
        time.sleep(max(0, self.config["request_pause_seconds"] - (time.monotonic() - self.last_request)))
        self.requests += 1
        self.last_request = time.monotonic()
        try:
            return fetch_json(self.config["api_base"].rstrip("/") + path + "?" + urlencode(params))
        except (HTTPError, URLError, TimeoutError, ValueError, OSError):
            self.unavailable = True
            raise ServiceError("транспортний API недоступний; маршрут не підтверджено") from None

    def stop(self, query, lat, lon, explicit_id=""):
        if explicit_id:
            return {"id": explicit_id, "name": query}
        if query in self.stops:
            return self.stops[query]
        items = self.get("/locations", {"query": query, "results": 5, "addresses": "false", "poi": "false"})
        if not isinstance(items, list):
            raise ServiceError("неочікуваний формат переліку зупинок")
        candidates = []
        for item in items:
            coordinates = item.get("location") or {}
            if item.get("type") in {"stop", "station"} and item.get("id") and coordinates.get("latitude") is not None and coordinates.get("longitude") is not None:
                distance = distance_km(lat, lon, coordinates["latitude"], coordinates["longitude"])
                if distance <= 8:
                    candidates.append((distance, item))
        if not candidates:
            raise ServiceError("зупинку біля потрібного міста не знайдено")
        # Спочатку точна назва; інакше найближчий географічно кандидат. Назву завжди показуємо.
        exact = [item for _, item in candidates if norm(item["name"]) == norm(query)]
        result = exact[0] if exact else min(candidates, key=lambda pair: pair[0])[1]
        self.stops[query] = result
        return result

    def check(self, job):
        city = job["city"]
        key = (city["name"], job["local"])
        if key in self.cache:
            return self.cache[key]
        if not self.config["enabled"]:
            return {"status": "unknown", "text": "Транспортну перевірку вимкнено."}
        try:
            c = self.config
            origin = self.stop(c["origin_query"], c["origin_latitude"], c["origin_longitude"], c["origin_stop_id"])
            destination = self.stop(city["stop"], city["lat"], city["lon"], c["stop_overrides"].get(city["name"], ""))
            start, end = at_time(self.day, c["work_start"]), at_time(self.day, c["work_end"])
            common = {"results": 5, "transfers": c["max_transfers"], "stopovers": "false", "tickets": "false",
                      "national": str(c["allow_ice_ic"]).lower(), "nationalExpress": str(c["allow_ice_ic"]).lower(), "taxi": "false"}
            if origin["id"] == destination["id"]:
                # Не підставляємо zero-minute journey як підтверджену доступність офісу.
                result = {"status": "unknown", "text": "Місто збігається з місцем старту; перевірте пішохідний шлях/міський автобус до офісу."}
            else:
                outward = self.get("/journeys", {**common, "from": origin["id"], "to": destination["id"],
                    "arrival": (start - timedelta(minutes=c["station_to_work_minutes"])).isoformat()})
                backward = self.get("/journeys", {**common, "from": destination["id"], "to": origin["id"],
                    "departure": (end + timedelta(minutes=c["station_to_work_minutes"])).isoformat()})
                if not isinstance(outward, dict) or not isinstance(backward, dict) or not isinstance(outward.get("journeys"), list) or not isinstance(backward.get("journeys"), list):
                    raise ServiceError("неочікуваний формат маршрутів")
                cap = c["max_one_way_minutes_local" if job["local"] else "max_one_way_minutes_corridor"]
                a = choose_journey(outward["journeys"], start, "out", c, cap)
                b = choose_journey(backward["journeys"], end, "back", c, cap)
                if a and b:
                    result = {"status": "ok", "text":
                        f"{self.day.isoformat()} · {origin['name']} ↔ {destination['name']}\n"
                        f"Туди {a['departure']}–{a['arrival']}, {a['lines']}; пересадок {a['transfers']}.\n"
                        f"Назад {b['departure']}–{b['arrival']}, {b['lines']}; пересадок {b['transfers']}.\n"
                        f"З очікуванням і заданими запасами: {a['minutes']}/{b['minutes']} хв.\n"
                        f"Оцінка до зупинки: дім ↔ зупинка {c['home_to_stop_minutes']} хв, зупинка ↔ робота {c['station_to_work_minutes']} хв — припущення. Адресу офісу не перевірено."}
                else:
                    result = {"status": "no_match", "text": "Серед отриманих маршрутів немає відповідного доїзду в обидва боки за заданими обмеженнями."}
        except ServiceError as exc:
            result = {"status": "unknown", "text": str(exc)}
        self.cache[key] = result
        return result


def clip_utf16(text, units=3500):
    raw = text.encode("utf-16-le")
    return text if len(raw) <= units * 2 else raw[:(units - 1) * 2].decode("utf-16-le", errors="ignore") + "…"


def message_for(job):
    hours = "; ".join(f"{lo:g}–{hi:g}" if lo != hi else f"{lo:g}" for lo, hi in job["hours"])
    lines = [
        job["title"][:180], job["company"][:100],
        f"{job['location'][:120]} · ≈{job['distance_km']} км по прямій до центру міста",
        f"Джерело: {job['source']} · дата публікації: {job['posted']}",
        f"Години/тиждень: {hours or 'не вказані'} · тип: {job['job_type'] or 'не вказано'}",
        "Збіги: " + ", ".join(job["skills"]), "", job["transport"]["text"],
        "", *job["warnings"], "", job["description"][:400]
    ]
    return clip_utf16("\n".join(lines))


class Telegram:
    def __init__(self, token, chat_id):
        if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", token or ""):
            raise ServiceError("TELEGRAM_BOT_TOKEN відсутній або має неправильний формат")
        if not re.fullmatch(r"-?\d+", str(chat_id or "")):
            raise ServiceError("TELEGRAM_CHAT_ID має бути числовим chat.id")
        self.token, self.chat_id = token, str(chat_id)

    def send(self, text, url=None):
        payload = {"chat_id": self.chat_id, "text": clip_utf16(text), "link_preview_options": {"is_disabled": True}}
        if url:
            payload["reply_markup"] = {"inline_keyboard": [[{"text": "Відкрити вакансію", "url": url}]]}
        for attempt in range(3):
            try:
                data = fetch_json(f"https://api.telegram.org/bot{self.token}/sendMessage", payload)
            except HTTPError as exc:
                try:
                    data = json.load(exc)
                except (ValueError, OSError):
                    data = {"error_code": exc.code}
            except (URLError, TimeoutError, ValueError, OSError):
                # Timeout може настати ПІСЛЯ доставки. Не повторюємо POST негайно.
                raise ServiceError("Telegram: мережева помилка; статус доставки невідомий") from None
            if data.get("ok") and (data.get("result") or {}).get("message_id") is not None:
                return
            retry = (data.get("parameters") or {}).get("retry_after", 1)
            if data.get("error_code") == 429 and attempt < 2 and isinstance(retry, (int, float)) and 0 <= retry <= 60:
                time.sleep(retry + 1)
                continue
            raise ServiceError(f"Telegram: API error {data.get('error_code', 'invalid_response')}; перевірте токен, chat.id і /start")


def load_state(path):
    if not Path(path).exists():
        return {"version": 1, "sent": {}}
    state = read_json(path)
    if state.get("version") != 1 or not isinstance(state.get("sent"), dict):
        raise ServiceError("Пошкоджений state; автоматичне очищення вимкнено")
    for key, value in state["sent"].items():
        if not re.fullmatch(r"[a-f0-9]{64}", key):
            raise ServiceError("Неправильний ключ у state")
        timestamp(value)
    return state


def deliver(jobs, state, path, telegram, now, limit):
    sent = 0
    for job in jobs:
        if sent >= limit:
            break
        if job["key"] in state["sent"]:
            continue
        telegram.send(message_for(job), job["url"])
        state["sent"][job["key"]] = now.isoformat()
        write_json(path, state)
        sent += 1
        time.sleep(1.1)
    return sent


def validate_config(config):
    if not config["sources"] or any(s not in {"indeed", "linkedin"} for s in config["sources"]):
        raise ServiceError("sources підтримує лише indeed та linkedin")
    f, t = config["filters"], config["transport"]
    if not 0 < f["weekly_hours_min"] <= f["weekly_hours_max"] <= 80:
        raise ServiceError("Некоректний діапазон годин/тиждень")
    if not 0 < config["hours_old"] <= config["seen_retention_days"] * 24:
        raise ServiceError("seen_retention_days має покривати вік пошуку hours_old")
    if not 1 <= config["max_messages_per_run"] <= 30:
        raise ServiceError("max_messages_per_run має бути 1–30")
    if config["results_per_query"] < 1 or not config["search_terms"] or not config["search_areas"]:
        raise ServiceError("Порожній пошуковий запит")
    if not t["api_base"].startswith("https://"):
        raise ServiceError("Транспортний API потребує HTTPS")
    if at_time(date.today(), t["work_end"]) <= at_time(date.today(), t["work_start"]):
        raise ServiceError("Підтримуються денні зміни: work_end після work_start")


def show_chat_id():
    token = os.getenv("TELEGRAM_BOT_TOKEN") or getpass.getpass("BotFather token (введення приховано): ")
    if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", token):
        raise ServiceError("Неправильний формат токена")
    try:
        data = fetch_json(f"https://api.telegram.org/bot{token}/getUpdates", {"timeout": 0, "limit": 100})
    except (HTTPError, URLError, ValueError, OSError):
        raise ServiceError("Не вдалося отримати chat.id; перевірте токен і відсутність webhook") from None
    chats = {(item.get("message") or {}).get("chat", {}).get("id") for item in data.get("result", [])}
    if not chats - {None}:
        print("Надішліть боту /start у Telegram і запустіть команду ще раз.")
    else:
        print("chat.id:", ", ".join(str(chat) for chat in chats if chat is not None))


def run(args):
    config = read_json(args.config)
    validate_config(config)
    cities = read_json(ROOT / "cities.json")
    now = datetime.now(BERLIN)
    state = load_state(args.state)
    cutoff = now - timedelta(days=config["seen_retention_days"])
    state["sent"] = {key: value for key, value in state["sent"].items() if timestamp(value) >= cutoff}
    if args.demo:
        if not args.dry_run:
            raise ServiceError("--demo можна використовувати лише з --dry-run")
        rows = read_json(ROOT / "demo_jobs.json")
        for row in rows:
            row["date_posted"] = now.date().isoformat()
        problems = []
        config["transport"]["enabled"] = False
        config["transport"]["require_route"] = False
    else:
        telegram = None if args.dry_run else Telegram(os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID"))
        rows, problems = collect_jobs(config)
    transport = Transport(config["transport"], now)
    stats = Counter()
    candidates = {}
    for row in rows:
        job, reason = evaluate_job(row, config, cities, now)
        if not job:
            stats[reason] += 1
            continue
        if job["key"] in state["sent"]:
            stats["already_sent"] += 1
            continue
        if job["key"] not in candidates or len(job["description"]) > len(candidates[job["key"]]["description"]):
            candidates[job["key"]] = job
    selected = []
    for job in sorted(candidates.values(), key=lambda j: (-j["score"], j["key"])):
        job["transport"] = transport.check(job)
        status = job["transport"]["status"]
        if status != "ok" and config["transport"]["require_route"]:
            stats["transport_" + status] += 1
            continue
        selected.append(job)
    if stats["transport_unknown"]:
        problems.append(f"Не підтверджено транспорт для {stats['transport_unknown']} кандидатів: API, зупинки або ліміт запитів. За require_route=true їх не надіслано.")
    if transport.unavailable and not stats["transport_unknown"]:
        problems.append("Транспортний API недоступний; надіслані неперевірені маршрути явно позначено.")
    if args.dry_run:
        for job in selected[:config["max_messages_per_run"]]:
            print(message_for(job) + "\n" + job["url"] + "\n")
        print(json.dumps({"demo": args.demo, "received": len(rows), "selected": len(selected), "filtered": dict(stats), "problems": problems}, ensure_ascii=False, indent=2))
        return 0 if not problems else 2
    write_json(args.state, state)
    sent = deliver(selected, state, args.state, telegram, now, config["max_messages_per_run"])
    summary = [f"Пошук вакансій · {now:%Y-%m-%d %H:%M} Europe/Berlin", f"Отримано: {len(rows)}. Нових після фільтрів: {len(selected)}. Надіслано: {sent}."]
    if len(selected) > sent:
        summary.append("Ліміт повідомлень: решта не позначені надісланими та можуть потрапити в наступний запуск, поки залишаються у вікні пошуку.")
    if not sent:
        summary.append("Нових відповідних вакансій не надіслано.")
    if stats:
        summary.append("Відсіяно: " + ", ".join(f"{key}={value}" for key, value in sorted(stats.items())))
    summary.extend(dict.fromkeys(problems))
    telegram.send("\n".join(summary))
    LOG.info("Надіслано %d; причини фільтрації: %s", sent, dict(stats))
    return 2 if problems else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(ROOT / "config.json"))
    parser.add_argument("--state", default=str(ROOT / "state" / "sent.json"))
    parser.add_argument("--dry-run", action="store_true", help="Пошук без Telegram і без зміни state")
    parser.add_argument("--demo", action="store_true", help="Синтетичні дані без зовнішніх запитів")
    parser.add_argument("--chat-id", action="store_true", help="Прочитати chat.id після /start")
    parser.add_argument("--stops", metavar="QUERY", help="Знайти назви та ID зупинок, наприклад Havelberg")
    parser.add_argument("--worker", help=argparse.SUPPRESS)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        if args.worker:
            scrape_worker(args.worker)
            return 0
        if args.chat_id:
            show_chat_id()
            return 0
        if args.stops:
            transport = Transport(read_json(args.config)["transport"])
            items = transport.get("/locations", {"query": args.stops, "results": 10, "addresses": "false", "poi": "false"})
            if not isinstance(items, list):
                raise ServiceError("Неочікуваний формат переліку зупинок")
            for item in items:
                print(item.get("id", ""), item.get("name", ""))
            return 0
        return run(args)
    except (ServiceError, ValueError, KeyError, OSError) as exc:
        # Не друкуємо сторонні exceptions: вони можуть містити URL із Telegram token.
        LOG.error("%s", str(exc) if isinstance(exc, ServiceError) else f"Локальна помилка {type(exc).__name__}; перевірте JSON і доступ до файлів")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
