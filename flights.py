import json
import os
import sys
import time
import argparse
from argparse import ArgumentParser
from datetime import datetime, timedelta

import requests
from rich.box import SIMPLE_HEAVY
from rich.console import Console
from rich.table import Table

# ============================================================
# SEARCH PARAMETERS
# ============================================================

SEARCH = {
    "origin": "OSL",
    "destination": "BKK",
    "departure_date": "2026-08-17",
    "return_date": "2026-09-04",
    "cabin": "ECONOMY",
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
    "highlight_fares": ["ECONOMY_CLASSIC", "BUSINESS_CLASSIC"],
    "avoid_airports": ["LHR", "LGW", "HEL", "KUL"],
    "max_stops": 1,
}

ALWAYS_EXCLUDED = {"ECONOMY_LITE", "FIRST_ELITE"}

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
    "delay_seconds": 20,
    "max_duration_hours": 25,
    "max_price": {"ECONOMY": 9000, "BUSINESS": 21000},
    "weight": "price",
    "top_n": 5,
    "min_trip_days": 8,
}

# ============================================================
# CONFIG FILE PATH
# ============================================================

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
LAYOUTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "layouts.json")
CURRENCY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "currency.json")
OFFERS_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "offers_cache.json")
PRICE_MAP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "price_map.json")
BAD_PATTERNS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bad_patterns.json")

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


def parse_pasted_headers(text):
    headers = {}
    cookies = {}
    for line in text.strip().splitlines():
        line = line.strip()
        if ": " not in line:
            continue
        key, value = line.split(": ", 1)
        key = key.strip()
        value = value.strip()
        if key == "Cookie":
            for part in value.split(";"):
                if "=" in part:
                    ck, cv = part.split("=", 1)
                    cookies[ck.strip()] = cv.strip()
        elif key.lower() in (
            "session-id",
            "x-assigneddeviceid",
            "nbx_fs_api_key",
            "request-id",
            "traceparent",
            "qr-lang",
        ):
            headers[key] = value
    headers.setdefault(
        "User-Agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:152.0) Gecko/20100101 Firefox/152.0",
    )
    headers.setdefault("Accept", "application/json, text/plain, */*")
    headers.setdefault("Content-Type", "application/json")
    headers.setdefault("Origin", "https://www.qatarairways.com")
    headers.setdefault("Referer", "https://www.qatarairways.com/")
    headers.setdefault("qr-lang", "en")
    return headers, cookies


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
    headers, cookies = parse_pasted_headers(text)
    if "session-id" not in headers:
        log.print("[red]Could not find 'session-id' in pasted headers.[/red]")
        sys.exit(1)
    cfg = {"headers": headers, "cookies": cookies}
    save_config(cfg)
    log.print(
        f"[green]Extracted: session-id={headers.get('session-id','?')[:20]}... "
        f"device-id={headers.get('X-AssignedDeviceID','?')[:20]}...[/green]"
    )
    return cfg


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
    if not os.path.exists(CURRENCY_FILE):
        return {}
    with open(CURRENCY_FILE) as f:
        return json.load(f)


def save_currency_rates(rates):
    with open(CURRENCY_FILE, "w") as f:
        json.dump(rates, f, indent=2)


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
        "cabinClass": SEARCH["cabin"],
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

    existing = {}
    if os.path.exists(PRICE_MAP_FILE):
        try:
            with open(PRICE_MAP_FILE) as f:
                existing = json.load(f)
        except (json.JSONDecodeError, OSError):
            existing = {}

    for r in results:
        date = r.get("departure_date")
        fare = r.get("fare")
        segs = r.get("segments", [])
        if not date or not fare or not segs:
            continue

        stops = r.get("stops", 0)
        if stops > 1:
            continue

        direction = f"{r['origin']}_{r['destination']}"
        day = datetime.strptime(date, "%Y-%m-%d").strftime("%a")
        price = r.get("price")
        if price is None:
            continue

        first = segs[0]
        last = segs[-1]
        flight_num = first.get("flight", "?")
        dep_time = first.get("departure", "")[11:16] if first.get("departure") else "?"
        arr_time = last.get("arrival", "")[11:16] if last.get("arrival") else "?"
        transfer = compute_transfer_minutes(segs)
        direct = stops == 1

        # generate a stable key for this itinerary pattern
        pat_key = f"{flight_num}|{dep_time}|{stops}|{transfer}"

        existing.setdefault(direction, {})
        existing[direction].setdefault(day, {})
        existing[direction][day].setdefault(pat_key, {
            "flight": flight_num,
            "departure": dep_time,
            "arrival": arr_time,
            "stops": stops,
            "transfer_minutes": transfer,
            "direct": direct,
            "fares": {},
        })
        entry = existing[direction][day][pat_key]

        # update arrival if we see a different one (e.g. different second leg)
        if arr_time != entry["arrival"]:
            entry["arrival"] = arr_time

        if fare not in entry["fares"]:
            entry["fares"][fare] = {"min": price, "max": price, "total": price, "samples": 1}
        else:
            fdata = entry["fares"][fare]
            if price < fdata["min"]:
                fdata["min"] = price
            if price > fdata["max"]:
                fdata["max"] = price
            fdata["total"] += price
            fdata["samples"] += 1

    with open(PRICE_MAP_FILE, "w") as f:
        json.dump(existing, f, indent=2)


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

    with open(PRICE_MAP_FILE) as f:
        data = json.load(f)

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
_filter_bad = False


def set_nocache(val):
    global _nocache
    _nocache = val


def set_filter_bad(val):
    global _filter_bad
    _filter_bad = val


def load_offers_cache():
    if not os.path.exists(OFFERS_CACHE_FILE):
        return {}
    with open(OFFERS_CACHE_FILE) as f:
        return json.load(f)


def save_offers_cache(cache):
    with open(OFFERS_CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)


# ============================================================
# REQUEST
# ============================================================

API_URL = "https://www.qatarairways.com/dapi/public/bff/web/flight-search/flight-offers"


def _is_cache_hit(origin, destination, date_str):
    if _nocache:
        return False
    key = f"{origin}_{destination}_{date_str}"
    cache = load_offers_cache()
    entry = cache.get(key)
    return bool(entry and entry.get("date") == datetime.now().strftime("%Y-%m-%d"))


def fetch_flights(headers, cookies, origin, destination, departure_date):
    cache_key = f"{origin}_{destination}_{departure_date}"
    today = datetime.now().strftime("%Y-%m-%d")

    if not _nocache:
        cache = load_offers_cache()
        entry = cache.get(cache_key)
        if entry and entry.get("date") == today:
            log.print(f"[dim]Cached {cache_key}[/dim]")
            return entry["data"], 200, None

    payload = build_payload(origin, destination, departure_date)
    resp = requests.post(
        API_URL,
        headers=headers,
        cookies=cookies,
        json=payload,
        timeout=30,
    )
    if resp.status_code != 200:
        return None, resp.status_code, resp.text[:500]

    data = resp.json()
    cache[cache_key] = {"date": today, "data": data}
    save_offers_cache(cache)
    return data, resp.status_code, None


def _fetch_and_parse(headers, cookies, origin, destination, date_str, dep_date, ret_date, direction, max_dur):
    data, status, err = fetch_flights(headers, cookies, origin, destination, date_str)
    if data is None:
        log.print(f"[red]  HTTP {status}: {err}[/red]")
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
    p.add_argument("--delay", type=int, default=None, metavar="SEC", help="Delay between scan requests (default 10 for drift, 20 for scan)")
    p.add_argument("--price-map", action="store_true", help="Show price map (day-of-week price trends from collected data)")
    p.add_argument("--recommend", type=int, default=None, const=10, nargs="?", metavar="N", help="Show top N mixed-cabin combos (default 10)")
    p.add_argument("--filter-bad", action="store_true", help="Skip API calls for known-bad day-of-week + direction combos")
    p.add_argument("--flag-bad", type=str, default=None, nargs=3, metavar=("DIRECTION", "DAY", "REASON"), help="Flag a bad pattern: OSL_BKK Mon 'expensive'")
    p.add_argument("--all-fares", action="store_true", help="Include BUSINESS_LITE, BUSINESS_ELITE (excluded by default; ECONOMY_LITE/FIRST always hidden)")
    p.add_argument("--origin", "-o", type=str, default=None, metavar="CODE", help="Origin airport IATA (default: OSL)")
    p.add_argument("--destination", "-d", type=str, default=None, metavar="CODE", help="Destination airport IATA (default: BKK)")
    p.add_argument("--depart", type=str, default=None, metavar="YYYY-MM-DD", help="Departure date (default: 2026-08-17)")
    p.add_argument("--ret", type=str, default=None, metavar="YYYY-MM-DD", help="Return date (default: 2026-09-04)")
    p.add_argument("--cabin", type=str, default=None, choices=["ECONOMY", "BUSINESS"], help="Cabin class (default: ECONOMY)")
    return p.parse_args(argv)


# ============================================================
# SCAN HELPERS
# ============================================================


def scan_outbound(headers, cookies, args, max_price, weight, max_dur, top_n, raw=False):
    base = datetime.strptime(SEARCH["departure_date"], "%Y-%m-%d").date()
    ret = datetime.strptime(SEARCH["return_date"], "%Y-%m-%d").date()
    before = args.ob if args.ob is not None else SCAN["days_before_outbound"]
    after = args.ob if args.ob is not None else SCAN["days_after_outbound"]
    min_trip = args.min_trip if args.min_trip is not None else SCAN["min_trip_days"]

    all_results = []
    dates_to_check = [base + timedelta(days=i) for i in range(-before, after + 1)]

    if _filter_bad:
        dates_to_check = [d for d in dates_to_check if not
            is_bad_day(f"{SEARCH['origin']}_{SEARCH['destination']}", d.isoformat())
        ]

    total = len(dates_to_check)

    for idx, d in enumerate(dates_to_check):
        if (ret - d).days < min_trip:
            log.print(f"[dim]Skipping {d}: trip={(ret-d).days}d < min {min_trip}d[/dim]")
            continue
        ob_str = d.isoformat()
        log.print(f"[cyan]Outbound request {idx+1}/{total}: {d}[/cyan]")
        results = _fetch_and_parse(headers, cookies, SEARCH["origin"], SEARCH["destination"], ob_str, ob_str, ret.isoformat(), "OB", max_dur)
        all_results.extend(results)
        if idx < total - 1:
            next_d = dates_to_check[idx + 1].isoformat()
            if not _is_cache_hit(SEARCH["origin"], SEARCH["destination"], next_d):
                delay = args.delay if args.delay is not None else SCAN["delay_seconds"]
                log.print(f"  [dim]Waiting {delay}s...[/dim]")
                time.sleep(delay)

    update_price_map(all_results)
    all_results = filter_dirt_cheap(all_results)

    if raw:
        return all_results
    filtered = filter_results(all_results, max_price, max_dur)
    if not filtered:
        log.print("[yellow]No results passed filters (max price/duration).[/yellow]")
        return []
    scored = score_results(filtered, weight)
    return scored[:top_n]


def scan_return(headers, cookies, args, max_price, weight, max_dur, top_n, raw=False):
    dep = datetime.strptime(SEARCH["departure_date"], "%Y-%m-%d").date()
    base = datetime.strptime(SEARCH["return_date"], "%Y-%m-%d").date()
    before = args.rb if args.rb is not None else SCAN["days_before_return"]
    after = args.rb if args.rb is not None else SCAN["days_after_return"]
    min_trip = args.min_trip if args.min_trip is not None else SCAN["min_trip_days"]

    all_results = []
    dates_to_check = [base + timedelta(days=i) for i in range(-before, after + 1)]

    if _filter_bad:
        dates_to_check = [d for d in dates_to_check if not
            is_bad_day(f"{SEARCH['destination']}_{SEARCH['origin']}", d.isoformat())
        ]

    total = len(dates_to_check)

    for idx, d in enumerate(dates_to_check):
        if (d - dep).days < min_trip:
            log.print(f"[dim]Skipping {d}: trip={(d-dep).days}d < min {min_trip}d[/dim]")
            continue
        rb_str = d.isoformat()
        log.print(f"[cyan]Return request {idx+1}/{total}: {d}[/cyan]")
        results = _fetch_and_parse(headers, cookies, SEARCH["destination"], SEARCH["origin"], rb_str, dep.isoformat(), rb_str, "RB", max_dur)
        all_results.extend(results)
        if idx < total - 1:
            next_d = dates_to_check[idx + 1].isoformat()
            if not _is_cache_hit(SEARCH["destination"], SEARCH["origin"], next_d):
                delay = args.delay if args.delay is not None else SCAN["delay_seconds"]
                log.print(f"  [dim]Waiting {delay}s...[/dim]")
                time.sleep(delay)

    update_price_map(all_results)
    all_results = filter_dirt_cheap(all_results)

    if raw:
        return all_results
    filtered = filter_results(all_results, max_price, max_dur)
    if not filtered:
        log.print("[yellow]No results passed filters (max price/duration).[/yellow]")
        return []
    scored = score_results(filtered, weight)
    return scored[:top_n]


# ============================================================
# DRIFT SCAN (shift both dates together, preserving trip length)
# ============================================================


def drift_scan(headers, cookies, args, max_price, max_dur):
    base_ob = datetime.strptime(SEARCH["departure_date"], "%Y-%m-%d").date()
    base_rb = datetime.strptime(SEARCH["return_date"], "%Y-%m-%d").date()
    trip_days = (base_rb - base_ob).days
    drift = args.drift
    delay = args.delay if args.delay is not None else 10

    all_results = []
    offsets = list(range(-drift, drift + 1))

    if _filter_bad:
        offsets = [o for o in offsets if not (
            is_bad_day(f"{SEARCH['origin']}_{SEARCH['destination']}", (base_ob + timedelta(days=o)).isoformat())
            or is_bad_day(f"{SEARCH['destination']}_{SEARCH['origin']}", (base_rb + timedelta(days=o)).isoformat())
        )]
        if len(offsets) < drift * 2 + 1:
            log.print(f"[dim]Filtered to {len(offsets)}/{drift*2+1} date pairs (bad days removed)[/dim]")

    total = len(offsets)

    for idx, offset in enumerate(offsets):
        ob_date = base_ob + timedelta(days=offset)
        rb_date = base_rb + timedelta(days=offset)
        ob_str = ob_date.isoformat()
        rb_str = rb_date.isoformat()
        log.print(f"[cyan]Drift {idx+1}/{total}: {ob_date} \u2192 {rb_date} ({trip_days}d trip)[/cyan]")

        results = _fetch_and_parse(headers, cookies, SEARCH["origin"], SEARCH["destination"], ob_str, ob_str, rb_str, "OB", max_dur)
        all_results.extend(results)

        if not _is_cache_hit(SEARCH["destination"], SEARCH["origin"], rb_str):
            time.sleep(delay)

        results = _fetch_and_parse(headers, cookies, SEARCH["destination"], SEARCH["origin"], rb_str, ob_str, rb_str, "RB", max_dur)
        all_results.extend(results)

        if idx < total - 1:
            next_ob = (base_ob + timedelta(days=offsets[idx + 1])).isoformat()
            next_rb = (base_rb + timedelta(days=offsets[idx + 1])).isoformat()
            ob_hit = _is_cache_hit(SEARCH["origin"], SEARCH["destination"], next_ob)
            rb_hit = _is_cache_hit(SEARCH["destination"], SEARCH["origin"], next_rb)
            if not (ob_hit and rb_hit):
                time.sleep(delay)

    update_price_map(all_results)
    all_results = filter_dirt_cheap(all_results)

    if not all_results:
        return []

    if args.nofilter:
        return all_results
    filtered = filter_results(all_results, max_price, max_dur)
    if not filtered:
        log.print("[yellow]No results passed filters (max price/duration).[/yellow]")
        return []
    return filtered


def drift_scan_return(headers, cookies, args, max_price, max_dur):
    base_ob = datetime.strptime(SEARCH["departure_date"], "%Y-%m-%d").date()
    base_rb = datetime.strptime(SEARCH["return_date"], "%Y-%m-%d").date()
    drift = args.drift_ret
    delay = args.delay if args.delay is not None else 10

    all_results = []
    offsets = list(range(-drift, drift + 1))

    if _filter_bad:
        offsets = [o for o in offsets if not
            is_bad_day(f"{SEARCH['destination']}_{SEARCH['origin']}", (base_rb + timedelta(days=o)).isoformat())
        ]
        if len(offsets) < drift * 2 + 1:
            log.print(f"[dim]Filtered to {len(offsets)}/{drift*2+1} return dates (bad days removed)[/dim]")

    total = len(offsets)

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

        if not _is_cache_hit(SEARCH["destination"], SEARCH["origin"], rb_str):
            time.sleep(delay)

        results = _fetch_and_parse(headers, cookies, SEARCH["destination"], SEARCH["origin"], rb_str, ob_str, rb_str, "RB", max_dur)
        all_results.extend(results)

        if idx < total - 1:
            next_rb = (base_rb + timedelta(days=offsets[idx + 1])).isoformat()
            rb_hit = _is_cache_hit(SEARCH["destination"], SEARCH["origin"], next_rb)
            if not rb_hit:
                time.sleep(delay)

    update_price_map(all_results)
    all_results = filter_dirt_cheap(all_results)

    if not all_results:
        return []

    if args.nofilter:
        return all_results
    filtered = filter_results(all_results, max_price, max_dur)
    if not filtered:
        log.print("[yellow]No results passed filters (max price/duration).[/yellow]")
        return []
    return filtered


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
# BAD-PATTERN FILTER (skip known-bad days before API calls)
# ============================================================


def load_bad_patterns():
    if not os.path.exists(BAD_PATTERNS_FILE):
        return {}
    with open(BAD_PATTERNS_FILE) as f:
        return json.load(f)


def save_bad_patterns(patterns):
    with open(BAD_PATTERNS_FILE, "w") as f:
        json.dump(patterns, f, indent=2)


def is_bad_day(direction, date_str):
    day = datetime.strptime(date_str, "%Y-%m-%d").strftime("%a")
    patterns = load_bad_patterns()
    return day in patterns.get(direction, {})


def flag_bad_day(direction, day, reason):
    patterns = load_bad_patterns()
    patterns.setdefault(direction, {})[day] = reason
    save_bad_patterns(patterns)
    log.print(f"[yellow]Flagged {direction} {day}: {reason}[/yellow]")


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
            url = booking_url(origin, dest, dep, ret, SEARCH.get("cabin", "ECONOMY"))
            console.print(f"  {dep} \u2192 {ret}  {url}")


# ============================================================
# MAIN
# ============================================================


def main():
    args = parse_args()

    # -- flag-bad: early-return curation command
    if args.flag_bad:
        flag_bad_day(args.flag_bad[0], args.flag_bad[1], args.flag_bad[2])
        return

    # -- filter-bad: activate bad-day pre-filter
    if args.filter_bad:
        set_filter_bad(True)

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

    headers = cfg["headers"]
    cookies = cfg.get("cookies", {})

    set_nocache(args.nocache)

    # CLI overrides for search params
    if args.origin:
        SEARCH["origin"] = args.origin.upper()
    if args.destination:
        SEARCH["destination"] = args.destination.upper()
    if args.depart:
        SEARCH["departure_date"] = args.depart
    if args.ret:
        SEARCH["return_date"] = args.ret
    if args.cabin:
        SEARCH["cabin"] = args.cabin.upper()

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
        f"  [bold]Cabin:[/bold] {SEARCH['cabin']}"
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

    if args.drift is not None:
        log.rule("[bold cyan]Drift scan[/bold cyan]")
        all_results = drift_scan(headers, cookies, args, max_price, max_dur)
        if not all_results:
            return
        label = f"Drift scan \u2014 {SEARCH['origin']}\u2192{SEARCH['destination']} (max {max_dur}h)"
        print_drift_pair_table(all_results, title=label, filter_note=filter_note)
        merged = merge_cabins(all_results)
        print_drift_summary(merged)
        if args.recommend:
            combos = find_best_mixed(all_results, top_n=args.recommend)
            print_mixed_table(combos)
        if args.urls:
            print_booking_links(all_results)
    elif args.drift_ret is not None:
        log.rule("[bold cyan]Drift-Ret scan[/bold cyan]")
        all_results = drift_scan_return(headers, cookies, args, max_price, max_dur)
        if not all_results:
            return
        label = f"Drift-Ret \u2014 {SEARCH['origin']}\u2192{SEARCH['destination']} (max {max_dur}h)"
        print_drift_pair_table(all_results, title=label, filter_note=filter_note)
        merged = merge_cabins(all_results)
        print_drift_summary(merged)
        if args.recommend:
            combos = find_best_mixed(all_results, top_n=args.recommend)
            print_mixed_table(combos)
        if args.urls:
            print_booking_links(all_results)
    elif in_scan_mode:
        scan_ob = args.ob is not None or (args.scan and args.rb is None)
        scan_rb = args.rb is not None

        all_results = []

        if scan_ob:
            log.rule("[bold cyan]Scanning outbound dates[/bold cyan]")
            top = scan_outbound(headers, cookies, args, max_price, weight, max_dur, top_n, raw=True)
            all_results.extend(top)

        if scan_rb:
            log.rule("[bold cyan]Scanning return dates[/bold cyan]")
            top = scan_return(headers, cookies, args, max_price, weight, max_dur, top_n, raw=True)
            all_results.extend(top)

        if not all_results:
            log.print("[yellow]No results after scan + filtering.[/yellow]")
            return

        if not args.nofilter:
            all_results = filter_results(all_results, max_price, max_dur)
        all_results = score_results(all_results, weight)[:top_n]

        label = (
            f"Scan results"
            f" (max {max_dur}h, max price: {max_price})"
        )
        display(all_results, title=label)
    else:
        # --- Outbound call ---
        log.print(
            f"[bold]Outbound:[/bold] {SEARCH['origin']} \u2192 {SEARCH['destination']} on {SEARCH['departure_date']}"
        )
        ob_data, ob_status, ob_err = fetch_flights(
            headers, cookies, SEARCH["origin"], SEARCH["destination"], SEARCH["departure_date"]
        )
        if ob_data is None:
            log.print(f"[red]Outbound HTTP {ob_status}: {ob_err}[/red]")
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
        rb_data, rb_status, rb_err = fetch_flights(
            headers, cookies, SEARCH["destination"], SEARCH["origin"], SEARCH["return_date"]
        )
        if rb_data is None:
            log.print(f"[red]Return HTTP {rb_status}: {rb_err}[/red]")
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
