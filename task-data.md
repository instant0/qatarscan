# task-data.md — shared reference for the `flight-offers` reliability fix

This is the single source of truth for data, schemas, policies, and conventions used by the
task documents in `tasks/`. Each task links here instead of repeating shared details.

- High-level design: `TASK.md`
- Deferred calendar research: `TASK_FARE_CALENDAR.md`

---

## 0. Task index

| ID | Required | Status | Depends on | Title |
|----|----------|--------|------------|-------|
| T01 | Yes | **Done** | — | Hotfix `--nocache` `UnboundLocalError` |
| T02 | Yes | **Done** | — | Paste parsing & config hardening |
| T03 | Yes | **Done** | T02 | HTTP client: Session, cookies, trace pair, headers, Referer |
| T04 | Yes | **Done** | — | Persisted rate limiter & jittered pacing |
| T05 | Yes | **Done** (2026-10-26) | T03, T04 | Scan integration: abort-on-403, partial results, UX |
| T06 | Yes | Pending | T01–T05, T09–T14 | Verification & rate calibration |
| T07 | Optional | Pending | T03 | TLS impersonation (`curl_cffi`) |
| T08 | Optional | Pending | T02 | Playwright warm-up (cookie harvesting) |
| T09 | Yes | Pending | T03, T05, T10 | Response parsing & FX conversion hardening |
| T10 | Yes | **Done** (2026-10-26) | T01, T05 | Durable JSON state persistence |
| T11 | Yes | Pending | T04, T05 | Rate limiter and cooldown hardening |
| T12 | Yes | Pending | T03, T05 | CLI, output, and local-mode correctness |
| T13 | Yes | Pending | T02, T03, T10 | Request-header, config, and cookie hardening |
| T14 | Yes | Pending | T09–T13 | Documentation and test-contract cleanup |

---

## 1. Background: the 403 and the two gates

The app receives an Akamai edge page, not a QR auth error:

```
403 <HTML>...Access Denied... Reference #18... errors.edgesuite.net/...
```

Two independent gates:

1. **Identity validity** — a stale/burned/incomplete paste blocks the *first* live call.
2. **Call budget** — a valid identity survives a few live calls, then the session is
   throttled. Observed: 4 live calls at 20s spacing, then 403.

Handle both: warm paste + cookie persistence (gate 1); jittered pacing, cross-run throttle,
abort-on-403 (gate 2).

---

## 2. Endpoint and request (`flight-offers`)

```
POST https://www.qatarairways.com/dapi/public/bff/web/flight-search/flight-offers
```

One itinerary per call; outbound and return are **separate** calls.

```json
{
  "channel": "WEB_DESKTOP",
  "itineraries": [
    {"origin": "OSL", "destination": "BKK", "departureDate": "2026-12-29", "isRequested": true}
  ],
  "cabinClass": "ECONOMY",
  "ignoreInvalidPromoCode": true,
  "passengers": [{"type": "ADT", "count": 1}]
}
```

- `cabinClass: "ECONOMY"` does not filter the response; economy and business fares return.
- Two `isRequested: true` itineraries returns empty.
- No multi-date support. One response = one direction/date.
- Request floor: **1 call per direction/date**, **2 per date pair**.

---

## 3. Response fields used

- `flightOffers[]` → `segments[]` + `fareOffers[]`.
- Segment: `flightNumber`, `vehicle.code`/`shortName`, `departure.dateTime`,
  `arrival.dateTime`, `duration`, `connectionTime`, `qSuiteEquipped`.
- Fare offer: `boundId`, `price.total`, `currencyCode`, `fareFamilyCode`, `cabinType`,
  `availableSeats`, `isLowestFare`.
- Use `price.total` (taxes included). Convert non-NOK with the existing FX helper.
- `boundId` is not needed by the current independent outbound/return flow.

---

## 4. Header policy

### Required app/auth headers
- `Content-Type: application/json`
- `nbx_fs_api_key`
- `session-id`
- `X-AssignedDeviceID`
- `Origin: https://www.qatarairways.com`
- `Referer` (date-accurate; see T03)
- `qr-lang: en`

### Browser-like headers (preserve from paste when present)
- `User-Agent` (must match the pasted session/TLS profile)
- `Accept: application/json, text/plain, */*`
- `Accept-Language: en`
- `Sec-GPC: 1`
- `Sec-Fetch-Dest: empty`, `Sec-Fetch-Mode: cors`, `Sec-Fetch-Site: same-origin`
- `Pragma: no-cache`, `Cache-Control: no-cache`

### Do not copy / do not send
- Do **not** copy `Host` or `Content-Length`; `requests` computes them.
- Do **not** send `Accept-Encoding: br, zstd` with plain `requests` unless
  `brotli`/`zstandard` are installed. Keep the library default (or use `curl_cffi`).
- Do **not** copy analytics cookies (see §5).

### Rotation
| Item | Policy |
|---|---|
| `Request-Id`, `traceparent` | New shared trace/span pair on **every** request |
| Payload `departureDate`, `Referer` dates | Match the actual requested date |
| Any real timestamp field | Generate from the request args + current clock |
| `User-Agent`, `session-id`, `X-AssignedDeviceID`, cookies | Stable per run; change only with a fresh browser session |

Trace format (both share the same IDs):
```
trace-id = secrets.token_hex(16)   # 32 hex
span-id  = secrets.token_hex(8)    # 16 hex
Request-Id:  |{trace-id}.{span-id}
traceparent: 00-{trace-id}-{span-id}-01
```

---

## 5. Cookie policy

Use an **allowlist**, not a blocklist.

Keep/echo:
- Akamai/sensor: `_abck`, `bm_sz`, `bm_sv`, `ak_bmsc`
- Load-balancer: `BIGipServer*`
- Session/gateway: `AKA_A2`, `TS01753a24`, `TS01163f70` (keep all `TS0*`)

Drop: analytics/marketing (`_ga*`, `AMCV*`, `OptanonConsent`, `s_*`, `mbox`, `_fbp`,
`_gcl_au`, `kndctr_*`, `FPID`, `FPLC`, `ai_user`, `ai_session`, etc.).

Persist `Set-Cookie` updates (notably `BIGipServer*`, refreshed `_abck`) via a
`requests.Session`; write the jar back to `config.json` after successful calls.

---

## 6. Pacing and rate policy

- Base spacing **45s**, jittered per call to ~40–55s (`random.randint(40, 55)`).
- Only network POSTs count as live calls; cache hits are free.
- No hard per-run cap initially. Optional `--budget` safety cap after calibration.
- Cross-run throttle persisted in `.rate_state.json`.
- On first 403: abort the run, set `blocked_until` (start 15–30 min), preserve results.
- Do not retry a 403 in a loop. Do not run via VPN/datacenter egress.

---

## 7. Persisted state and files

| File | Purpose | Notes |
|---|---|---|
| `config.json` | headers, cookies, saved_at, user_agent, source_endpoint, last_abck_state | gitignored |
| `offers_cache.json` | per-date API responses | current day |
| `cache_archive.json` | stale responses | 1-year retention |
| `price_map.json` | day-of-week/fare aggregates | existing |
| `currency.json` | non-NOK → NOK FX cache | cache, not a feature switch |
| `.rate_state.json` | live-call timestamps + `blocked_until` | new; gitignored |
| `.rate_state.lock` | rate-state lock file | new; gitignored |
| `.data_state.lock` | JSON cache/archive/price-map/currency lock | new; gitignored |
| `*.corrupt-*` | quarantined corrupt JSON state | gitignored |

`config.json` schema:

```json
{
  "headers": { "...": "..." },
  "cookies": { "...": "..." },
  "saved_at": "ISO-8601",
  "user_agent": "Mozilla/5.0 (... rv:158.0) ... Firefox/158.0",
  "impersonate": "firefox",
  "source_endpoint": "flight-offers",
  "last_abck_state": "valid|invalid|unknown"
}
```

`.rate_state.json` schema:

```json
{
  "live_calls": ["2026-10-25T18:00:00+00:00"],
  "blocked_until": "2026-10-25T18:30:00+00:00"
}
```

---

## 8. Error taxonomy

| Condition | Classification | Action |
|---|---|---|
| 403 + HTML / `Access Denied` / `edgesuite` | Akamai edge block | Abort run, set cooldown, friendly message |
| 401/403 + JSON | App auth/session error | Abort, suggest `--paste`, do not set rate cooldown |
| 5xx / timeout | Transient | Fail the call; optional single retry |
| 200 + non-JSON / malformed payload | Transient parse failure (T09) | Skip in scans; non-scan exits 1; no cooldown, no traceback |
| Corrupt local JSON state | Local state error (T10/T11) | Quarantine + warn; limiter fails closed |
| 200 + JSON | Success | Parse, cache, persist cookies |

---

## 9. Definitions

- **Live call**: an actual network POST. Cache hits are not live calls.
- **Date pair**: outbound date + return date. Costs 2 live calls.
- **Drift ±N**: 2×(2N+1) date pairs, hence 2×(2N+1) live calls.
- **Identity**: the pasted headers + cookie jar (session). Validated by a warm paste.
- **Warm paste**: headers/cookies copied from a browser session that is currently active.
- **Block**: an Akamai edge denial (HTML), not an app JSON error.

---

## 10. Code map

Locate functions with `grep -n 'def <name>' flights.py`; line numbers drift as tasks land.

| Function | Responsibility |
|---|---|
| `load_config` / `save_config` | config.json read/write |
| `parse_pasted_headers` | paste parser |
| `prompt_paste_headers` | interactive paste |
| `validate_config` | saved-config checks |
| `build_payload` | flight-offers payload |
| `parse_results` | response → result rows |
| `convert_prices` | FX conversion |
| `update_price_map` | price-map aggregation |
| `_load_archive_prices` | archive aggregation |
| `print_price_calendar` | local calendar view |
| `print_price_trends` | trend view |
| `filter_dirt_cheap` / `score_results` | filtering/scoring |
| `fetch_flights` | HTTP request + offers cache |
| `_fetch_with_retries` | transient-only retry wrapper |
| `_fetch_and_parse` | fetch + parse wrapper |
| `_scan_dates` | outbound/return scan loop |
| `scan_outbound` / `scan_return` | wrappers |
| `drift_scan` | drift both dates |
| `drift_scan_return` | drift return only |
| `ScanAborted` | typed partial-result abort |
| `_record_block_for_abort` | edge-only cooldown recording |
| `main` | CLI orchestration |
| `API_URL` | endpoint constant |

---

## 11. Test conventions

- Use stdlib `unittest`, new `tests/` directory.
- Run: `python -m unittest discover -s tests -v`
- Compile check: `python -m py_compile flights.py`
- Fake the clock and use temporary directories for state.
- Monkeypatch `requests.Session` / `HttpClient` for network tests.
- No live network calls in unit tests; live checks are manual/opt-in.
- Each task lists its own tests; T06 runs the full suite and calibration.

---

## 12. Related documents

- `TASK.md` — high-level design and history.
- `TASK_FARE_CALENDAR.md` — deferred `fare-calendars` research.
- `api.md` — original endpoint notes.
- `README.md` — user-facing flags.
