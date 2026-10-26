# TASK: Restore reliable `flight-offers` scanning (Akamai 403 mitigation)

Status: Phase 1 implemented through T05, T09, T10, T11 (2026-10-26); post-review remediation T12–T14 pending, then T06 verification
Owner: (unassigned)
Related: `flights.py`, `config.json`, `api.md`, `README.md`
Execution tasks: `tasks/` (index in `task-data.md` §0)
Deferred research: `TASK_FARE_CALENDAR.md`

## Revision history

- 2026-10-25 — initial draft from the 403 analysis.
- 2026-10-25 — separated the two failure gates (identity validity vs. call budget), added
  45s jittered spacing, rotation rules, accurate-date rules, abort-on-403, and header
  provenance.
- 2026-10-25 — QC pass: header cautions, 403 classification, rate-limiter details, extra
  tests, `--nocache` regression.
- 2026-10-25 — scope narrowed to detail-first `flight-offers`; `fare-calendars` research
  moved to `TASK_FARE_CALENDAR.md` (deferred).
- 2026-10-26 — T04 rate limiter shipped; T05 revised for unconditional abort, transient-only
  retries, and partial-result finalization.
- 2026-10-26 — T05 scan integration shipped; full suite passes (64 tests).
- 2026-10-26 — post-T05 code/doc review found parsing, persistence, limiter, output, config,
  and test-contract gaps. Added required remediation tasks T09–T14; T06 now depends on them.
  No runtime code changed in this review pass.
- 2026-10-26 — T10 durable JSON persistence shipped: atomic/locked writes, corruption
  quarantine, merge-on-save, and guarded read-only state views. Full suite passes (78 tests).
- 2026-10-26 — T09 response parsing and FX hardening shipped: non-JSON/malformed 200s are
  typed transient failures, parsing skips bad offers/fares/segments, and FX/cache entries are
  validated before use. Full suite passes (98 tests).

---

## 0. At a glance

**Scope**
- Full detail per date: all flight combinations and fares (the existing behavior).
- Endpoint: `flight-offers` only. One call = one direction + one date.
- A round-trip date pair = **2 calls**; that is the floor.
- Caching (`offers_cache.json`/archive) is the only safe way to avoid repeats across runs.

**Confirmed**
- The 403 is an Akamai edge block, not app authentication.
- Two gates: **identity validity** and **call budget**.
- A valid identity survived **4 live calls** at 20s, then blocked.
- One `flight-offers` response contains everything for **one** direction/date only.

**Decided**
- 45s base spacing + jitter; abort on the first 403; persisted cross-run throttle.
- Rotate `Request-Id`/`traceparent` every call; keep
  `User-Agent`/`session-id`/`X-AssignedDeviceID`/cookies stable per run.
- All dates/timestamps derived from the real request arguments + current clock.

**Known gaps**
- Is 45s actually sufficient? (Phase 0.5 calibration)
- Is the block session-scoped or IP/time-scoped?

**Execution order**
1. Fix the `--nocache` `UnboundLocalError`.
2. Phase 1.0: paste provenance validation.
3. Phase 1: Session/cookies, header coherence, trace pair rotation, 45s jitter,
   abort-on-403, persisted throttle.
4. Optional: `curl_cffi`; Playwright.

---

## 1. Summary

The scanner worked intermittently, then began returning:

```
HTTP 403: <HTML><HEAD><TITLE>Access Denied</TITLE>...</HEAD></HTML>
Reference #18.cd0a655f.1792949274.77119c3
https://errors.edgesuite.net/18.cd0a655f.1792949274.77119c3
```

This is **not** a QR authentication failure and **not** the documented ~24h session
expiry. It is **Akamai Bot Manager / edge denial**: the request is rejected at the CDN
before it reaches the QR BFF. The tool must behave like a real browser session and stop
hammering once denied.

The goal is to make the existing per-date `flight-offers` scanning reliable again. Full
detail (all flight combinations and fares) is required for every date, so each direction
needs its own call. Caching is the only way to avoid repeats.

### 1.1 Agreed decisions

- **Endpoint**: `flight-offers` only (one direction/date per call).
- **Spacing**: base **45s** between live calls, jittered per call (e.g. 45, 40, 50 →
  ~40–55s). Multi-minute runs are acceptable.
- **Rotation**: rotate `Request-Id`/`traceparent` every request; keep
  `session-id`/`X-AssignedDeviceID`/cookie jar stable per session.
- **Accuracy**: all dates/timestamps must be generated from the real request arguments and
  the current clock; never stale or hardcoded.
- **Abort on the first 403**; do not fire the remaining dates or retry in a loop.
- **Cross-run throttle**: persist live-call timestamps so back-to-back runs don't stack.

### 1.2 Request floor (detail-first)

- One direction + one date: **1 call**.
- Round-trip date pair: **2 calls** (outbound + return). The response is direction-specific,
  and two `isRequested: true` itineraries in one call returns empty.
- ±N drift: **2 × (2N+1)** calls (e.g. ±2 = 10).
- Caching is the only safe reducer; do not trade detail for fewer calls.

---

## 2. Symptom and reproduction

Command:

```
python flights.py --ob 2 --rb 2 --depart 2026-12-27 --ret 2027-01-06 \
  --origin OSL --destination BKK
```

Observed:

```
Outbound request 1/5: 2026-12-25  Cached ... → 8 offers
Outbound request 2/5: 2026-12-26  Cached ... → 8 offers
  Waiting 20s...
Outbound request 3/5: 2026-12-27  HTTP 403 (Akamai Access Denied)
  Waiting 20s...
Outbound request 4/5: 2026-12-28  HTTP 403
  Waiting 20s...
Outbound request 5/5: 2026-12-29  HTTP 403
```

Key observations:

- The two `Cached` dates were fetched successfully **earlier the same day**, so the
  session was valid at some point today.
- The **first live** request in this run was already blocked. This points to a
  **burned/stale identity** (or incomplete paste), not to the request payload.
- A later run (section 3.1) with a warm paste allowed several live calls before blocking.
  That second behaviour is the **call-budget/velocity** gate.
- There are two gates: (1) is the identity/session valid at all, and (2) how many live
  calls it can spend before Akamai throttles. Both must be handled.

---

## 3. Evidence from the browser captures

Captured `flight-offers` requests from a working browser session:

1. Outbound — `Content-Length: 305`
2. Return — `Content-Length: 370`; Referer includes `boundType=I&oBoundId=BC1-2-OFR-...`
3. Another return date — same shape

Facts extracted:

| Observation | Implication |
|---|---|
| `User-Agent: Firefox/158.0` in browser; app sends `Firefox/152.0` | UA does not match the cookies/TLS that produced them |
| `session-id` identical across all return-flow captures | `session-id` is per page-session, fine to keep stable per run |
| `X-AssignedDeviceID` identical across **all** captures | Device ID is stable; rotating it per request is abnormal |
| `Request-Id` and `traceparent` **change on every request** | These must be regenerated per request, never replayed |
| `_abck` appeared as both `...~0~...` and `...~-1~...` | Akamai sensor cookie is stateful and rotates; `~-1~` alone is not disqualifying |
| `BIGipServeroriginazqacwww11933911-dapi-https-pool` appears mid-session | Server issues `Set-Cookie`; the app currently discards it |
| Full `Sec-Fetch-*`, `Accept-Language`, `Pragma`, `Cache-Control`, `TE` sent by browser | App sends none of these |
| Referer includes exact `departing`/`returning` dates + bound info | App sends a generic `https://www.qatarairways.com/` Referer |

### 3.1 Decisive evidence: two separate gates

After a warm paste from an active browser session (the copied request happened to come from
a calendar view; that provenance does not affect this finding), `--scan` was run
(`--depart 2026-12-27 --ret 2027-01-07`):

```
request 1/9  2026-12-23  → 13 offers   (live)
request 2/9  2026-12-24  → 10 offers   (live)
request 3/9  2026-12-25  Cached
request 4/9  2026-12-26  Cached
request 5/9  2026-12-27  →  4 offers   (live)
request 6/9  2026-12-28  →  6 offers   (live)
request 7/9  2026-12-29  HTTP 403
request 8/9  2026-12-30  HTTP 403
```

Findings:

- The same identity worked for **four live calls** (requests 1, 2, 5, 6), then was blocked
  at request 7. This is a **call-budget / velocity** gate.
- The earlier run blocked on its **first live call**. That is the other gate: the
  **identity/session was not valid** (burned, stale, or incomplete).
- `2026-12-27` was blocked in the first run but succeeded in the second, so the block is
  **not date-specific**.
- `_abck` ending in `~-1~` was present in the working run, so that state alone is not
  disqualifying.

Conclusion: handle **both** gates —

1. **Identity validity**: paste from a warm, complete browser session; persist and refresh
   cookies; abort and re-paste when the first call blocks.
2. **Call budget**: 45s jittered spacing, a persisted cross-run throttle, and caching.

Header-endpoint provenance is useful hardening (Phase 1.0) but is not the root cause.

---

## 4. Root cause analysis

The denial is the result of several compounding signals.

### 4.1 TLS / HTTP2 fingerprint mismatch (contributing)
`requests` uses OpenSSL with a distinct JA3/JA4 profile and speaks HTTP/1.1, while the
browser speaks HTTP/2 with Firefox's TLS profile. Akamai fingerprints this and it raises
the bot score, but the latest evidence shows requests succeeding repeatedly, so it is a
contributing signal rather than a hard per-request block. Still worth fixing (Phase 2) for
long-term headroom.

### 4.2 Frozen Akamai sensor cookies (contributing)
The app pastes one cookie snapshot and replays it forever. `_abck` / `bm_sz` / `bm_sv` /
`ak_bmsc` are stateful and normally refreshed by the browser's sensor JS and by server
`Set-Cookie` responses. The latest run succeeded with `_abck` ending in `~-1~`, so this is
not by itself sufficient to explain the 403. Reusing a stale jar still raises risk and
prevents server-side rotation.

### 4.3 Response cookies are discarded
`fetch_flights()` builds a plain dict and calls `requests.post(..., cookies=...)`. It never
reads `Set-Cookie`, so rotating cookies (`BIGipServer...`, refreshed `_abck`) are thrown
away. There is no `requests.Session`.

### 4.4 Velocity / adaptive throttling (confirmed cause of the mid-run block)
Scans fire many POSTs in a short window (`--ob 2 --rb 2` = 10; `--drift 5` = ~22; the
latest `--scan` = 9 + return). The latest run proves a call-count budget: four live calls
succeeded, then the session was blocked and stayed blocked. The current code keeps firing
the remaining dates, which deepens the penalty and wastes the budget. `20s` spacing is too
fast, and delays exist only *within* a run.

### 4.5 Header inconsistency and replayed trace IDs
The app sends a Firefox **152** UA with Firefox **158** cookies, omits browser headers, and
replays the same `Request-Id`/`traceparent` on every call. Each is a small but cumulative
bot signal.

### 4.6 Fixed delay and fail-open loop
A fixed 20s interval is itself a pattern, and on 403 the loop continues to the next date
instead of aborting. This extends the block and wastes the remaining budget.

### 4.7 No cross-run throttle or persisted request budget
The delay is per-process only. If the user runs two scans back-to-back, Akamai counts both
against the same session/IP while the tool starts fresh. Without a persisted rate limiter
(recent live-request timestamps on disk), runs can stack past the limit.

### 4.8 Budget is unknown and must be calibrated
Observed: ~4 live calls succeeded before the block at 20s spacing. The exact rule (calls per
minute vs. calls per session vs. decay window) is unknown, and the 4-call figure may simply
reflect the too-fast 20s spacing. **Starting mitigation: 45s base spacing with per-request
jitter (~40–55s); abort on the first 403.** Do not hard-code the observed 4 as a permanent
cap — calibrate the real budget (Phase 0.5) and only then add a configurable safety cap if
needed.

> Important: if a run succeeds several times and then blocks, it is rate-based. Do not
> respond by retrying immediately — that extends the penalty.

---

## 5. Current implementation issues (code references)

| # | Issue | Location | Status |
|---|-------|----------|--------|
| 1 | Plain cookie dict; response `Set-Cookie` discarded; no `Session` | `fetch_flights()` | **Fixed (T03)** |
| 2 | Paste parser dropped `User-Agent`, `Accept-Language`, `Sec-Fetch-*`, `Pragma`, `Cache-Control` | `parse_pasted_headers()` | **Fixed (T02)** |
| 3 | UA hardcoded to Firefox 152 | `parse_pasted_headers()` | **Fixed (T02)** |
| 4 | `Request-Id` / `traceparent` stored once and replayed forever | `fetch_flights()` | **Fixed (T03)** |
| 5 | Generic Referer, not date-accurate | `fetch_flights()` / `build_referer()` | **Fixed (T03)** |
| 6 | Fixed 20s (scan) / 10s (drift) delay, no jitter | `_scan_dates()`, `drift_scan*()` | **Fixed (T05)** |
| 7 | On non-200 the scan continues to the next date; no abort/backoff | `fetch_flights()`, `_scan_dates()` | **Fixed (T05)** |
| 8 | Raw Akamai HTML dumped into the console | `_fetch_and_parse()` | **Fixed (T05)** |
| 9 | **Bug:** `--nocache` + successful fetch crashed | `fetch_flights()` | **Fixed (T01)** |
| 10 | No surfacing of `_abck` validation state | `parse_pasted_headers()` | **Partially fixed (T02); T13** |
| 11 | No cross-run throttle / persisted request budget | scan loops | **Fixed (T04)** |
| 12 | Fixed 20s spacing too aggressive | `SCAN` | **Fixed (T04)** |
| 13 | Request budget never calibrated | — | T06 |
| 14 | No partial-result reporting on abort | scan loops | **Fixed (T05)** |
| 15 | No lock/single-instance guard for rate state | — | **Fixed (T04)** |
| 16 | Referer not date-accurate | `build_referer()` | **Fixed (T03)** |

### Post-T05 review remediation (2026-10-26)

| Task | Finding | Severity |
|------|---------|----------|
| T09 | Non-JSON/malformed 200s and unsafe FX/price conversion | High |
| T10 | Non-atomic/unguarded offers cache, archive, price map, currency writes | High |
| T11 | Cooldown race, fail-open corrupt state, abort-masking block writes | Done (2026-10-26) |
| T12 | Rich-markup JSON corruption, `--urls`/JSON conflicts, local-mode/config and exit-code bugs | Medium |
| T13 | Header policy not enforced at send time; config/cookie fragility; incomplete `_abck` warning; unbounded error bodies | Medium |
| T14 | README/TASK/task-data contradictions and remaining regression-test gaps | Medium |

T09–T11 fixed (2026-10-26); T12–T14 remain.

Historical note: issue 9 was fixed in T01; the `--nocache` diagnostic path remains covered by
`tests/test_nocache.py`.

---

## 6. API notes (`flight-offers`)

### 6.1 Endpoint

```
POST https://www.qatarairways.com/dapi/public/bff/web/flight-search/flight-offers
```

### 6.2 Request

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

- One itinerary per call; outbound and return are **separate** calls.
- Two `isRequested: true` itineraries returns empty.
- `cabinClass: "ECONOMY"` does not filter the response; economy and business fares are
  returned.
- No multi-date support.

### 6.3 Response

- `flightOffers[]` → `segments[]` + `fareOffers[]`.
- Segment: `flightNumber`, `vehicle.code`, `departure/arrival`, `duration`,
  `qSuiteEquipped`.
- Fare offer: `boundId`, `price.total`, `currencyCode`, `fareFamilyCode`, `cabinType`,
  `availableSeats`, `isLowestFare`.
- Use `price.total` (taxes included), then convert non-NOK via the existing FX helper.
- One response = one direction/date only.

### 6.4 Required vs. browser-like headers

- App/auth essentials: `Content-Type: application/json`, `nbx_fs_api_key`, `session-id`,
  `X-AssignedDeviceID`, `Origin`, `Referer`, plus cookies.
- Browser-like (to pass Akamai): UA matching TLS, `Accept`, `Accept-Language: en`,
  `Accept-Encoding`, `Sec-Fetch-Dest/Mode/Site`, `Pragma`, `Cache-Control`.
- Safe to drop: marketing/analytics cookies (`_ga*`, `AMCV*`, `OptanonConsent`, `s_*`,
  `mbox`, `_fbp`, `_gcl_au`, `kndctr_*`, `FPID`, `FPLC`).
- Must keep/echo: `_abck`, `bm_sz`, `bm_sv`, `ak_bmsc`, `BIGipServer*`, `AKA_A2`,
  `TS01753a24`/`TS01163f70`.
- Do not copy `Host` or `Content-Length`; `requests` computes them.

> Deferred calendar research: see `TASK_FARE_CALENDAR.md`.

---

## 7. Proposed solution

### Phase 0 — Diagnostics (do first)
1. Fix the `--nocache` `UnboundLocalError` (issue 9).
2. `--paste` from a **warm, complete** browser session, then make **one** live request
   (`--nocache`, single date).
   - Success → identity valid; the mid-run problem is the call budget (Phase 0.5/1).
   - 403 → identity invalid: re-paste a warm session and verify cookie completeness first.
     Only if a genuinely fresh warm paste still 403s on the first call should TLS
     impersonation (Phase 2) be considered.
3. Log `_abck` state (`~0~` vs `~-1~`) and warn when it looks stale.

### Phase 0.5 — Rate-limit calibration
Run a controlled experiment to find the budget. Each data point starts from a fresh
`--paste` (or a 30+ min cooldown) and makes N live calls at interval T:

- **T = 45s base with jitter (~40–55s)** — the agreed starting point (e.g. 45, 40, 50).
  Record how many live calls succeed before the first 403. A full scan taking several
  minutes is acceptable.
- Only if 45s still blocks: try longer bases (60s, 90s, 120s) and record the count.
- Also test whether the limit is **per session** (re-paste resets it) or **per IP/window**
  (re-paste does not reset it, only time does).

Output: a conservative default (spacing range, cooldown) and whether the limit is
session-scoped or time-scoped. Encode these in `SCAN` config.

### Phase 1.0 — Header provenance & validation (hardening)
1. Parse the request line (`POST /dapi/public/bff/web/flight-search/flight-offers HTTP/2`)
   and the `Host`. Currently ignored (`flights.py:158-191`).
2. Accept only QR `flight-offers` requests; reject other hosts/paths with a clear error
   ("Paste headers from a qatarairways.com flight-search request").
3. Require `session-id`, `X-AssignedDeviceID`, `nbx_fs_api_key`. Warn (don't fail) if the
   Akamai cookies (`_abck`, `bm_sz`, `ak_bmsc`) are missing.
4. Store `source_endpoint` and the full browser header set in `config.json`.
5. If the request line is absent (headers-only paste), allow but warn that provenance could
   not be verified.

### Phase 1 — Session, headers, trace IDs, pacing
1. Replace `requests.post` with a persistent `requests.Session`; load `config.json`
   cookies into the session and let `Set-Cookie` update them; persist back to disk.
2. Regenerate per request, **sharing the same trace/span pair** (as the browser does):
   - trace-id = 16 random bytes (`secrets.token_hex(16)`); span-id = 8 random bytes
     (`secrets.token_hex(8)`)
   - `Request-Id: |{trace-id}.{span-id}`
   - `traceparent: 00-{trace-id}-{span-id}-01`
3. Honor the pasted `User-Agent`; add `Accept-Language: en`, `Sec-GPC: 1`,
   `Sec-Fetch-Dest/Mode/Site`, `Pragma: no-cache`, `Cache-Control: no-cache`.
   - Do **not** copy `Host` or `Content-Length` from the paste; `requests` computes them.
   - Do **not** send `Accept-Encoding: br, zstd` with plain `requests` unless
     `brotli`/`zstandard` are installed; keep the library default (or use `curl_cffi`).
4. Build a date-accurate `Referer` from the request arguments (`departing`/`returning` =
   the actual requested leg dates).
5. Add jittered delays, a **persisted rate limiter**, and **abort on 403**:
   - base delay = **45s**, jittered per call (~40–55s, e.g. `random.randint(40, 55)`)
   - **no hard per-run cap initially** — 45s spacing is the primary control and
     abort-on-403 is the hard stop. After Phase 0.5, add an optional `--budget` safety cap
     only if the measured limit warrants it
   - `.rate_state.json` stores **UTC ISO timestamps** of live calls plus a `blocked_until`
     timestamp; use `fcntl.flock` to serialize and prune old entries
   - enforce the minimum spacing **across process runs**; cache hits do **not** count
   - if `blocked_until` is in the future, refuse live calls and print the remaining time
     (override only with an explicit `--force`)
   - on 403: stop the run immediately; do not fire remaining dates; set `blocked_until`
     (start 15–30 min, calibrate) and print "Blocked at Akamai edge — wait or `--paste`";
     preserve and report the results already collected
   - do not retry an edge block in the same run; a later run after the cooldown may try
     again. `--max-retries` applies only to transient 5xx/timeout failures
6. Print a clear message instead of raw HTML.
   - Classify: HTML / `Access Denied` / `edgesuite` = **Akamai edge block**; JSON 401/403 =
     **app-level auth/session error**. Handle them differently.
7. Persist updated cookies + `saved_at` after each successful request.

### Phase 2 — TLS/HTTP2 impersonation (optional hardening)
- Do this only if a genuinely fresh, warm paste still 403s on the first call; the captures
  show plain `requests` can succeed many times within a valid session.
- Add optional `curl_cffi` backend: `curl_cffi.requests.Session(impersonate="firefox…")`.
- Set the UA to match the chosen impersonation profile.
- Fall back to `requests` with a warning if `curl_cffi` is not installed.
- Update README install instructions (`pip install requests rich curl_cffi`).

### Phase 3 — Optional robustness
- Playwright-driven session for cookie harvesting/warm-up if Phases 1–2 are insufficient.
  Heaviest option; only if needed.

---

## 8. Implementation details

### 8.1 Config schema (`config.json`)

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

Add `.rate_state.json` to `.gitignore` (local data).

### 8.2 New / changed functions
- `parse_pasted_headers()` — stop hardcoding UA; preserve the pasted UA and browser headers;
  use a cookie **allowlist** (Akamai + session) rather than a blocklist.
- `make_trace_headers()` — fresh shared trace/span pair formatted as `Request-Id` and
  `traceparent`.
- `build_referer(origin, destination, depart, ret)` — date-accurate.
- `HttpClient` — holds a `Session`, applies headers, regenerates trace headers,
  injects/updates cookies, returns `(status, json|text)`.
- `fetch_flights()` — use `HttpClient`; fix the `cache` initialization bug; on 403 return a
  typed error.
- `_scan_dates()`, `drift_scan()`, `drift_scan_return()` — abort on block, preserve
  partials; pacing stays in `fetch_flights()`.
- `classify_block(status, body)` — Akamai HTML vs app JSON error.
- `RateLimiter` — spacing + cross-run history + `blocked_until`, guarded by a file lock.

### 8.3 CLI
- Keep `--delay` (base value with jitter; default 45).
- Abort-on-block is unconditional; do not add a disable flag.
- Add `--max-retries {0,1}` (default 0) for transient 5xx/timeout failures only; never retry
  edge/auth/rate/budget.
- Add `--budget` (optional live-call safety cap; default off until calibrated).
- Add `--no-impersonate` to force the `requests` backend.

### 8.4 Backward compatibility
- Existing `config.json` without `user_agent`/`impersonate` still loads.
- If `curl_cffi` is missing, behave exactly as today but warn once.

---

## 9. Rate/delay policy (decided)

Evidence: a valid identity survived four live calls at 20s spacing, then blocked for the
rest of the run. Longer runs are acceptable — correctness beats speed.

- **Base spacing = 45s**, with per-request jitter so the interval is not a fixed pattern.
  Example for 10 calls: `45, 40, 50, 43, 47, …` (~40–55s, `random.randint(40, 55)`).
- **Live-call definition**: only network POSTs count; cache hits are free.
- **Per-run budget**: no hard cap initially — 45s spacing + abort-on-403 are the controls.
  After calibration, add an optional `--budget` safety cap only if needed.
- **Cross-run throttle**: persist recent live-call timestamps and enforce the same spacing
  across back-to-back runs (`.rate_state.json`); lock the file or document single-instance
  use.
- **Abort on the first 403**: stop immediately, do not fire the remaining dates, record a
  cooldown, print the edge-block message, and preserve the results already collected.
- **Cooldown after a block**: 15–30 min, or re-paste fresh cookies; do not retry sooner.
- **Reduce calls**: rely on `offers_cache.json`/archive; the floor is 2 calls per date pair.
- Prefer several short runs spread over time (using the cache) over one large scan.
- Do not run through VPN/datacenter egress; it worsens IP reputation.

### 9.1 Rotation and timestamp accuracy

- **Rotate every request**: `Request-Id` and `traceparent`, generated as a shared
  trace/span pair (random, **not** time-based).
- **Vary with the request**: payload `departureDate` and the `Referer` `departing`/
  `returning` values must match the date actually being queried.
- **Keep stable per session**: `User-Agent`, `session-id`, `X-AssignedDeviceID`, and the
  Akamai cookie jar. They may change between runs (fresh browser session) but must not
  rotate per call.
- **Timestamps must be accurate**: trace/span IDs contain no timestamp and should be
  random. Every real date/time field must be generated from the request arguments and the
  current clock — never hardcoded, stale, or copied from an old capture.
- Do not fabricate cookie-embedded timestamps (e.g. `ai_session`); echo them from the paste
  and let server `Set-Cookie` update them.

---

## 10. Rotate vs. keep vs. drop

| Item | Policy |
|---|---|
| `Request-Id`, `traceparent` | Rotate **every request** |
| Payload dates / `Referer` dates | Vary per request to match the target date |
| Timestamp/clock fields in payload | Generate fresh from the actual request; must be accurate |
| `User-Agent` | Keep stable; must match the pasted session/TLS profile — do not randomize |
| `session-id` | Keep stable per run; new value per fresh browser session |
| `X-AssignedDeviceID` | Keep stable per session; **do not randomize per request** |
| `_abck`, `bm_sz`, `bm_sv`, `ak_bmsc`, `BIGipServer*` | Keep and update from responses; refresh via paste when stale |
| Marketing/analytics cookies | Drop (irrelevant) |

Note: it is fine to start a new identity per run, but a **random** device ID with no valid
sensor cookie is a stronger bot signal. `X-AssignedDeviceID` and `session-id` should move
together with a real browser session, not be randomized per request.

---

## 11. Diagnostic experiments

1. **Identity sanity check**: fresh warm paste → single live request. Record status +
   `_abck` state. A 403 here means the identity gate failed (stale/incomplete paste or IP).
2. **Rate calibration** (Phase 0.5): baseline 45s with jitter (~40–55s). Record how many
   live calls succeed before the first 403; only lengthen the base if 45s still blocks.
3. **Scope**: after a block, re-paste and immediately try one call. If it still 403s, the
   block is IP/time-scoped; if it succeeds, it is session-scoped.
4. Only if a **fresh warm single** request 403s: test a `curl_cffi` Firefox profile with the
   same headers/cookies. If that succeeds, TLS fingerprint is the gate.

Record results in section 17.

---

## 12. Acceptance criteria

- A single live `flight-offers` request succeeds immediately after `--paste` from a warm
  browser session.
- Live calls are spaced at a **45s base with per-request jitter (~40–55s)**, never a fixed
  interval; a full scan may take several minutes.
- Cross-run spacing is enforced via `.rate_state.json`; cache hits do not count as live
  calls; an optional safety cap can be configured after calibration.
- A scan either completes within the measured budget, or aborts cleanly with a clear,
  actionable message (no raw HTML, no silent continuation, no retry storm).
- The run aborts on the **first 403**; already-collected results are preserved and shown.
- A **10-call scan at 45s jitter** is the target validation case.
- Response `Set-Cookie` values are persisted and reused on the next call.
- `Request-Id`/`traceparent` rotate every request as a shared trace/span pair; date/timestamp
  fields always match the actual requested dates and current time.
- `--nocache` works without crashing.
- The `--paste` validator rejects blocks that are not QR `flight-offers` requests or that
  lack the required auth headers (Phase 1.0).

---

## 13. Test plan

- Unit: `make_trace_headers()` returns a shared trace/span pair and both headers match.
- Unit: `build_referer()` includes the correct dates; `parse_pasted_headers()` preserves UA
  and Akamai cookies and validates provenance.
- Unit: rate limiter enforces spacing, cross-run history, and `blocked_until` with a fake
  clock; cache hits don't count.
- Unit: abort-on-403 stops the loop and preserves earlier results.
- Unit: `classify_block()` distinguishes Akamai HTML from JSON errors.
- Unit: date/timestamp fields equal the requested dates and the current clock.
- Unit: `fetch_flights()` cache path with `_nocache=True` and `False` (regression for
  issue 9).
- Integration (opt-in, real network): one live request; then a 10-call scan at 45s jitter.
- Regression: cached runs remain fast and produce identical output.
- Error path: simulate 403 HTML and assert the run aborts with the friendly message.

---

## 14. Risks and tradeoffs

- `curl_cffi` (Phase 2) adds a dependency and must track browser profiles; UA/impersonation
  must be consistent or the mismatch worsens detection. It is optional.
- Akamai rules change without notice; periodic re-pasting may still be required.
- `config.json` contains live session cookies — never log or commit it (it is gitignored).
- Aborting on 403 means incomplete scans; results already collected are preserved.
- **Runtime**: 45s × 10 calls ≈ 7.5 min per direction; drift scans can take 10–20+ min.
  Accepted, but the tool is no longer fast.
- A persisted rate limiter can block the user after a 403 (by design); show the cooldown and
  provide an explicit override if needed.
- Concurrent runs need a lock (or a documented single-instance assumption) or the state can
  race.

---

## 15. Open questions

1. Whether the **45s base** is sufficient or needs lengthening (Phase 0.5 results).
2. Is the limit session-scoped or IP/time-scoped? (re-paste test)
3. Whether the user accepts `curl_cffi` as a dependency (Phase 2, optional).

---

## 16. Milestones

- M1: Phase 0 + issue 9 fix + Phase 0.5 calibration (diagnosis; only `--nocache` behaviour
  change).
- M2: Phase 1.0 + Phase 1 (header validation, session cookies, rotating trace headers,
  accurate dates/timestamps, 45s jittered spacing, persisted cross-run throttle, lock,
  abort-on-403, partial results).
- M3 (optional): Phase 2 (`curl_cffi` backend + fallback), only if first-call 403s persist.
- M4 (optional): Phase 3 (Playwright).

---

## 17. Results log

| Date | Test | Result | Notes |
|------|------|--------|-------|
| 2026-10-25 | `--ob 2 --rb 2`, 20s | 403 on first live call (req 3 overall) | 2 dates cached; stale/burned identity |
| 2026-10-25 | `--scan` after warm paste, 20s | 4 live OK (req 1,2,5,6), 403 at req 7 | call-budget/velocity gate; identity was valid |

---

## 18. QC review and improvements

### 18.1 Corrected
- **Scope is detail-first**: `flight-offers` only; one call per direction/date; 2 calls per
  date pair.
- **Header edge cases**: do not copy `Host`/`Content-Length`; do not request `br`/`zstd`
  with plain `requests` unless the decoders are installed.
- **403 classification**: distinguish Akamai HTML (`Access Denied`/`edgesuite`) from app
  JSON errors.
- **Rate limiter details**: UTC timestamps, `blocked_until`, file lock, live-calls-only.
- **Referer/date accuracy**: build from the real request arguments, never from samples.
- **`--nocache` regression**: `cache` is undefined on the `_nocache` path and would crash on
  a successful response (`fetch_flights()`, fixed in T01).

### 18.2 Hypotheses to test (Phase 0.5)
1. **45s may still be above the budget.** 20s allowed 4 live calls; test 45/60/90/120s and
   record the first 403. If 45s blocks quickly, increase the base rather than retrying.
2. **The block is probably per session + IP with a slow decay.** Re-paste immediately after
   a block and send one call: if it still 403s, only time/IP change helps.
3. **Caching is the only safe request reducer.** The detail requirement forbids swapping in
   price-only calls; rely on `offers_cache.json` + archive.
4. **Header coherence may matter more than raw delay.** If 45s still blocks, test the
   browser-like header set (UA, `Sec-Fetch-*`, accurate Referer) before assuming a pure rate
   cap.

### 18.3 Suggested improvements (not required)
- **Cookie allowlist** instead of a blocklist: send only Akamai + session cookies; drop all
  analytics.
- **`--dry-run`**: print the exact payload, headers, trace pair, and computed dates without
  sending. Validates rotation/accuracy cheaply.
- **`--doctor`**: validate `config.json`, required headers/cookies, `_abck` state, and the
  rate-limiter state; print what to fix.
- **Structured request log**: timestamp, endpoint, duration, status, calls used/spacing.
- **Budget meter**: show "3/4 live calls used" in scans so the user knows when to stop.
- **Mirror the browser `flight-offers` payload** (include the unrequested return leg) for
  closer parity, if symptoms persist. The single-itinerary payload works today.

### 18.4 Residual risks / non-goals
- This cannot guarantee bypassing Akamai; the goal is polite, browser-like behaviour and a
  clean abort. Never hammer after a 403.
- Detail is mandatory; do not trade it for price-only shortcuts.
- `config.json` and cookies are sensitive; never log or commit them.

---

## 19. Related documents

- `task-data.md` — shared reference: headers, cookies, config, rate policy, error taxonomy,
  code map, test conventions.
- `tasks/` — executable task documents (T01–T14); index in `task-data.md` §0.
- `TASK_FARE_CALENDAR.md` — deferred `fare-calendars` research (price-only; not used).
- `api.md` — flight-offers API reference.
- `README.md` — user-facing flags and examples.
