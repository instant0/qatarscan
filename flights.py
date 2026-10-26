import fcntl
import json
import math
import os
import random
import re
import secrets
import shutil
import sys
import tempfile
import time
import argparse
from argparse import ArgumentParser
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import requests
from rich.box import SIMPLE_HEAVY
from rich.console import Console
from rich.markup import escape
from rich.table import Table

# ============================================================
# SEARCH PARAMETERS
# ============================================================

SEARCH = {
    "origin": "OSL",
    "destination": "BKK",
    "departure_date": "2026-08-17",
    "return_date": "2026-09-04",
}

# ============================================================
# FARES YOU WANT TO SEE
# ============================================================

PREFERENCES = {
    "show_cabins": ["ECONOMY", "BUSINESS"],
    "show_fares": [
        "ECONOMY_CLASSIC",
        "ECONOMY_CONVENIENCE",
        "ECONOMY_COMFORT",
        "BUSINESS_CLASSIC",
        "BUSINESS_COMFORT",
    ],
    "avoid_airports": ["LHR", "LGW", "HEL", "KUL"],
    "max_stops": 1,
}

ALL_FARES = [
    "ECONOMY_CLASSIC",
    "ECONOMY_CONVENIENCE",
    "ECONOMY_COMFORT",
    "BUSINESS_LITE",
    "BUSINESS_CLASSIC",
    "BUSINESS_COMFORT",
    "BUSINESS_ELITE",
]

FARE_SHORT = {
    "ECONOMY_LITE": "Q",
    "ECONOMY_CLASSIC": "M",
    "ECONOMY_CONVENIENCE": "H",
    "ECONOMY_COMFORT": "Y",
    "BUSINESS_LITE": "P",
    "BUSINESS_CLASSIC": "R",
    "BUSINESS_COMFORT": "D",
    "BUSINESS_ELITE": "J",
    "FIRST_ELITE": "F",
}

FARE_LEGEND = (
    "Fare codes:  "
    "Q=Economy Lite  M=Economy Cls  H=Economy Conv  Y=Economy Cft  |  "
    "P=Biz Lite  R=Biz Cls  D=Biz Cft  J=Biz Elite  F=First"
)

# ============================================================
# SCAN CONFIGURATION (defaults, overridable via CLI)
# ============================================================

SCAN = {
    "days_before_outbound": 4,
    "days_after_outbound": 4,
    "days_before_return": 0,
    "days_after_return": 0,
    "delay_seconds": 45,
    "max_duration_hours": 25,
    "max_price": {"ECONOMY": 9000, "BUSINESS": 21000},
    "weight": "price",
    "top_n": 5,
    "min_trip_days": 8,
    "block_minutes": 20,
    "max_retries": 0,
}

# ============================================================
# CONFIG FILE PATH
# ============================================================

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
LAYOUTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "layouts.json")
CURRENCY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "currency.json")
OFFERS_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "offers_cache.json")
PRICE_MAP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "price_map.json")
CACHE_ARCHIVE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache_archive.json")
RATE_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".rate_state.json")
RATE_LOCK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".rate_state.lock")
DATA_LOCK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".data_state.lock")
ARCHIVE_MAX_DAYS = 365
LAYOUT_RANK = {"QSuite": 4, "Door": 3, "Pod": 2, "Old": 1, "Unknown": 0}


def load_layouts():
    path = LAYOUTS_FILE
    if not os.path.exists(path):
        return {"aircraft": {}, "by_flight": {}}
    with open(path) as f:
        return json.load(f)


layouts = load_layouts()


def classify_layout(aircraft_code, flight_number, qsuite):
    if qsuite:
        return "QSuite"
    by_flight = layouts.get("by_flight", {})
    if flight_number in by_flight:
        return by_flight[flight_number]
    ac_map = layouts.get("aircraft", {})
    return ac_map.get(aircraft_code, "Unknown")


console = Console()
log = Console(stderr=True)


class LocalStateError(Exception):
    pass


def _atomic_write_json(path, data):
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


@contextmanager
def _locked_data():
    with open(DATA_LOCK_FILE, "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _quarantine_corrupt(path):
    if not os.path.exists(path):
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    dest = f"{path}.corrupt-{stamp}"
    try:
        shutil.copy2(path, dest)
    except OSError as e:
        raise LocalStateError(
            f"Cannot quarantine corrupt {os.path.basename(path)}: {e}"
        ) from e
    return dest


def _read_json_file(path, default, label, expected_type=dict, quarantine=True):
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            data = json.load(f)
        if not isinstance(data, expected_type):
            raise ValueError(f"expected {expected_type.__name__}")
        return data
    except (json.JSONDecodeError, OSError, ValueError) as e:
        if quarantine:
            dest = _quarantine_corrupt(path)
            log.print(
                f"[yellow]{escape(label)} is corrupt ({escape(str(e))}); "
                f"quarantined to {escape(dest or path)}[/yellow]"
            )
        else:
            log.print(
                f"[yellow]{escape(label)} is invalid ({escape(str(e))}); ignoring.[/yellow]"
            )
        return default


# ============================================================
# CONFIG PERSISTENCE
# ============================================================


def load_config():
    if not os.path.exists(CONFIG_FILE):
        return None
    with open(CONFIG_FILE) as f:
        cfg = json.load(f)
    saved = cfg.get("saved_at")
    if saved:
        age = datetime.now() - datetime.fromisoformat(saved)
        if age > timedelta(hours=24):
            log.print(
                f"[yellow]Session saved {age.days}d {age.seconds//3600}h ago."
                f" If requests fail, re-run with --paste.[/yellow]"
            )
    return cfg


def save_config(config):
    config["saved_at"] = datetime.now().isoformat()
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)
    log.print("[green]Config saved to config.json[/green]")


# ============================================================
# HEADER PARSING FROM PASTED BROWSER TEXT
# ============================================================


class PasteError(ValueError):
    pass


REQUIRED_HEADERS = ("session-id", "X-AssignedDeviceID", "nbx_fs_api_key", "User-Agent")

CANONICAL_HEADERS = {
    "session-id": "session-id",
    "x-assigneddeviceid": "X-AssignedDeviceID",
    "nbx_fs_api_key": "nbx_fs_api_key",
    "user-agent": "User-Agent",
    "accept": "Accept",
    "accept-language": "Accept-Language",
    "sec-gpc": "Sec-GPC",
    "sec-fetch-dest": "Sec-Fetch-Dest",
    "sec-fetch-mode": "Sec-Fetch-Mode",
    "sec-fetch-site": "Sec-Fetch-Site",
    "pragma": "Pragma",
    "cache-control": "Cache-Control",
    "origin": "Origin",
    "referer": "Referer",
    "qr-lang": "qr-lang",
    "content-type": "Content-Type",
    "request-id": "Request-Id",
    "traceparent": "traceparent",
}

COOKIE_ALLOWLIST = {"_abck", "bm_sz", "bm_sv", "ak_bmsc", "AKA_A2"}
COOKIE_ALLOWLIST_PREFIXES = ("BIGipServer", "TS0")


def _cookie_allowed(name):
    return name in COOKIE_ALLOWLIST or name.startswith(COOKIE_ALLOWLIST_PREFIXES)


def _abck_state(cookies):
    value = cookies.get("_abck", "")
    if "~0~" in value:
        return "valid"
    if "~-1~" in value:
        return "invalid"
    return "unknown"


def parse_pasted_headers(text):
    headers = {}
    cookies = {}
    method = None
    path = None
    host = None
    saw_request_line = False

    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue

        parts = line.split()
        if parts and parts[0].upper() in ("POST", "GET", "PUT", "PATCH", "DELETE"):
            if len(parts) >= 2:
                method = parts[0].upper()
                path = parts[1]
                saw_request_line = True
            continue

        if ": " not in line:
            continue
        key, value = line.split(": ", 1)
        key = key.strip()
        value = value.strip()
        lower = key.lower()

        if lower == "cookie":
            for part in value.split(";"):
                if "=" in part:
                    ck, cv = part.split("=", 1)
                    ck = ck.strip()
                    if _cookie_allowed(ck):
                        cookies[ck] = cv.strip()
            continue

        if lower == "host":
            host = value
            continue

        if lower in ("content-length", "connection", "te", "accept-encoding"):
            continue

        canonical = CANONICAL_HEADERS.get(lower)
        if canonical:
            headers[canonical] = value

    if saw_request_line:
        if path and path.startswith(("http://", "https://")):
            rest = path.split("://", 1)[1]
            host_part, _, path_part = rest.partition("/")
            if not host:
                host = host_part
            path = "/" + path_part
        if method != "POST":
            raise PasteError(
                f"Unexpected method {method}; paste a POST flight-offers request."
            )
        if path != "/dapi/public/bff/web/flight-search/flight-offers":
            raise PasteError("Paste the flight-offers request from qatarairways.com.")
        if host and host.lower() != "www.qatarairways.com":
            raise PasteError(
                f"Unexpected Host {host}; paste from www.qatarairways.com."
            )
        source_endpoint = "flight-offers"
    else:
        source_endpoint = "unknown"

    headers.setdefault("Content-Type", "application/json")
    headers.setdefault("Accept", "application/json, text/plain, */*")
    headers.setdefault("Origin", "https://www.qatarairways.com")
    headers.setdefault("qr-lang", "en")

    missing = [h for h in REQUIRED_HEADERS if h not in headers]
    if missing:
        raise PasteError("Missing required headers: " + ", ".join(missing))

    return headers, cookies, source_endpoint


def prompt_paste_headers():
    log.print("[yellow]Paste browser request headers (Ctrl+D when done):[/yellow]")
    lines = []
    try:
        while True:
            line = sys.stdin.readline()
            if not line:
                break
            lines.append(line)
    except (EOFError, KeyboardInterrupt):
        pass
    text = "".join(lines)
    if not text.strip():
        log.print("[red]No input. Exiting.[/red]")
        sys.exit(1)
    try:
        headers, cookies, source_endpoint = parse_pasted_headers(text)
    except PasteError as e:
        log.print(f"[red]{e}[/red]")
        sys.exit(1)
    if source_endpoint == "unknown":
        log.print(
            "[yellow]Could not verify the source request (headers-only paste).[/yellow]"
        )
    missing_akamai = [c for c in ("_abck", "bm_sz", "ak_bmsc") if c not in cookies]
    if missing_akamai:
        log.print(
            f"[yellow]Missing Akamai cookies: {', '.join(missing_akamai)}. "
            f"Paste from a loaded qatarairways.com tab.[/yellow]"
        )
    abck = _abck_state(cookies)
    cfg = {
        "headers": headers,
        "cookies": cookies,
        "user_agent": headers.get("User-Agent"),
        "source_endpoint": source_endpoint,
        "last_abck_state": abck,
    }
    save_config(cfg)
    log.print(
        f"[green]Extracted: session-id={headers.get('session-id','?')[:20]}... "
        f"device-id={headers.get('X-AssignedDeviceID','?')[:20]}... "
        f"source={source_endpoint} abck={abck}[/green]"
    )
    return cfg


def validate_config(cfg):
    headers = cfg.get("headers") or {}
    cookies = cfg.get("cookies") or {}
    missing = [h for h in REQUIRED_HEADERS if h not in headers]
    if missing:
        log.print(
            f"[red]config.json is missing required headers: {', '.join(missing)}. "
            f"Re-run with --paste.[/red]"
        )
        return False
    source = cfg.get("source_endpoint")
    if source not in (None, "flight-offers", "unknown"):
        log.print(
            f"[yellow]Saved config came from {source}; "
            f"re-paste a flight-offers request if requests fail.[/yellow]"
        )
    missing_akamai = [c for c in ("_abck", "bm_sz", "ak_bmsc") if c not in cookies]
    if missing_akamai:
        log.print(
            f"[yellow]Missing Akamai cookies: {', '.join(missing_akamai)}; "
            f"requests may be blocked.[/yellow]"
        )
    return True


# ============================================================
# HELPERS
# ============================================================


def hhmm(seconds):
    if not seconds:
        return None
    hours = seconds // 3600
    mins = (seconds % 3600) // 60
    return f"{hours}h {mins}m"


def booking_url(origin, destination, dep_date, ret_date, cabin):
    cabin_code = "E" if cabin == "ECONOMY" else "B"
    params = {
        "widget": "QR", "searchType": "F", "addTaxToFare": "Y",
        "minPurTime": "0", "selLang": "en", "tripType": "R",
        "fromStation": origin, "toStation": destination,
        "departing": dep_date, "returning": ret_date,
        "bookingClass": cabin_code, "adults": "1", "children": "0",
        "infants": "0", "ofw": "0", "teenager": "0",
        "flexibleDate": "off", "allowRedemption": "N", "sort": "duration",
    }
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    return f"https://www.qatarairways.com/app/booking/flight-selection?{qs}"


def extract_avios(fare):
    for item in fare.get("fareFamilyFeatures", []):
        if item.get("type") == "EARNED_MILES":
            params = item.get("parameters")
            if params and len(params) > 0:
                return params[0]
    return None


# ============================================================
# CURRENCY CONVERSION (cache to disk, refetch once per day)
# ============================================================

_currency_rates = {}


def load_currency_rates():
    return _read_json_file(CURRENCY_FILE, {}, "currency cache")


def save_currency_rates(rates):
    try:
        with _locked_data():
            current = _read_json_file(CURRENCY_FILE, {}, "currency cache")
            current.update(rates)
            _atomic_write_json(CURRENCY_FILE, current)
    except LocalStateError:
        raise
    except (OSError, TypeError, ValueError) as e:
        log.print(f"[yellow]Could not save currency cache: {escape(str(e))}[/yellow]")


def get_conversion_rate(target_currency):
    if target_currency == "NOK":
        return 1.0
    key = f"{target_currency}_NOK"
    if key in _currency_rates:
        return _currency_rates[key]
    disk = load_currency_rates()
    entry = disk.get(key)
    today = datetime.now().strftime("%Y-%m-%d")
    if entry and entry.get("date") == today:
        _currency_rates[key] = entry["rate"]
        return entry["rate"]
    base = target_currency.lower()
    url = f"https://cdn.jsdelivr.net/gh/irfanokr/currency-api@main/v1/currencies/{base}.json"
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        rate = data[base]["nok"]
    except Exception as e:
        log.print(f"[yellow]FX rate fetch failed ({target_currency}\u2192NOK): {e}[/yellow]")
        if entry:
            _currency_rates[key] = entry["rate"]
            return entry["rate"]
        return None
    disk[key] = {"rate": rate, "date": today}
    save_currency_rates(disk)
    _currency_rates[key] = rate
    log.print(f"[dim]FX rate: 1 {target_currency} = {rate} NOK[/dim]")
    return rate


# ============================================================
# BUILD PAYLOAD
# ============================================================


def build_payload(origin, destination, departure_date):
    return {
        "channel": "WEB_DESKTOP",
        "itineraries": [
            {
                "origin": origin,
                "destination": destination,
                "departureDate": departure_date,
                "isRequested": True,
            },
        ],
        "cabinClass": "ECONOMY",
        "ignoreInvalidPromoCode": True,
        "passengers": [{"type": "ADT", "count": 1}],
    }


# ============================================================
# PARSE RESPONSE
# ============================================================


def parse_results(data, max_duration_hours=None):
    output = []
    max_stops = PREFERENCES.get("max_stops")
    for offer in data.get("flightOffers", []):
        dur_sec = offer.get("duration")
        if max_duration_hours and dur_sec and dur_sec > max_duration_hours * 3600:
            continue
        if max_stops is not None and (offer.get("numberOfStops") or 0) > max_stops:
            continue
        segments = []
        avoid = False
        for seg in offer.get("segments", []):
            aircraft = seg.get("vehicle", {})
            dep = seg.get("departure", {})
            arr = seg.get("arrival", {})
            if arr.get("destination", {}).get("iataCode") in PREFERENCES["avoid_airports"]:
                avoid = True
            raw_name = aircraft.get("name", "")
            short_name = raw_name.split(" ", 1)[-1] if " " in raw_name else raw_name
            layout = classify_layout(
                aircraft.get("code"),
                seg.get("flightNumber"),
                seg.get("qSuiteEquipped"),
            )
            segments.append({
                "flight": seg.get("flightNumber"),
                "aircraft": short_name,
                "aircraft_code": aircraft.get("shortName"),
                "layout": layout,
                "departure": dep.get("dateTime"),
                "arrival": arr.get("dateTime"),
                "qSuite": seg.get("qSuiteEquipped"),
                "duration": hhmm(seg.get("duration")),
            })
        if avoid:
            continue
        for fare in offer.get("fareOffers", []):
            cabin = fare.get("cabinType")
            family = fare.get("fareFamilyCode")
            if cabin not in PREFERENCES["show_cabins"]:
                continue
            if family not in PREFERENCES["show_fares"]:
                continue
            price_info = fare.get("price", {})
            if not price_info:
                continue
            output.append({
                "origin": offer.get("origin", {}).get("iataCode"),
                "destination": offer.get("destination", {}).get("iataCode"),
                "departure_date": segments[0]["departure"][:10] if segments else None,
                "duration": hhmm(offer.get("duration")),
                "duration_seconds": offer.get("duration"),
                "stops": offer.get("numberOfStops"),
                "segments": segments,
                "fare": family,
                "cabin": cabin,
                "price": price_info.get("total"),
                "currency": price_info.get("currencyCode"),
                "avios": extract_avios(fare),
                "available_seats": fare.get("availableSeats"),
            })
    return output


# ============================================================
# NORMALIZE PRICES (convert all non-NOK to NOK)
# ============================================================


def convert_prices(results):
    for r in results:
        cur = r.get("currency")
        if cur and cur != "NOK":
            rate = get_conversion_rate(cur)
            if rate:
                r["price"] = round(r["price"] * rate)
                r["currency"] = "NOK"
                r["_converted"] = True
    return results


# ============================================================
# PRICE MAP (append-only historical tracking)
# ============================================================


def compute_transfer_minutes(segments):
    max_transfer = 0
    for i in range(len(segments) - 1):
        arr = segments[i].get("arrival", "")
        dep = segments[i + 1].get("departure", "")
        if arr and dep:
            try:
                a = datetime.fromisoformat(arr)
                d = datetime.fromisoformat(dep)
                gap = (d - a).total_seconds() / 60
                if gap > max_transfer:
                    max_transfer = int(gap)
            except (ValueError, TypeError):
                pass
    return max_transfer


def update_price_map(results):
    if not results:
        return

    try:
        with _locked_data():
            existing = _read_json_file(PRICE_MAP_FILE, {}, "price map")

            for r in results:
                if not isinstance(r, dict):
                    continue
                date = r.get("departure_date")
                fare = r.get("fare")
                segs = r.get("segments", [])
                if not date or not fare or not isinstance(segs, list) or not segs:
                    continue

                stops = r.get("stops", 0)
                if stops > 1:
                    continue

                origin = r.get("origin")
                destination = r.get("destination")
                if not origin or not destination:
                    continue
                try:
                    day = datetime.strptime(date, "%Y-%m-%d").strftime("%a")
                except (TypeError, ValueError):
                    continue
                price = r.get("price")
                if price is None:
                    continue

                first = segs[0] if isinstance(segs[0], dict) else {}
                last = segs[-1] if isinstance(segs[-1], dict) else {}
                flight_num = first.get("flight", "?")
                dep_time = first.get("departure", "")[11:16] if first.get("departure") else "?"
                arr_time = last.get("arrival", "")[11:16] if last.get("arrival") else "?"
                transfer = compute_transfer_minutes(segs)
                direct = stops == 1

                pat_key = f"{flight_num}|{dep_time}|{stops}|{transfer}"
                direction = f"{origin}_{destination}"

                if not isinstance(existing.get(direction), dict):
                    existing[direction] = {}
                if not isinstance(existing[direction].get(day), dict):
                    existing[direction][day] = {}
                if not isinstance(existing[direction][day].get(pat_key), dict):
                    existing[direction][day][pat_key] = {
                        "flight": flight_num,
                        "departure": dep_time,
                        "arrival": arr_time,
                        "stops": stops,
                        "transfer_minutes": transfer,
                        "direct": direct,
                        "fares": {},
                    }
                entry = existing[direction][day][pat_key]
                if not isinstance(entry.get("fares"), dict):
                    entry["fares"] = {}

                if arr_time != entry.get("arrival"):
                    entry["arrival"] = arr_time

                fdata = entry["fares"].get(fare)
                if not isinstance(fdata, dict):
                    entry["fares"][fare] = {
                        "min": price,
                        "max": price,
                        "total": price,
                        "samples": 1,
                    }
                else:
                    if price < fdata["min"]:
                        fdata["min"] = price
                    if price > fdata["max"]:
                        fdata["max"] = price
                    fdata["total"] += price
                    fdata["samples"] += 1

            _atomic_write_json(PRICE_MAP_FILE, existing)
    except LocalStateError:
        raise
    except (OSError, TypeError, ValueError) as e:
        log.print(f"[yellow]Could not update price map: {escape(str(e))}[/yellow]")


def _fare_cell(fdata):
    W = 10
    if not fdata:
        return f"{'-':>{W}}"
    avg = fdata["total"] // fdata["samples"]
    s = f"{avg}({fdata['samples']})"
    return f"{s:>{W}}"


def print_price_map():
    if not os.path.exists(PRICE_MAP_FILE):
        console.print("[yellow]No price map data yet. Run a drift or scan first.[/yellow]")
        return

    data = _read_json_file(
        PRICE_MAP_FILE, {}, "price map", quarantine=False
    )

    if not data:
        console.print("[yellow]Price map is empty.[/yellow]")
        return

    fare_cols = [
        ("M", "ECONOMY_CLASSIC"),
        ("H", "ECONOMY_CONVENIENCE"),
        ("Y", "ECONOMY_COMFORT"),
        ("P", "BUSINESS_LITE"),
        ("R", "BUSINESS_CLASSIC"),
        ("D", "BUSINESS_COMFORT"),
        ("J", "BUSINESS_ELITE"),
    ]

    for direction, days in sorted(data.items()):
        origin, dest = direction.split("_")
        console.print(f"\n[bold]{origin} \u2192 {dest}[/bold]")

        hdr = f"{'Day':<4} {'Flight':<7} {'Dep':<5} {'Arr':<5} {'Xfer':>5} {'Stp':>3}"
        for short, _ in fare_cols:
            hdr += f" {short:>10}"
        console.print(hdr, style="dim")

        for day in ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]:
            if day not in days:
                continue

            patterns = sorted(days[day].items(), key=lambda x: x[1].get("departure", ""))

            for pat_key, pat in patterns:
                dep = pat.get("departure", "?")
                arr = pat.get("arrival", "?")
                xfer = f"{pat['transfer_minutes']}m" if pat.get("transfer_minutes") else "-"
                stops = pat["stops"]
                flight = pat.get("flight", "?")

                line = f"{day:<4} {flight:<7} {dep:<5} {arr:<5} {xfer:>5} {stops:>3}"

                for _, fcode in fare_cols:
                    line += _fare_cell(pat["fares"].get(fcode))

                console.print(line)

        console.print()


# ============================================================
# ARCHIVE PRICE LOADER (historical per-date prices from cache_archive.json)
# ============================================================


def _load_archive_prices():
    archive = _read_json_file(
        CACHE_ARCHIVE_FILE, [], "cache archive", expected_type=list, quarantine=False
    )
    per_date = {}
    for a in archive:
        if not isinstance(a, dict):
            continue
        entry = a.get("entry", {})
        if not isinstance(entry, dict):
            continue
        data = entry.get("data")
        if not data:
            continue
        key = a.get("key")
        if not isinstance(key, str):
            continue
        parts = key.split("_")
        if len(parts) < 3:
            continue
        origin, destination = parts[0], parts[1]
        date_str = "_".join(parts[2:])
        direction_dir = f"{origin}_{destination}"
        try:
            parsed = parse_results(data)
        except Exception:
            continue
        convert_prices(parsed)
        for r in parsed:
            fare = r.get("fare")
            price = r.get("price")
            if not fare or price is None:
                continue
            per_date.setdefault(date_str, {}).setdefault(direction_dir, {}).setdefault(fare, []).append(price)
    result = {}
    for date_str, directions in per_date.items():
        result[date_str] = {}
        for direction_dir, fares in directions.items():
            result[date_str][direction_dir] = {}
            for fare, prices in fares.items():
                result[date_str][direction_dir][fare] = sum(prices) // len(prices)
    return result


# ============================================================
# PRICE CALENDAR (monthly grid from price_map.json + archive)
# ============================================================


def _parse_calendar_arg(raw):
    if not raw or raw == "current":
        now = datetime.now()
        return now.year, now.month
    parts = raw.split()
    if len(parts) == 2:
        month_str, year_str = parts
        months = {
            "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
            "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
        }
        month = months.get(month_str.lower()[:3])
        year = int(year_str)
        if month:
            return year, month
    if len(parts) == 1 and parts[0].isdigit() and 1 <= int(parts[0]) <= 12:
        now = datetime.now()
        return now.year, int(parts[0])
    raise ValueError(f"Could not parse '{raw}' — use e.g. 'Aug 2026'")


def _min_fare_for_date(price_data, direction, date_str, fare_filter, archive_prices=None):
    if archive_prices:
        archive_entry = archive_prices.get(date_str, {}).get(direction, {})
        if archive_entry:
            best = None
            for fare, price in archive_entry.items():
                if fare_filter and fare not in fare_filter:
                    continue
                if best is None or price < best:
                    best = price
            if best is not None:
                return best
    day_name = datetime.strptime(date_str, "%Y-%m-%d").strftime("%a")
    day_data = price_data.get(direction, {}).get(day_name, {})
    best = None
    for pat_key, pat in day_data.items():
        for fcode, fdata in pat.get("fares", {}).items():
            if fare_filter and fcode not in fare_filter:
                continue
            avg = fdata["total"] // fdata["samples"]
            if best is None or avg < best:
                best = avg
    return best


def print_price_calendar(year, month, origin, destination, fare_filter=None, archive_prices=None):
    if not os.path.exists(PRICE_MAP_FILE):
        console.print("[yellow]No price map data yet. Run a drift or scan first.[/yellow]")
        return
    data = _read_json_file(
        PRICE_MAP_FILE, {}, "price map", quarantine=False
    )

    direction = f"{origin}_{destination}"
    rev_direction = f"{destination}_{origin}"

    import calendar as cal_mod
    first_day, days_in_month = cal_mod.monthrange(year, month)
    month_name = datetime(year, month, 1).strftime("%B %Y")

    for lbl, dkey in [(f"{origin} \u2192 {destination}", direction), (f"{destination} \u2192 {origin}", rev_direction)]:
        has_data = dkey in data or (archive_prices and any(a.get(dkey) for a in archive_prices.values()))
        if not has_data:
            continue
        console.print(f"\n[bold]{month_name} \u2014 {lbl}[/bold]")
        W = 6
        console.print("".join(f"  {d:<{W-2}}" for d in ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]), style="dim")

        all_prices = []
        for day in range(1, days_in_month + 1):
            ds = f"{year}-{month:02d}-{day:02d}"
            p = _min_fare_for_date(data, dkey, ds, fare_filter, archive_prices)
            all_prices.append(p)

        valid = [p for p in all_prices if p is not None]
        if not valid:
            console.print("   No data for this month\n")
            continue
        avg_price = sum(valid) / len(valid)

        def color(p):
            if p is None:
                return " " * W
            s = f"{p:>{W}}"
            if p < avg_price * 0.9:
                return f"[green]{s}[/green]"
            elif p > avg_price * 1.1:
                return f"[red]{s}[/red]"
            return s

        line = " " * (W * first_day)
        for day in range(1, days_in_month + 1):
            line += color(all_prices[day - 1])
            if (first_day + day) % 7 == 0:
                console.print(line)
                line = ""
        if line.strip():
            console.print(line)
        console.print()


# ============================================================
# TREND ANALYSIS (from price_map.json + archive)
# ============================================================


def print_price_trends(origin, destination, archive_prices=None):
    if not os.path.exists(PRICE_MAP_FILE):
        console.print("[yellow]No price map data yet. Run a drift or scan first.[/yellow]")
        return
    price_map = _read_json_file(
        PRICE_MAP_FILE, {}, "price map", quarantine=False
    )

    direction = f"{origin}_{destination}"
    rev_direction = f"{destination}_{origin}"

    for lbl, dkey in [(f"{origin} \u2192 {destination}", direction), (f"{destination} \u2192 {origin}", rev_direction)]:
        day_data = price_map.get(dkey, {})
        has_archive = archive_prices and any(a.get(dkey) for a in archive_prices.values())
        if not day_data and not has_archive:
            continue
        console.print(f"\n[bold]Price Trends \u2014 {lbl}[/bold]")

        day_order = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        day_stats = {}
        all_fares = {}
        all_prices = []

        for day_name, patterns in day_data.items():
            totals = []
            for pat_key, pat in patterns.items():
                for fcode, fdata in pat.get("fares", {}).items():
                    avg = fdata["total"] // fdata["samples"]
                    totals.append(avg)
                    all_fares.setdefault(fcode, 0)
                    all_fares[fcode] += fdata["samples"]
                    all_prices.append(avg)
            if totals:
                day_stats[day_name] = {
                    "avg": sum(totals) // len(totals),
                    "min": min(totals),
                    "max": max(totals),
                    "count": len(totals),
                }

        if archive_prices:
            for date_str, directions in archive_prices.items():
                if dkey not in directions:
                    continue
                day_name = datetime.strptime(date_str, "%Y-%m-%d").strftime("%a")
                for fare, price in directions[dkey].items():
                    all_fares.setdefault(fare, 0)
                    all_fares[fare] += 1
                    all_prices.append(price)
                    if day_name not in day_stats:
                        day_stats[day_name] = {"avg": price, "min": price, "max": price, "count": 1}
                    else:
                        s = day_stats[day_name]
                        s["min"] = min(s["min"], price)
                        s["max"] = max(s["max"], price)
                        s["total"] = s.get("total", s["avg"] * s["count"]) + price
                        s["count"] += 1
                        s["avg"] = s["total"] // s["count"]

        if not day_stats:
            console.print("   No data\n")
            continue

        best_day = min(day_order, key=lambda d: day_stats.get(d, {}).get("avg", 999999))
        worst_day = max(day_order, key=lambda d: day_stats.get(d, {}).get("avg", 0))

        console.print(f"   Cheapest day:  [green]{best_day}[/green] (avg {day_stats[best_day]['avg']})" if best_day in day_stats else "")
        console.print(f"   Most expensive: [red]{worst_day}[/red] (avg {day_stats[worst_day]['avg']})" if worst_day in day_stats else "")

        if all_prices:
            avg_p = sum(all_prices) // len(all_prices)
            stddev = (sum((p - avg_p) ** 2 for p in all_prices) / len(all_prices)) ** 0.5
            vol = (stddev / avg_p) * 100 if avg_p else 0
            console.print(f"   Volatility:     \u00b1{vol:.1f}%")

        console.print(f"   Fare classes:  {', '.join(sorted(all_fares.keys()))}" if all_fares else "")

        table = Table(box=SIMPLE_HEAVY, header_style="bold cyan")
        table.add_column("Day", style="white")
        table.add_column("Avg", justify="right", style="yellow")
        table.add_column("Min", justify="right", style="green")
        table.add_column("Max", justify="right", style="red")
        table.add_column("Samples", justify="right", style="dim")
        for d in day_order:
            if d not in day_stats:
                continue
            s = day_stats[d]
            table.add_row(d, str(s["avg"]), str(s["min"]), str(s["max"]), str(s["count"]))
        console.print(table)


# ============================================================
# MERGE CABINS (group economy + business by itinerary)
# ============================================================


def make_merge_key(result):
    parts = []
    for s in result["segments"]:
        parts.append(f"{s['flight']}|{s['departure']}|{s['arrival']}")
    return "::".join(parts)


def merge_cabins(results):
    groups = {}
    for r in results:
        key = make_merge_key(r)
        if key not in groups:
            groups[key] = {
                "origin": r["origin"],
                "destination": r["destination"],
                "departure_date": r["departure_date"],
                "duration": r["duration"],
                "duration_seconds": r["duration_seconds"],
                "stops": r["stops"],
                "segments": r["segments"],
                "cabins": {},
            }
        cabin = r["cabin"]
        group = groups[key]
        if cabin not in group["cabins"]:
            group["cabins"][cabin] = []
        group["cabins"][cabin].append({
            "fare": r["fare"],
            "price": r["price"],
            "currency": r["currency"],
            "avios": r["avios"],
            "available_seats": r["available_seats"],
        })
    return list(groups.values())


# ============================================================
# FILTERING & SCORING
# ============================================================


def filter_results(results, max_price_map, max_duration_hours):
    filtered = []
    for r in results:
        cabin = r["cabin"]
        price = r["price"]
        max_p = max_price_map.get(cabin)
        if max_p is not None and price is not None and price > max_p:
            continue
        if max_duration_hours and r.get("duration_seconds"):
            if r["duration_seconds"] > max_duration_hours * 3600:
                continue
        filtered.append(r)
    return filtered


def filter_dirt_cheap(results):
    min_econ = None
    for r in results:
        if r.get("cabin") == "ECONOMY" and r.get("price") is not None:
            if min_econ is None or r["price"] < min_econ:
                min_econ = r["price"]
    if min_econ is None:
        return results
    threshold = int(min_econ * 1.3)
    return [
        r for r in results
        if not (r.get("cabin") == "BUSINESS" and r.get("fare") in ("BUSINESS_LITE", "BUSINESS_ELITE") and (r.get("price") or 0) > threshold)
    ]


def score_results(results, weight):
    if not results:
        return results
    prices = [r["price"] or 0 for r in results]
    durations = [r["duration_seconds"] or 0 for r in results]
    max_price = max(prices) if prices else 1
    max_dur = max(durations) if durations else 1
    max_price = max(max_price, 1)
    max_dur = max(max_dur, 1)
    for r in results:
        np_val = (r["price"] or 0) / max_price
        nd_val = (r["duration_seconds"] or 0) / max_dur
        if weight == "price":
            r["_score"] = np_val
        elif weight == "duration":
            r["_score"] = nd_val
        else:
            r["_score"] = 0.5 * np_val + 0.5 * nd_val
    results.sort(key=lambda x: x["_score"])
    return results


# ============================================================
# OUTPUT
# ============================================================


def print_compare_table(results, title="Flight Results", filter_note=None):
    if not results:
        console.print("[yellow]No results found.[/yellow]")
        return
    table = Table(title=title, box=SIMPLE_HEAVY, header_style="bold cyan")
    table.add_column("Date", style="white")
    table.add_column("Route", style="white")
    table.add_column("Dur", style="yellow")
    table.add_column("Flight", style="green")
    table.add_column("Aircraft", style="dim white")
    table.add_column("Layout", style="magenta")
    table.add_column("Dep", style="white")
    table.add_column("Arr", style="white")
    table.add_column("Econ", style="cyan")
    table.add_column("Biz", style="magenta")
    table.add_column("Seats", style="white", justify="right")


    def fmt_fares(fares):
        if not fares:
            return "-"
        currency = fares[0].get("currency", "")
        converted = fares[0].get("_converted")
        label = "NOK*" if converted else (currency if currency else "")
        parts = [f"{FARE_SHORT.get(f['fare'], f['fare'])} {f['price']}" for f in fares]
        return " / ".join(parts) + (f" {label}" if label else "")

    for r in results:
        cabins = r.get("cabins", {})
        dep_dt = r["segments"][0]["departure"] if r["segments"] else ""
        arr_dt = r["segments"][-1]["arrival"] if r["segments"] else ""
        flights = ", ".join(s["flight"] for s in r["segments"]) if r["segments"] else ""
        acodes = ", ".join(s["aircraft"] for s in r["segments"]) if r["segments"] else ""
        layouts = ", ".join(s["layout"] for s in r["segments"] if s.get("layout")) if r["segments"] else "-"
        dep_time = dep_dt[11:16] if dep_dt else ""
        arr_time = arr_dt[11:16] if arr_dt else ""

        econ_str = fmt_fares(cabins.get("ECONOMY", []))
        biz_str = fmt_fares(cabins.get("BUSINESS", []))

        all_seats = []
        for cabin, fares in cabins.items():
            if fares:
                s = fares[0].get("available_seats")
                if s is not None:
                    all_seats.append(f"{cabin[:1]}{s}")
        seats_str = " ".join(all_seats) if all_seats else "-"

        table.add_row(
            r.get("departure_date", ""),
            f"{r.get('origin','')}\u2192{r.get('destination','')}",
            r["duration"] or "-",
            flights,
            acodes,
            layouts,
            dep_time,
            arr_time,
            econ_str,
            biz_str,
            seats_str,
        )

    console.print(table)
    console.print(FARE_LEGEND, style="white")
    if filter_note:
        console.print(filter_note, style="dim white")


def print_json(results):
    clean = [{k: v for k, v in r.items() if not k.startswith("_")} for r in results]
    console.print(json.dumps(clean, indent=2, ensure_ascii=False))


def print_json_combined(data):
    clean = {}
    for direction, items in data.items():
        clean[direction] = [
            {k: v for k, v in r.items() if not k.startswith("_")} for r in items
        ]
    console.print(json.dumps(clean, indent=2, ensure_ascii=False))


# ============================================================
# OFFERS CACHE (raw API responses, refetch once per day)
# ============================================================

_nocache = False


def set_nocache(val):
    global _nocache
    _nocache = val


def load_offers_cache():
    return _read_json_file(OFFERS_CACHE_FILE, {}, "offers cache")


def save_offers_cache(cache):
    try:
        with _locked_data():
            current = _read_json_file(OFFERS_CACHE_FILE, {}, "offers cache")
            current.update(cache)
            _atomic_write_json(OFFERS_CACHE_FILE, current)
    except LocalStateError:
        raise
    except (OSError, TypeError, ValueError) as e:
        log.print(f"[yellow]Could not save offers cache: {escape(str(e))}[/yellow]")


def archive_stale_cache():
    if not os.path.exists(OFFERS_CACHE_FILE):
        return 0
    try:
        with _locked_data():
            cache = _read_json_file(OFFERS_CACHE_FILE, {}, "offers cache")
            if not cache:
                return 0
            today = datetime.now().strftime("%Y-%m-%d")
            stale = {}
            fresh = {}
            for key, entry in cache.items():
                if not isinstance(entry, dict):
                    fresh[key] = entry
                    continue
                entry_date = entry.get("date", "")
                if not isinstance(entry_date, str):
                    fresh[key] = entry
                elif entry_date < today:
                    stale[key] = entry
                else:
                    fresh[key] = entry
            if not stale:
                return 0
            archive = _read_json_file(
                CACHE_ARCHIVE_FILE, [], "cache archive", expected_type=list
            )
            now = datetime.now(timezone.utc).isoformat()
            archive_by_key = {}
            for a in archive:
                if isinstance(a, dict) and a.get("key"):
                    archive_by_key[a["key"]] = a
            for key, entry in stale.items():
                archive_by_key[key] = {
                    "archived_at": now,
                    "key": key,
                    "entry": entry,
                }
            cutoff = (
                datetime.now() - timedelta(days=ARCHIVE_MAX_DAYS)
            ).strftime("%Y-%m-%d")
            archive = [
                a for a in archive_by_key.values()
                if isinstance(a.get("entry"), dict)
                and isinstance(a["entry"].get("date"), str)
                and a["entry"]["date"] >= cutoff
            ]
            _atomic_write_json(CACHE_ARCHIVE_FILE, archive)
            _atomic_write_json(OFFERS_CACHE_FILE, fresh)
            return len(stale)
    except LocalStateError:
        raise
    except (OSError, TypeError, ValueError) as e:
        log.print(f"[yellow]Could not archive offers cache: {escape(str(e))}[/yellow]")
        return 0


# ============================================================
# REQUEST
# ============================================================

API_URL = "https://www.qatarairways.com/dapi/public/bff/web/flight-search/flight-offers"


class RateLimited(Exception):
    def __init__(self, remaining_seconds, blocked_until):
        self.remaining_seconds = remaining_seconds
        self.blocked_until = blocked_until
        minutes = max(1, math.ceil(remaining_seconds / 60))
        super().__init__(
            f"Rate-limited: {minutes} min remaining (until {blocked_until})"
        )


class BudgetExceeded(Exception):
    def __init__(self, budget):
        self.budget = budget
        label = "call" if budget == 1 else "calls"
        super().__init__(f"Per-run budget exhausted ({budget} live {label})")


class RateLimiter:
    def __init__(
        self,
        state_file=None,
        lock_file=None,
        base_delay=None,
        budget=None,
        force=False,
        clock=None,
        sleep=None,
        rng=None,
    ):
        self.state_file = RATE_STATE_FILE if state_file is None else state_file
        self.lock_file = RATE_LOCK_FILE if lock_file is None else lock_file
        self.base_delay = SCAN["delay_seconds"] if base_delay is None else base_delay
        self.budget = budget
        self.force = force
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.sleep = sleep or time.sleep
        self.rng = rng or random
        self.calls_this_run = 0

    @contextmanager
    def _lock(self):
        directory = os.path.dirname(self.lock_file)
        if directory:
            os.makedirs(directory, exist_ok=True)
        handle = open(self.lock_file, "a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()

    def _parse_timestamp(self, value):
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value)
            except ValueError:
                return None
        else:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _read_state(self):
        raw = {}
        try:
            with open(self.state_file) as f:
                text = f.read()
            if text.strip():
                raw = json.loads(text)
        except (OSError, ValueError, TypeError):
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        cutoff = self.clock() - timedelta(hours=24)
        calls = []
        raw_calls = raw.get("live_calls")
        if isinstance(raw_calls, list):
            for value in raw_calls:
                parsed = self._parse_timestamp(value)
                if parsed is not None and parsed >= cutoff:
                    calls.append(parsed.isoformat())
        blocked = self._parse_timestamp(raw.get("blocked_until"))
        return {
            "live_calls": calls,
            "blocked_until": blocked.isoformat() if blocked else None,
        }

    def _write_state(self, state):
        directory = os.path.dirname(self.state_file) or "."
        os.makedirs(directory, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(
            prefix=".rate_state.", suffix=".tmp", dir=directory
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(state, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, self.state_file)
        finally:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

    def _required_interval(self):
        base = max(1, int(self.base_delay))
        low = max(1, base - 5)
        high = max(low, base + 10)
        return self.rng.randint(low, high)

    def _blocked_info(self):
        with self._lock():
            state = self._read_state()
        blocked = self._parse_timestamp(state.get("blocked_until"))
        if blocked is None:
            return 0, None
        remaining = (blocked - self.clock()).total_seconds()
        if remaining <= 0:
            return 0, state.get("blocked_until")
        return math.ceil(remaining), state.get("blocked_until")

    def blocked_remaining(self):
        return self._blocked_info()[0]

    def is_blocked(self):
        return self.blocked_remaining() > 0

    def record_call(self):
        self.calls_this_run += 1
        try:
            with self._lock():
                state = self._read_state()
                state["live_calls"].append(self.clock().isoformat())
                self._write_state(state)
        except Exception as e:
            log.print(f"[yellow]Rate state update failed: {e}[/yellow]")
        if self.budget is not None:
            log.print(f"[dim]Live calls: {self.calls_this_run}/{self.budget}[/dim]")

    def wait(self):
        remaining, blocked_until = self._blocked_info()
        if remaining > 0 and not self.force:
            raise RateLimited(remaining, blocked_until)
        if self.budget is not None and self.calls_this_run >= self.budget:
            raise BudgetExceeded(self.budget)
        with self._lock():
            state = self._read_state()
            last_call = None
            for value in state["live_calls"]:
                parsed = self._parse_timestamp(value)
                if parsed is not None and (last_call is None or parsed > last_call):
                    last_call = parsed
            now = self.clock()
            delay = 0.0
            if last_call is not None:
                target = last_call + timedelta(seconds=self._required_interval())
                if now < target:
                    delay = (target - now).total_seconds()
        if delay > 0:
            log.print(f"[dim]Rate-limit pause {delay:.0f}s[/dim]")
            self.sleep(delay)

    def record_block(self, minutes=20):
        with self._lock():
            state = self._read_state()
            blocked_until = self.clock() + timedelta(minutes=minutes)
            state["blocked_until"] = blocked_until.isoformat()
            self._write_state(state)

    def clear_block(self):
        with self._lock():
            state = self._read_state()
            state["blocked_until"] = None
            self._write_state(state)


_rate_limiter = None


def get_rate_limiter():
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = RateLimiter()
    return _rate_limiter


def configure_rate_limiter(**kwargs):
    global _rate_limiter
    _rate_limiter = RateLimiter(**kwargs)
    return _rate_limiter


def reset_rate_limiter():
    global _rate_limiter
    _rate_limiter = None


class EdgeBlock(Exception):
    def __init__(self, message, status=None, reference=None):
        self.status = status
        self.reference = reference
        super().__init__(message)


class AppAuthError(Exception):
    pass


class ScanAborted(Exception):
    def __init__(self, results, reason, cause=None):
        self.results = list(results or [])
        self.reason = reason
        self.cause = cause
        super().__init__(reason)


def _safe_body_summary(body):
    text = " ".join((body or "").split())
    lowered = text.lower()
    if any(
        marker in lowered
        for marker in ("<html", "<!doctype", "access denied", "edgesuite")
    ):
        return "HTML response body (omitted)"
    return text[:200]


class RequestFailed(Exception):
    def __init__(self, status, body):
        self.status = status
        self.body = body or ""
        self.body_summary = _safe_body_summary(self.body)
        if status == 0:
            message = "Network error"
        else:
            message = f"HTTP {status}"
        if self.body_summary:
            message = f"{message}: {self.body_summary}"
        super().__init__(message)


def _abort_reason(exc):
    if isinstance(exc, EdgeBlock):
        return "edge"
    if isinstance(exc, AppAuthError):
        return "auth"
    if isinstance(exc, RateLimited):
        return "rate"
    if isinstance(exc, BudgetExceeded):
        return "budget"
    raise exc


def _abort_message(aborted):
    cause = aborted.cause
    if aborted.reason == "edge":
        reference = getattr(cause, "reference", None)
        ref_text = f" ({reference})" if reference else ""
        return (
            f"Blocked at Akamai edge{ref_text} \u2014 wait 15\u201330 min or refresh with `--paste`."
        )
    if aborted.reason == "auth":
        return "Session rejected by the API \u2014 refresh with `--paste`."
    if aborted.reason == "rate":
        remaining = getattr(cause, "remaining_seconds", 0) or 0
        minutes = max(1, math.ceil(remaining / 60))
        return (
            f"Rate-limit cooldown active \u2014 about {minutes} min remaining. "
            "Use `--force` for this run or `--clear-block` to reset."
        )
    if aborted.reason == "budget":
        budget = getattr(cause, "budget", "?")
        return (
            f"Live-call budget exhausted ({budget} calls). "
            "Re-run later or raise `--budget`."
        )
    return "Scan aborted."


def _record_block_for_abort(aborted):
    if aborted.reason == "edge":
        get_rate_limiter().record_block(SCAN["block_minutes"])


def _log_error(text):
    log.print(f"[red]{escape(str(text))}[/red]")


def _edge_reference(body):
    match = re.search(r"Reference #([A-Za-z0-9.]+)", body or "", re.I)
    return match.group(1) if match else None


def make_trace_headers():
    trace_id = secrets.token_hex(16)
    span_id = secrets.token_hex(8)
    return {
        "Request-Id": f"|{trace_id}.{span_id}",
        "traceparent": f"00-{trace_id}-{span_id}-01",
    }


def build_referer(origin, destination, depart, ret):
    params = {
        "widget": "QR",
        "searchType": "F",
        "addTaxToFare": "Y",
        "minPurTime": "0",
        "selLang": "en",
        "tripType": "R",
        "fromStation": origin,
        "toStation": destination,
        "departing": depart,
        "returning": ret,
        "bookingClass": "E",
        "adults": "1",
        "children": "0",
        "infants": "0",
        "ofw": "0",
        "teenager": "0",
        "flexibleDate": "off",
        "allowRedemption": "N",
        "sort": "duration",
    }
    return (
        "https://www.qatarairways.com/app/booking/flight-selection?"
        + urlencode(params)
    )


def classify_block(status, body):
    if status == 200:
        return None
    text = body or ""
    lowered = text.lower()
    if status == 403 and (
        "<html" in lowered
        or "access denied" in lowered
        or "edgesuite" in lowered
    ):
        return "edge"
    if status in (401, 403):
        try:
            json.loads(text)
            return "auth"
        except (TypeError, ValueError):
            return "other"
    return "other"


class HttpClient:
    def __init__(self, headers, cookies):
        self.headers = dict(headers)
        self.cookies = cookies
        self.session = requests.Session()
        self.session.cookies.update(cookies)

    def post_offers(self, payload, referer):
        headers = dict(self.headers)
        headers.update(make_trace_headers())
        headers["Referer"] = referer
        resp = self.session.post(
            API_URL,
            headers=headers,
            json=payload,
            timeout=30,
        )
        self.cookies.update(self.session.cookies.get_dict())
        return resp


def _persist_cookies(cookies):
    cfg = {}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                cfg = json.load(f)
        except (json.JSONDecodeError, OSError):
            cfg = {}
    cfg["cookies"] = cookies
    save_config(cfg)


def fetch_flights(headers, cookies, origin, destination, departure_date, referer=None):
    cache_key = f"{origin}_{destination}_{departure_date}"
    today = datetime.now().strftime("%Y-%m-%d")

    cache = load_offers_cache()

    if not _nocache:
        entry = cache.get(cache_key)
        if entry and entry.get("date") == today:
            log.print(f"[dim]Cached {cache_key}[/dim]")
            return entry["data"], 200, None

    referer = referer or headers.get("Referer", "https://www.qatarairways.com/")
    payload = build_payload(origin, destination, departure_date)
    client = HttpClient(headers, cookies)
    limiter = get_rate_limiter()
    limiter.wait()
    try:
        resp = client.post_offers(payload, referer)
    except requests.RequestException as e:
        raise RequestFailed(0, str(e)) from e
    finally:
        limiter.record_call()
    _persist_cookies(cookies)

    kind = classify_block(resp.status_code, resp.text)
    if kind == "edge":
        reference = _edge_reference(resp.text)
        detail = f"HTTP {resp.status_code}, ref {reference}" if reference else f"HTTP {resp.status_code}"
        raise EdgeBlock(
            f"Akamai edge block ({detail})",
            status=resp.status_code,
            reference=reference,
        )
    if kind == "auth":
        raise AppAuthError(f"Session/auth error (HTTP {resp.status_code})")
    if kind == "other":
        raise RequestFailed(resp.status_code, resp.text)

    data = resp.json()
    cache[cache_key] = {"date": today, "data": data}
    save_offers_cache(cache)
    return data, resp.status_code, None


def _fetch_with_retries(headers, cookies, origin, destination, departure_date, referer=None):
    attempts = 1 + SCAN["max_retries"]
    for attempt in range(attempts):
        try:
            return fetch_flights(
                headers, cookies, origin, destination, departure_date, referer=referer
            )
        except RequestFailed as e:
            retryable = e.status == 0 or e.status >= 500
            if attempt + 1 >= attempts or not retryable:
                raise
            log.print(
                f"[yellow]Transient failure ({escape(str(e))}); retrying.[/yellow]"
            )


def _fetch_and_parse(headers, cookies, origin, destination, date_str, dep_date, ret_date, direction, max_dur):
    referer = build_referer(SEARCH["origin"], SEARCH["destination"], dep_date, ret_date)
    try:
        data, status, err = _fetch_with_retries(
            headers, cookies, origin, destination, date_str, referer=referer
        )
    except RequestFailed as e:
        _log_error(f"  {e}")
        return []
    if data is None:
        _log_error(f"  HTTP {status}: {err}")
        return []
    results = parse_results(data, max_dur)
    convert_prices(results)
    for r in results:
        r["_dep_date"] = dep_date
        r["_ret_date"] = ret_date
        r["_direction"] = direction
    log.print(f"  [green]\u2192 {len(results)} offers[/green]")
    return results


# ============================================================
# CLI
# ============================================================


def parse_args(argv=None):
    p = ArgumentParser(
        description="Qatar Airways flight scanner",
        epilog=(
            "Examples:\n"
            "  %(prog)s                         single request, rich table\n"
            "  %(prog)s --json                  single request, JSON output\n"
            "  %(prog)s --paste                 paste fresh browser headers\n"
            "  %(prog)s --scan                  scan outbound \u00b14 days\n"
            "  %(prog)s --scan --ob 3           scan outbound \u00b13 days\n"
            "  %(prog)s --scan --rb 2           scan return \u00b12 days\n"
            "  %(prog)s --scan --ob 3 --rb 2    scan both directions\n"
            "  %(prog)s --drift 3                 drift both dates by ±3 days\n"
            "  %(prog)s --drift-ret 3             drift only return date by ±3 days\n"
            "  %(prog)s --scan --weight duration --max-price ECONOMY=8000\n"
            "  %(prog)s --all-fares             show BUSINESS_LITE/ELITE if dirt-cheap"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--paste", action="store_true", help="Paste fresh browser headers and save as config")
    p.add_argument("--json", action="store_true", help="Output raw JSON instead of table")
    p.add_argument("--scan", action="store_true", help="Enable scan mode (scan outbound by default)")
    p.add_argument("--ob", type=int, default=None, metavar="DAYS", help="Scan \u00b1DAYS for outbound")
    p.add_argument("--rb", type=int, default=None, metavar="DAYS", help="Scan \u00b1DAYS for return")
    p.add_argument("--min-trip", type=int, default=None, metavar="DAYS", help="Minimum trip length in days")
    p.add_argument("--weight", choices=["price", "duration", "balanced"], default=None, help="Optimization weight")
    p.add_argument("--max-price", type=str, default=None, metavar="CABIN=VAL,...", help="e.g. ECONOMY=9000,BUSINESS=15000")
    p.add_argument("--max-duration", type=int, default=None, metavar="HOURS", help="Max flight duration in hours")
    p.add_argument("--top", type=int, default=None, metavar="N", help="Show top N results")
    p.add_argument("--urls", action="store_true", help="Show booking URLs for date pairs")
    p.add_argument("--nocache", action="store_true", help="Bypass offers cache, fetch fresh data")
    p.add_argument("--nofilter", action="store_true", help="Disable max-price filtering, show all results")
    p.add_argument("--drift", type=int, default=None, metavar="N", help="Scan drift: ±N days shifting both outbound+return together")
    p.add_argument("--drift-ret", type=int, default=None, metavar="N", help="Scan drift on return only: ±N days shifting return date, outbound fixed")
    p.add_argument("--delay", type=int, default=None, metavar="SEC", help="Base spacing between live calls in seconds (default 45, jittered -5/+10)")
    p.add_argument("--max-retries", type=int, choices=[0, 1], default=0, metavar="{0,1}", help="Retry transient 5xx/timeout failures once (edge/auth/rate/budget are never retried)")
    p.add_argument("--budget", type=int, default=None, metavar="N", help="Maximum live POST attempts per run (default: unlimited)")
    p.add_argument("--force", action="store_true", help="Ignore an active rate-limit cooldown for this run")
    p.add_argument("--clear-block", action="store_true", help="Clear the persisted rate-limit cooldown and exit")
    p.add_argument("--price-map", action="store_true", help="Show price map (day-of-week price trends from collected data)")
    p.add_argument("--recommend", type=int, default=None, const=10, nargs="?", metavar="N", help="Show top N mixed-cabin combos (default 10)")
    p.add_argument("--all-fares", action="store_true", help="Include BUSINESS_LITE, BUSINESS_ELITE (excluded by default; ECONOMY_LITE/FIRST always hidden)")
    p.add_argument("--prune-cache", action="store_true", help="Archive stale cache entries (older than 30 days)")
    p.add_argument("--calendar", type=str, default=None, const="current", nargs="?", metavar="MONTH YEAR", help="Show price calendar for a month (e.g. 'Aug 2026', default: current month)")
    p.add_argument("--trends", action="store_true", help="Show price trend analysis from collected data")
    p.add_argument("--origin", "-o", type=str, default=None, metavar="CODE", help="Origin airport IATA (default: OSL)")
    p.add_argument("--destination", "-d", type=str, default=None, metavar="CODE", help="Destination airport IATA (default: BKK)")
    p.add_argument("--depart", type=str, default=None, metavar="YYYY-MM-DD", help="Departure date (default: 2026-08-17)")
    p.add_argument("--ret", type=str, default=None, metavar="YYYY-MM-DD", help="Return date (default: 2026-09-04)")
    return p.parse_args(argv)


# ============================================================
# SCAN HELPERS
# ============================================================


def _scan_dates(headers, cookies, args, max_price, weight, max_dur, top_n, raw, direction):
    if direction == "OB":
        base = datetime.strptime(SEARCH["departure_date"], "%Y-%m-%d").date()
        fixed = datetime.strptime(SEARCH["return_date"], "%Y-%m-%d").date()
        before_key, after_key = "days_before_outbound", "days_after_outbound"
        arg_val, label = args.ob, "Outbound"
        origin, dest = SEARCH["origin"], SEARCH["destination"]
    else:
        base = datetime.strptime(SEARCH["return_date"], "%Y-%m-%d").date()
        fixed = datetime.strptime(SEARCH["departure_date"], "%Y-%m-%d").date()
        before_key, after_key = "days_before_return", "days_after_return"
        arg_val, label = args.rb, "Return"
        origin, dest = SEARCH["destination"], SEARCH["origin"]

    before = arg_val if arg_val is not None else SCAN[before_key]
    after = arg_val if arg_val is not None else SCAN[after_key]
    min_trip = args.min_trip if args.min_trip is not None else SCAN["min_trip_days"]

    all_results = []
    dates_to_check = [base + timedelta(days=i) for i in range(-before, after + 1)]
    total = len(dates_to_check)

    aborted = None
    try:
        for idx, d in enumerate(dates_to_check):
            trip = (fixed - d).days if direction == "OB" else (d - fixed).days
            if trip < min_trip:
                log.print(f"[dim]Skipping {d}: trip={trip}d < min {min_trip}d[/dim]")
                continue
            date_str = d.isoformat()
            log.print(f"[cyan]{label} request {idx+1}/{total}: {d}[/cyan]")
            dep_date = date_str if direction == "OB" else fixed.isoformat()
            ret_date = fixed.isoformat() if direction == "OB" else date_str
            results = _fetch_and_parse(headers, cookies, origin, dest, date_str, dep_date, ret_date, direction, max_dur)
            all_results.extend(results)
    except (EdgeBlock, AppAuthError, RateLimited, BudgetExceeded) as exc:
        aborted = exc

    update_price_map(all_results)
    all_results = filter_dirt_cheap(all_results)

    if raw:
        final = all_results
    else:
        filtered = filter_results(all_results, max_price, max_dur)
        if not filtered:
            if aborted is None:
                log.print("[yellow]No results passed filters (max price/duration).[/yellow]")
            final = []
        else:
            final = score_results(filtered, weight)[:top_n]

    if aborted is not None:
        log.print(f"[yellow]Scan aborted; showing {len(final)} partial result(s).[/yellow]")
        raise ScanAborted(final, _abort_reason(aborted), aborted) from aborted
    return final


def scan_outbound(headers, cookies, args, max_price, weight, max_dur, top_n, raw=False):
    return _scan_dates(headers, cookies, args, max_price, weight, max_dur, top_n, raw, "OB")


def scan_return(headers, cookies, args, max_price, weight, max_dur, top_n, raw=False):
    return _scan_dates(headers, cookies, args, max_price, weight, max_dur, top_n, raw, "RB")


# ============================================================
# DRIFT SCAN (shift both dates together, preserving trip length)
# ============================================================


def drift_scan(headers, cookies, args, max_price, max_dur):
    base_ob = datetime.strptime(SEARCH["departure_date"], "%Y-%m-%d").date()
    base_rb = datetime.strptime(SEARCH["return_date"], "%Y-%m-%d").date()
    trip_days = (base_rb - base_ob).days
    drift = args.drift

    all_results = []
    offsets = list(range(-drift, drift + 1))

    total = len(offsets)

    aborted = None
    try:
        for idx, offset in enumerate(offsets):
            ob_date = base_ob + timedelta(days=offset)
            rb_date = base_rb + timedelta(days=offset)
            ob_str = ob_date.isoformat()
            rb_str = rb_date.isoformat()
            log.print(f"[cyan]Drift {idx+1}/{total}: {ob_date} \u2192 {rb_date} ({trip_days}d trip)[/cyan]")

            results = _fetch_and_parse(headers, cookies, SEARCH["origin"], SEARCH["destination"], ob_str, ob_str, rb_str, "OB", max_dur)
            all_results.extend(results)

            results = _fetch_and_parse(headers, cookies, SEARCH["destination"], SEARCH["origin"], rb_str, ob_str, rb_str, "RB", max_dur)
            all_results.extend(results)
    except (EdgeBlock, AppAuthError, RateLimited, BudgetExceeded) as exc:
        aborted = exc

    update_price_map(all_results)
    all_results = filter_dirt_cheap(all_results)

    if not all_results:
        final = []
    elif args.nofilter:
        final = all_results
    else:
        filtered = filter_results(all_results, max_price, max_dur)
        if not filtered:
            if aborted is None:
                log.print("[yellow]No results passed filters (max price/duration).[/yellow]")
            final = []
        else:
            final = filtered

    if aborted is not None:
        log.print(f"[yellow]Scan aborted; showing {len(final)} partial result(s).[/yellow]")
        raise ScanAborted(final, _abort_reason(aborted), aborted) from aborted
    return final


def drift_scan_return(headers, cookies, args, max_price, max_dur):
    base_ob = datetime.strptime(SEARCH["departure_date"], "%Y-%m-%d").date()
    base_rb = datetime.strptime(SEARCH["return_date"], "%Y-%m-%d").date()
    drift = args.drift_ret

    all_results = []
    offsets = list(range(-drift, drift + 1))

    total = len(offsets)

    aborted = None
    try:
        for idx, offset in enumerate(offsets):
            ob_date = base_ob
            rb_date = base_rb + timedelta(days=offset)
            ob_str = ob_date.isoformat()
            rb_str = rb_date.isoformat()
            trip_days = (rb_date - ob_date).days

            if rb_date <= ob_date:
                log.print(f"[dim]Skipping {rb_date}: return before or equal to departure[/dim]")
                continue
            min_trip = args.min_trip if args.min_trip is not None else SCAN["min_trip_days"]
            if trip_days < min_trip:
                log.print(f"[dim]Skipping {rb_date}: trip={trip_days}d < min {min_trip}d[/dim]")
                continue

            log.print(f"[cyan]Drift-Ret {idx+1}/{total}: {ob_date} \u2192 {rb_date} ({trip_days}d trip)[/cyan]")

            results = _fetch_and_parse(headers, cookies, SEARCH["origin"], SEARCH["destination"], ob_str, ob_str, rb_str, "OB", max_dur)
            all_results.extend(results)

            results = _fetch_and_parse(headers, cookies, SEARCH["destination"], SEARCH["origin"], rb_str, ob_str, rb_str, "RB", max_dur)
            all_results.extend(results)
    except (EdgeBlock, AppAuthError, RateLimited, BudgetExceeded) as exc:
        aborted = exc

    update_price_map(all_results)
    all_results = filter_dirt_cheap(all_results)

    if not all_results:
        final = []
    elif args.nofilter:
        final = all_results
    else:
        filtered = filter_results(all_results, max_price, max_dur)
        if not filtered:
            if aborted is None:
                log.print("[yellow]No results passed filters (max price/duration).[/yellow]")
            final = []
        else:
            final = filtered

    if aborted is not None:
        log.print(f"[yellow]Scan aborted; showing {len(final)} partial result(s).[/yellow]")
        raise ScanAborted(final, _abort_reason(aborted), aborted) from aborted
    return final


def print_drift_pair_table(results, title=None, filter_note=None):
    pairs = {}
    for r in results:
        dep = r.get("_dep_date")
        ret = r.get("_ret_date")
        if not dep or not ret:
            continue
        key = f"{dep}|{ret}"
        if key not in pairs:
            pairs[key] = {"dep": dep, "ret": ret, "offers": []}
        pairs[key]["offers"].append(r)

    if not pairs:
        console.print("[yellow]No results found.[/yellow]")
        return

    table = Table(title=title, box=SIMPLE_HEAVY, header_style="bold cyan")
    table.add_column("Date Pair", style="white")
    table.add_column("OB Econ", style="cyan")
    table.add_column("OB Biz", style="magenta")
    table.add_column("RB Econ", style="cyan")
    table.add_column("RB Biz", style="magenta")

    def worst_layout(segs):
        return min(LAYOUT_RANK.get(s.get("layout", "Unknown"), 0) for s in segs)

    def best_layout_str(segs):
        ranked = sorted(
            [s.get("layout", "Unknown") for s in segs if s.get("layout")],
            key=lambda x: LAYOUT_RANK.get(x, 0),
        )
        return ranked[0] if ranked else "?"

    def cell_text(dir_cabin):
        r = dir_cabin
        if not r:
            return "-"
        fc = FARE_SHORT.get(r["fare"], r["fare"])
        p = r["price"]
        s = "*" if r.get("_converted") else ""
        if r["cabin"] == "BUSINESS":
            lay = best_layout_str(r.get("segments", []))
            return f"{fc} {p}{s} {lay}"
        return f"{fc} {p}{s}"

    def best_offer(offers, direction, cabin):
        best = None
        for r in offers:
            if r.get("_direction") != direction or r.get("cabin") != cabin:
                continue
            if best is None or (r.get("price") or 0) < (best.get("price") or 0):
                best = r
        return best

    for key in sorted(pairs.keys()):
        p = pairs[key]
        date_label = f"{p['dep']} \u2192 {p['ret']}"
        table.add_row(
            date_label,
            cell_text(best_offer(p["offers"], "OB", "ECONOMY")),
            cell_text(best_offer(p["offers"], "OB", "BUSINESS")),
            cell_text(best_offer(p["offers"], "RB", "ECONOMY")),
            cell_text(best_offer(p["offers"], "RB", "BUSINESS")),
        )

    console.print(table)
    console.print(FARE_LEGEND, style="white")
    if filter_note:
        console.print(filter_note, style="dim white")


# ============================================================
# MIXED-CABIN MATCHER (best combos across date pairs)
# ============================================================


def find_best_mixed(results, top_n=10):
    pairs = {}
    for r in results:
        dep = r.get("_dep_date")
        ret = r.get("_ret_date")
        if not dep or not ret:
            continue
        key = f"{dep}|{ret}"
        pairs.setdefault(key, {"_dep_date": dep, "_ret_date": ret, "offers": []})
        pairs[key]["offers"].append(r)

    combos = []

    for key, p in pairs.items():
        offers = p["offers"]

        def best(direction, cabin, exclude_set=None):
            best_r = None
            for r in offers:
                if r.get("_direction") != direction or r.get("cabin") != cabin:
                    continue
                if exclude_set and r.get("fare") in exclude_set:
                    continue
                if best_r is None or (r.get("price") or 0) < (best_r.get("price") or 0):
                    best_r = r
            return best_r

        ob_e = best("OB", "ECONOMY", {"ECONOMY_LITE"})
        ob_b = best("OB", "BUSINESS", {"BUSINESS_LITE", "BUSINESS_ELITE"})
        rb_e = best("RB", "ECONOMY", {"ECONOMY_LITE"})
        rb_b = best("RB", "BUSINESS", {"BUSINESS_LITE", "BUSINESS_ELITE"})

        configs = [
            ("Econ+Biz", ob_e, rb_b),
            ("Biz+Econ", ob_b, rb_e),
            ("Econ+Econ", ob_e, rb_e),
            ("Biz+Biz", ob_b, rb_b),
        ]

        for label, leg1, leg2 in configs:
            if not leg1 or not leg2:
                continue
            total = (leg1.get("price") or 0) + (leg2.get("price") or 0)
            biz_leg = leg1 if leg1.get("cabin") == "BUSINESS" else leg2 if leg2.get("cabin") == "BUSINESS" else None
            biz_layout = None
            if biz_leg:
                segs = biz_leg.get("segments", [])
                ranked = sorted(
                    [s.get("layout", "Unknown") for s in segs if s.get("layout")],
                    key=lambda x: LAYOUT_RANK.get(x, 0),
                )
                biz_layout = ranked[0] if ranked else None

            combos.append({
                "label": label,
                "pair": f"{p['_dep_date']} \u2192 {p['_ret_date']}",
                "_dep_date": p["_dep_date"],
                "_ret_date": p["_ret_date"],
                "leg1": leg1,
                "leg2": leg2,
                "total": total,
                "biz_layout": biz_layout,
            })

    combos.sort(key=lambda c: c["total"])
    return combos[:top_n]


def print_mixed_table(combos):
    if not combos:
        return
    table = Table(title="Best Mixed-Cabin Combos (sorted by total price)", box=SIMPLE_HEAVY, header_style="bold cyan")
    table.add_column("#", style="dim", justify="right")
    table.add_column("Combo", style="white")
    table.add_column("Date Pair", style="white")
    table.add_column("OB", style="cyan")
    table.add_column("RB", style="magenta")
    table.add_column("Total", style="bold yellow", justify="right")

    def fmt_leg(r):
        fc = FARE_SHORT.get(r["fare"], r["fare"])
        p = r["price"]
        s = "*" if r.get("_converted") else ""
        dur = r.get("duration", "")
        return f"{fc} {p}{s}  {dur}"

    for i, c in enumerate(combos, 1):
        ob_leg = c["leg1"] if c["leg1"].get("_direction") == "OB" else c["leg2"]
        rb_leg = c["leg2"] if c["leg2"].get("_direction") == "RB" else c["leg1"]
        ob_str = fmt_leg(ob_leg)
        rb_str = fmt_leg(rb_leg)
        if c["biz_layout"]:
            biz_side = "OB" if any(s.get("_direction") == "OB" and s.get("cabin") == "BUSINESS" for s in [c["leg1"], c["leg2"]]) else "RB"
            if biz_side == "OB":
                ob_str += f" {c['biz_layout']}"
            else:
                rb_str += f" {c['biz_layout']}"
        table.add_row(str(i), c["label"], c["pair"], ob_str, rb_str, str(c["total"]))
    console.print(table)
    console.print(FARE_LEGEND, style="white")


def print_drift_summary(merged_results):
    for cabin in ["ECONOMY", "BUSINESS"]:
        cabin_rows = [r for r in merged_results if cabin in r.get("cabins", {})]
        if not cabin_rows:
            continue

        best_fare, best_br = None, None
        for r in cabin_rows:
            for f in r["cabins"][cabin]:
                if best_fare is None or f["price"] < best_fare["price"]:
                    best_fare, best_br = f, r

        best_dr = min(cabin_rows, key=lambda r: r["duration_seconds"] or 999999)

        def line(label, *parts):
            p = "  ".join(str(x) for x in parts if x)
            console.print(f"  {label:<14} {p}")

        def worst_layout_rank(segments):
            return min(LAYOUT_RANK.get(s.get("layout", "Unknown"), 0) for s in segments)

        best_layout_row = max(cabin_rows, key=lambda r: worst_layout_rank(r.get("segments", [])))

        console.print(f"\n[bold]{cabin} — Best of Drift:[/bold]")
        if best_fare:
            s = "*" if best_fare.get("_converted") else ""
            c = FARE_SHORT.get(best_fare["fare"], best_fare["fare"])
            line("Best price:", f"{c} {best_fare['price']} NOK{s}", best_br["departure_date"], f"{best_br['origin']}\u2192{best_br['destination']}", best_br["duration"])
        line("Best duration:", best_dr["departure_date"], f"{best_dr['origin']}\u2192{best_dr['destination']}", best_dr["duration"])
        layouts_str = ", ".join(s["layout"] for s in best_layout_row["segments"])
        line("Best layout:", layouts_str, best_layout_row["departure_date"], f"{best_layout_row['origin']}\u2192{best_layout_row['destination']}", best_layout_row["duration"])

        if best_fare:
            url = booking_url(best_br["origin"], best_br["destination"], best_br.get("_dep_date", best_br["departure_date"]), best_br.get("_ret_date", best_br["departure_date"]), cabin)
            console.print(f"  {'Book:':<14} {url}")


def print_booking_links(results):
    pairs = set()
    for r in results:
        dep = r.get("_dep_date") or r.get("departure_date")
        ret = r.get("_ret_date")
        if dep and ret:
            pairs.add((r["origin"], r["destination"], dep, ret))
    if pairs:
        console.print("\n[bold]Booking links:[/bold]")
        for origin, dest, dep, ret in sorted(pairs):
            url = booking_url(origin, dest, dep, ret, "ECONOMY")
            console.print(f"  {dep} \u2192 {ret}  {url}")


# ============================================================
# MAIN
# ============================================================


def main():
    try:
        _main()
    except LocalStateError as e:
        log.print(f"[red]{escape(str(e))}[/red]")
        sys.exit(1)


def _main():
    args = parse_args()
    SCAN["max_retries"] = max(0, min(1, args.max_retries))
    configure_rate_limiter(
        base_delay=args.delay if args.delay is not None else SCAN["delay_seconds"],
        budget=args.budget,
        force=args.force,
    )

    if args.clear_block:
        get_rate_limiter().clear_block()
        log.print("[green]Rate-limit cooldown cleared.[/green]")
        return

    if args.force:
        limiter = get_rate_limiter()
        if limiter.is_blocked():
            log.print(
                "[yellow]Ignoring active cooldown (--force); it remains on disk.[/yellow]"
            )

    # -- paste mode
    if args.paste:
        cfg = prompt_paste_headers()
    else:
        cfg = load_config()
        if cfg is None:
            log.print(
                "[yellow]No saved config found. Run with --paste to set up headers.[/yellow]"
            )
            sys.exit(1)

    if args.price_map:
        print_price_map()
        return

    headers = cfg.get("headers", {})
    cookies = cfg.get("cookies", {})

    set_nocache(args.nocache)

    # Cache archival (auto on every run)
    archived = archive_stale_cache()
    if archived:
        log.print(f"[dim]Archived {archived} stale entries, archive trimmed to {ARCHIVE_MAX_DAYS}d[/dim]")

    # --prune-cache: manual archive trigger
    if args.prune_cache:
        log.print(f"[green]Pruned: {archived} entries archived, archive \u2264 {ARCHIVE_MAX_DAYS}d[/green]")
        return

    # --calendar / --trends: analytics mode (can combine both)
    if args.calendar or args.trends:
        archive_prices = _load_archive_prices()
        if args.calendar:
            try:
                cal_year, cal_month = _parse_calendar_arg(args.calendar)
            except ValueError as e:
                log.print(f"[red]{e}[/red]")
                sys.exit(1)
            print_price_calendar(cal_year, cal_month, SEARCH["origin"], SEARCH["destination"], archive_prices=archive_prices)
        if args.trends:
            print_price_trends(SEARCH["origin"], SEARCH["destination"], archive_prices=archive_prices)
        return

    if not validate_config(cfg):
        sys.exit(1)

    # CLI overrides for search params
    if args.origin:
        SEARCH["origin"] = args.origin.upper()
    if args.destination:
        SEARCH["destination"] = args.destination.upper()
    if args.depart:
        SEARCH["departure_date"] = args.depart
    if args.ret:
        SEARCH["return_date"] = args.ret
    # Validate dates
    try:
        ob_date = datetime.strptime(SEARCH["departure_date"], "%Y-%m-%d")
        rb_date = datetime.strptime(SEARCH["return_date"], "%Y-%m-%d")
        if ob_date >= rb_date:
            log.print("[red]Departure date must be before return date.[/red]")
            sys.exit(1)
    except ValueError as e:
        log.print(f"[red]Invalid date: {e}[/red]")
        sys.exit(1)

    # parse --max-price override e.g. "ECONOMY=8000,BUSINESS=14000"
    max_price = dict(SCAN["max_price"])
    if args.max_price:
        for part in args.max_price.split(","):
            if "=" in part:
                cabin, val = part.split("=", 1)
                max_price[cabin.strip()] = int(val.strip())

    # --all-fares: include lite/elite fare types
    if args.all_fares:
        PREFERENCES["show_fares"] = list(ALL_FARES)
        log.print("[dim]Including all fare types (LITE, ELITE)[/dim]")

    weight = args.weight or SCAN["weight"]
    max_dur = args.max_duration or SCAN["max_duration_hours"]
    top_n = args.top or SCAN["top_n"]

    # Search summary
    log.print(
        f"[bold]Search:[/bold] {SEARCH['origin']} \u2192 {SEARCH['destination']}"
        f"  [bold]Dates:[/bold] {SEARCH['departure_date']} \u2192 {SEARCH['return_date']}"
        f"  [bold]Max dur:[/bold] {max_dur}h"
    )

    # determine scan mode
    # --drift N = drift scan (shifts both dates together)
    # --drift-ret N = drift scan (shifts only return date)
    # --scan alone = scan outbound (default)
    # --ob N = scan outbound
    # --rb N = scan return
    # --ob N --rb N = scan both
    in_scan_mode = args.scan or args.ob is not None or args.rb is not None

    def display(results, title):
        if not results:
            log.print("[yellow]No results found.[/yellow]")
            return
        merged = merge_cabins(results)
        if args.json:
            print_json(merged)
        else:
            print_compare_table(merged, title=title)

    filter_note = None
    if not args.nofilter:
        price_filter_active = args.drift is not None or args.drift_ret is not None or not in_scan_mode
        if price_filter_active:
            filter_note = f"Filtered by max price: ECONOMY \u2264 {max_price.get('ECONOMY','?')} NOK, BUSINESS \u2264 {max_price.get('BUSINESS','?')} NOK"

    def display_outbound_partial(results):
        ob_label = f"Outbound \u2014  {SEARCH['origin']}\u2192{SEARCH['destination']}  {SEARCH['departure_date']}"
        if args.json:
            print_json_combined({"outbound": merge_cabins(results), "return": []})
        else:
            print_compare_table(merge_cabins(results), title=ob_label, filter_note=filter_note)
        if args.urls and results:
            print_booking_links(results)

    if args.drift is not None:
        log.rule("[bold cyan]Drift scan[/bold cyan]")
        aborted = None
        try:
            all_results = drift_scan(headers, cookies, args, max_price, max_dur)
        except ScanAborted as e:
            all_results = e.results
            aborted = e
        if aborted is not None and aborted.reason == "edge":
            _record_block_for_abort(aborted)
        if all_results:
            label = f"Drift scan \u2014 {SEARCH['origin']}\u2192{SEARCH['destination']} (max {max_dur}h)"
            print_drift_pair_table(all_results, title=label, filter_note=filter_note)
            merged = merge_cabins(all_results)
            print_drift_summary(merged)
            if args.recommend:
                combos = find_best_mixed(all_results, top_n=args.recommend)
                print_mixed_table(combos)
            if args.urls:
                print_booking_links(all_results)
        if aborted is not None:
            _log_error(_abort_message(aborted))
            sys.exit(1)
        if not all_results:
            return
    elif args.drift_ret is not None:
        log.rule("[bold cyan]Drift-Ret scan[/bold cyan]")
        aborted = None
        try:
            all_results = drift_scan_return(headers, cookies, args, max_price, max_dur)
        except ScanAborted as e:
            all_results = e.results
            aborted = e
        if aborted is not None and aborted.reason == "edge":
            _record_block_for_abort(aborted)
        if all_results:
            label = f"Drift-Ret \u2014 {SEARCH['origin']}\u2192{SEARCH['destination']} (max {max_dur}h)"
            print_drift_pair_table(all_results, title=label, filter_note=filter_note)
            merged = merge_cabins(all_results)
            print_drift_summary(merged)
            if args.recommend:
                combos = find_best_mixed(all_results, top_n=args.recommend)
                print_mixed_table(combos)
            if args.urls:
                print_booking_links(all_results)
        if aborted is not None:
            _log_error(_abort_message(aborted))
            sys.exit(1)
        if not all_results:
            return
    elif in_scan_mode:
        scan_ob = args.ob is not None or (args.scan and args.rb is None)
        scan_rb = args.rb is not None

        all_results = []
        aborted = None
        try:
            if scan_ob:
                log.rule("[bold cyan]Scanning outbound dates[/bold cyan]")
                top = scan_outbound(headers, cookies, args, max_price, weight, max_dur, top_n, raw=True)
                all_results.extend(top)

            if scan_rb:
                log.rule("[bold cyan]Scanning return dates[/bold cyan]")
                top = scan_return(headers, cookies, args, max_price, weight, max_dur, top_n, raw=True)
                all_results.extend(top)
        except ScanAborted as e:
            all_results.extend(e.results)
            aborted = e

        if aborted is not None and aborted.reason == "edge":
            _record_block_for_abort(aborted)

        if all_results:
            if not args.nofilter:
                all_results = filter_results(all_results, max_price, max_dur)
            all_results = score_results(all_results, weight)[:top_n]

            label = (
                f"Scan results"
                f" (max {max_dur}h, max price: {max_price})"
            )
            display(all_results, title=label)
        elif aborted is None:
            log.print("[yellow]No results after scan + filtering.[/yellow]")
            return

        if aborted is not None:
            _log_error(_abort_message(aborted))
            sys.exit(1)
    else:
        # --- Outbound call ---
        log.print(
            f"[bold]Outbound:[/bold] {SEARCH['origin']} \u2192 {SEARCH['destination']} on {SEARCH['departure_date']}"
        )
        try:
            ob_data, ob_status, ob_err = _fetch_with_retries(
                headers,
                cookies,
                SEARCH["origin"],
                SEARCH["destination"],
                SEARCH["departure_date"],
                referer=build_referer(
                    SEARCH["origin"],
                    SEARCH["destination"],
                    SEARCH["departure_date"],
                    SEARCH["return_date"],
                ),
            )
        except (EdgeBlock, AppAuthError, RateLimited, BudgetExceeded) as e:
            aborted = ScanAborted([], _abort_reason(e), e)
            _record_block_for_abort(aborted)
            _log_error(_abort_message(aborted))
            sys.exit(1)
        except RequestFailed as e:
            _log_error(f"Outbound: {e}")
            sys.exit(1)
        if ob_data is None:
            _log_error(f"Outbound HTTP {ob_status}: {ob_err}")
            sys.exit(1)
        ob_results = parse_results(ob_data, max_dur)
        convert_prices(ob_results)
        for r in ob_results:
            r["_dep_date"] = SEARCH["departure_date"]
            r["_ret_date"] = SEARCH["return_date"]
        ob_results = filter_dirt_cheap(ob_results)
        if not args.nofilter:
            ob_results = filter_results(ob_results, max_price, None)

        # --- Return call ---
        log.print(
            f"[bold]Return:[/bold] {SEARCH['destination']} \u2192 {SEARCH['origin']} on {SEARCH['return_date']}"
        )
        try:
            rb_data, rb_status, rb_err = _fetch_with_retries(
                headers,
                cookies,
                SEARCH["destination"],
                SEARCH["origin"],
                SEARCH["return_date"],
                referer=build_referer(
                    SEARCH["origin"],
                    SEARCH["destination"],
                    SEARCH["departure_date"],
                    SEARCH["return_date"],
                ),
            )
        except (EdgeBlock, AppAuthError, RateLimited, BudgetExceeded) as e:
            update_price_map(ob_results)
            aborted = ScanAborted([], _abort_reason(e), e)
            _record_block_for_abort(aborted)
            display_outbound_partial(ob_results)
            _log_error(_abort_message(aborted))
            sys.exit(1)
        except RequestFailed as e:
            display_outbound_partial(ob_results)
            _log_error(f"Return: {e}")
            sys.exit(1)
        if rb_data is None:
            display_outbound_partial(ob_results)
            _log_error(f"Return HTTP {rb_status}: {rb_err}")
            sys.exit(1)
        rb_results = parse_results(rb_data, max_dur)
        convert_prices(rb_results)
        for r in rb_results:
            r["_dep_date"] = SEARCH["departure_date"]
            r["_ret_date"] = SEARCH["return_date"]
        rb_results = filter_dirt_cheap(rb_results)
        if not args.nofilter:
            rb_results = filter_results(rb_results, max_price, None)
        update_price_map(ob_results + rb_results)

        ob_label = f"Outbound \u2014  {SEARCH['origin']}\u2192{SEARCH['destination']}  {SEARCH['departure_date']}"
        rb_label = f"Return \u2014  {SEARCH['destination']}\u2192{SEARCH['origin']}  {SEARCH['return_date']}"
        if args.json:
            combined = {"outbound": merge_cabins(ob_results), "return": merge_cabins(rb_results)}
            print_json_combined(combined)
        else:
            ob_merged = merge_cabins(ob_results)
            print_compare_table(ob_merged, title=ob_label, filter_note=filter_note)
            rb_merged = merge_cabins(rb_results)
            print_compare_table(rb_merged, title=rb_label, filter_note=filter_note)

        if args.urls:
            print_booking_links(ob_results + rb_results)


if __name__ == "__main__":
    main()
