# T03 — HTTP client: Session, cookie persistence, trace pair, headers, Referer

- **Required**: Yes
- **Status**: **Done** (2026-10-25)
- **Depends on**: T02
- **Blocks**: T05, T07
- **Reference**: `../task-data.md` §2–§8 (§4 headers, §5 cookies, §7 files, §8 errors)

## Objective

Replace the ad-hoc `requests.post(..., cookies=dict)` with a single HTTP client that merges
`Set-Cookie`, regenerates trace headers per call, sends coherent browser headers, builds a
date-accurate Referer, and classifies edge blocks vs app errors.

## Scope

`flights.py` only: new helpers/classes, `fetch_flights()`, `_fetch_and_parse()`, and the two
single-request call sites in `main()`. Do **not** implement abort-on-403 or rate limiting
(T05/T04).

## Steps

1. **Exceptions**
   - `EdgeBlock(Exception)`, `AppAuthError(Exception)`, `RequestFailed(Exception)`.
   - `RequestFailed` should carry the status and a one-line body summary.
2. **`make_trace_headers() -> dict`**
   - `trace-id = secrets.token_hex(16)`, `span-id = secrets.token_hex(8)`.
   - Return `{"Request-Id": f"|{trace_id}.{span_id}",
     "traceparent": f"00-{trace_id}-{span_id}-01"}`.
   - Add `import secrets`.
3. **`build_referer(origin, destination, depart, ret) -> str`**
   - `https://www.qatarairways.com/app/booking/flight-selection?` + URL-encoded params:
     `widget=QR`, `searchType=F`, `addTaxToFare=Y`, `minPurTime=0`, `selLang=en`,
     `tripType=R`, `fromStation=<origin>`, `toStation=<destination>`, `departing=<depart>`,
     `returning=<ret>`, `bookingClass=E`, `adults=1`, `children=0`, `infants=0`, `ofw=0`,
     `teenager=0`, `flexibleDate=off`, `allowRedemption=N`, `sort=duration`.
   - Add `from urllib.parse import urlencode`.
4. **`classify_block(status, body) -> str | None`**
   - `None` for 200.
   - `"edge"` for a 403 whose body is HTML / contains `Access Denied` / `edgesuite`.
   - `"auth"` for JSON 401/403.
   - `"other"` for any other non-200.
   - Keep it pure (no printing/raising); `fetch_flights()` raises.
5. **`HttpClient`**
   - `__init__(headers, cookies)`: copy the headers dict; keep a reference to the caller's
     `cookies` dict; `self.session = requests.Session()`;
     `self.session.cookies.update(cookies)`.
   - `post_offers(payload, referer) -> response`:
     - `headers = dict(self.headers)`;
     - `headers.update(make_trace_headers())`;
     - `headers["Referer"] = referer`;
     - `resp = self.session.post(API_URL, headers=headers, json=payload, timeout=30)`;
     - `self.cookies.update(self.session.cookies.get_dict())`;
     - return `resp`.
   - Do **not** pass `cookies=` to `session.post`; the session owns them.
   - Do not add `Accept-Encoding`; T02 dropped it.
6. **`_persist_cookies(cookies)`**
   - Read `CONFIG_FILE` directly (do not call `load_config()`; avoid its age warning), merge
     `{"cookies": cookies}`, call `save_config(cfg)`.
   - Tolerate a missing/corrupt config.

   > Clarified by T13: the current implementation quarantines a corrupt `config.json`, warns,
   > and skips automatic cookie persistence (it does not overwrite the corrupt file). A
   > missing config still saves the new cookies. The original T03 log below is historical.
7. **`fetch_flights(headers, cookies, origin, destination, departure_date, referer=None)`**
   - Keep the T01 cache behavior exactly (load once; `_nocache` skips the hit check).
   - If `referer` is None, fall back to `headers.get("Referer",
     "https://www.qatarairways.com/")`.
   - On a cache miss: build payload, `HttpClient(...)`, `post_offers(...)`.
   - After the response, call `_persist_cookies(cookies)` (captures `Set-Cookie` even on
     errors).
   - `kind = classify_block(resp.status_code, resp.text)`:
     - `"edge"` → raise `EdgeBlock`;
     - `"auth"` → raise `AppAuthError`;
     - `"other"` → raise `RequestFailed`;
     - `None` → parse JSON, update cache, return `(data, 200, None)`.
   - Keep `(data, status, error)` on success/cache hit for callers.
8. **`_fetch_and_parse()` (temporary, standalone-safe)**
   - Build the Referer from the original search pair and the actual leg dates:
     `build_referer(SEARCH["origin"], SEARCH["destination"], dep_date, ret_date)`.
   - Pass `referer=` to `fetch_flights`.
   - Catch `EdgeBlock`/`AppAuthError`/`RequestFailed`, print a one-line red message, and
     return `[]`. **T05 will change this to abort**; do not implement abort here.
9. **`main()` single-request call sites**
   - Pass the same date-accurate Referer:
     `build_referer(SEARCH["origin"], SEARCH["destination"], SEARCH["departure_date"],
     SEARCH["return_date"])`.
   - Catch the three typed errors per call, print `Outbound:`/`Return:` + message, `sys.exit(1)`.
10. **No rate limiter in T03**
    - Do not add a `limiter.wait()` placeholder. T04 will insert it immediately before
      `post_offers()`.

## Acceptance criteria

- Two consecutive requests have different `Request-Id`/`traceparent`, and each pair shares the
  same trace/span IDs.
- `Set-Cookie` from a response is merged into the in-memory cookie dict and written to
  `config.json`.
- UA/browser headers are preserved; `Referer` is the date-accurate flight-selection URL.
- `Host`/`Content-Length`/`Accept-Encoding` are not sent from stored headers.
- Edge blocks raise `EdgeBlock`; JSON 401/403 raise `AppAuthError`; others raise
  `RequestFailed`.
- Cache hits still avoid the network and do not persist cookies.
- `python flights.py --help` and existing tests still pass.

## Tests (`tests/test_http_client.py`)

- `make_trace_headers()`: two calls differ; within one call, IDs match across both headers.
- `build_referer()`: query contains the right stations/dates.
- `classify_block()`: edge vs auth vs other vs 200.
- `HttpClient`: fake session sets a cookie; assert the caller's cookie dict is updated and the
  request headers contain a fresh trace pair + the supplied Referer, and preserve UA.
- `fetch_flights`: temp `OFFERS_CACHE_FILE`, monkeypatch `_persist_cookies`, fake
  `post_offers`; assert `EdgeBlock`/`AppAuthError`/`RequestFailed` and the success cache write.
- Keep `tests/test_nocache.py` and `tests/test_paste.py` green.

## Notes

- No code comments (repo rule).
- Do not run git commands.
- Do not delete/rename files.
- T05 owns abort-on-403; T04 owns pacing. Keep this task minimal and standalone-safe.

## Execution log

- 2026-10-25 — added `secrets`/`urlencode` imports, `EdgeBlock`, `AppAuthError`,
  `RequestFailed`, `make_trace_headers()`, `build_referer()`, `classify_block()`,
  `HttpClient`, and `_persist_cookies()`.
- `fetch_flights()` now accepts `referer`, uses the session-backed client, persists cookies
  after every response, and raises the typed errors on classified failures. T01 cache
  semantics are unchanged.
- `_fetch_and_parse()` builds a date-accurate Referer and logs typed failures as one line;
  the two single-request `main()` calls pass the Referer and exit on typed errors.
- Updated `tests/test_nocache.py` to patch `HttpClient.post_offers` (network now goes
  through the session) and to stub `_persist_cookies` so tests never touch `config.json`.
- Tests: `python -m py_compile flights.py` clean; `python -m unittest discover -s tests -v`
  → 24 tests OK (13 new in `tests/test_http_client.py`); `python flights.py --help` exits 0.
