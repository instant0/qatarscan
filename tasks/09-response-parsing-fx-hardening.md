# T09 — Response parsing & FX conversion hardening

- **Required**: Yes
- **Status**: **Done** (2026-10-26)
- **Depends on**: T03, T05, T10
- **Blocks**: T06, T14
- **Reference**: `../task-data.md` §3 (response fields), §7 (files), §8 (errors),
  §11 (tests); post-T05 review H1/M6

## Objective

Make a successful HTTP response safe to parse even when it is not the expected JSON or when
individual offers/fares are incomplete, and make FX conversion safe for missing/non-numeric
prices. A malformed response must produce a typed, user-facing failure instead of an uncaught
`JSONDecodeError`, `TypeError`, or traceback.

## Scope

`flights.py`: `fetch_flights()`, `_fetch_and_parse()`, `parse_results()`,
`convert_prices()`, `get_conversion_rate()`, the non-scan parse call sites, and
`_load_archive_prices()`'s reparse call. T10 owns the atomic JSON helper, corruption
quarantine, `currency.json` persistence mechanics, and the read-only price-map/archive
consumers. T13 owns `config.json` and request-header handling.

## Review findings addressed

- A 200 response with HTML/non-JSON crashes at `resp.json()` / `parse_results()`.
- The draft pointed the non-JSON hook at `_fetch_and_parse()`, but decoding happens in
  `fetch_flights()`; `_fetch_and_parse()` receives an already-decoded dict.
- A malformed offer can crash on `segments[0]["departure"]`, numeric duration/stops, nested
  `fareFamilyFeatures`, or `price_info["total"]`.
- `convert_prices()` crashes when `price` is `None` or non-numeric, and would warn once per
  fare if the warning is placed inside the loop.
- FX fetch/cache failures are silent or can leave a stale rate without an explicit warning;
  memory/disk cache entries are trusted without validation.
- A review probe mutated the `USD_NOK` cache row; the cache must preserve
  unknown/existing currency rows.

## Steps

1. **Non-JSON 200**
   - Wrap `resp.json()` in `fetch_flights()`, after block/status classification and before
     caching.
   - On `ValueError` / `requests.exceptions.JSONDecodeError`, raise
     `RequestFailed(resp.status_code, "Non-JSON response")` from the decode error. Do not cache
     the decoded/failed payload.
   - Do not print raw HTML; use the existing sanitized one-line error path.
   - Preserve T05 behavior: `RequestFailed` is transient, does not record a cooldown, and is
     skipped by scan loops; non-scan callers render a clear error and exit non-zero.
2. **Payload shape validation**
   - Treat `200 + {}` or a missing/empty `flightOffers` list as a valid empty result. Treat a
     non-dict payload or any other non-list `flightOffers` value (including `null`) as
     malformed: raise `RequestFailed(200, "Malformed flight-offers payload")`. Individual
     non-dict offers/fares/segments are skipped and counted so valid siblings still render.
   - Parse each offer/fare defensively:
     - skip offers with no non-empty segment list, any non-dict segment, or no valid
       first-segment departure timestamp;
     - validate `duration`/`numberOfStops` as finite non-negative numbers where used;
       an invalid numeric value is treated as missing, not a crash;
     - never iterate a non-list `segments`/`fareOffers`/`fareFamilyFeatures` or index a list
       without checking it;
     - skip non-dict offers/fares and fares with missing/empty non-dict price objects;
     - derive `departure_date` only from a valid timestamp;
     - keep a fare with a non-empty price object even if `total` is missing; conversion will
       leave it unchanged and warn;
     - normalize an invalid/missing `numberOfStops` to `0` so downstream price-map code does
       not receive `None`;
     - make `extract_avios()` defensive without changing its successful return value;
     - log at most one warning per response with counts of skipped offers/fares. Add
       `parse_results(..., warn=True)` and pass `warn=False` from `_load_archive_prices()` so
       historical re-parsing does not spam warnings.
3. **Call-site handling**
   - In `_fetch_and_parse()`, catch `RequestFailed` from `parse_results()` and return `[]`
     after the existing one-line error log, matching the current transient-failure behavior.
   - In non-scan outbound/return paths, catch `RequestFailed` around `parse_results()` and
     exit 1 without a traceback; do not update `price_map.json` with unparsed data. A valid
     outbound partial may still be displayed when the return parse fails.
   - In `_load_archive_prices()`, keep skipping malformed stored payloads but call
     `parse_results(data, warn=False)` and `convert_prices(parsed, warn=False)`.
4. **Price conversion**
   - In `convert_prices(results, warn=True)`, convert only finite, non-negative `int`/`float`
     prices (exclude `bool`); require a non-empty string currency. Treat `nok`
     case-insensitively as NOK.
   - If the price or currency is missing/invalid, keep the original price/currency and do not
     raise. Log one summary warning per call with the skipped count, not one warning per fare;
     `warn=False` suppresses it for archive re-parsing.
   - Validate that the returned FX rate is a positive finite number before multiplying.
   - Set `_converted = True` only when conversion succeeds.
5. **FX fetch/cache semantics**
   - Normalize `target_currency` to uppercase and validate it as a 3-letter alphabetic code
     before building the URL/cache key. Invalid codes warn once and return `None`.
   - Accept only a positive finite `data[base]["nok"]` rate from the FX endpoint.
   - Validate memory and disk cache entries before use: the entry must be a dict with a positive
     finite numeric `rate` and, for same-day use, a matching date string.
   - On network, decode, or validation failure:
     - use the existing cached rate only if it is valid;
     - log that the stale rate is being used and its date;
     - otherwise return `None` and leave the fare unconverted.
   - Do not write an invalid rate and do not remove any existing keys from `currency.json`.
   - Preserve all keys on write, including `USD_NOK`; merging/atomic writes are T10.
   - Do not add FX calls to the live-call budget or rate limiter; they are a different host.
6. **Tests**
   - Add `tests/test_parsing.py`:
     - `fetch_flights()` on 200 HTML/non-JSON raises `RequestFailed` and does not cache it;
       `_fetch_and_parse()` returns `[]` after logging.
     - non-dict/top-level malformed payloads and non-list `flightOffers` raise
       `RequestFailed`.
     - non-dict offers, non-list segments/fares, missing segments/departure, invalid
       duration/stops, and malformed `fareFamilyFeatures` are skipped without crashing.
     - empty/missing `flightOffers` returns `[]`.
     - valid payload still parses and preserves existing fields; `warn=False` suppresses
       skip warnings.
     - `_load_archive_prices()` skips malformed stored payloads without warning spam.
   - Add `tests/test_currency.py`:
     - `price=None`/string/NaN/inf/negative/bool is left unchanged; one summary warning per
       call.
     - valid conversion rounds and sets `_converted`; lowercase currency works; `warn=False`
       suppresses the summary warning.
     - FX payload missing/invalid/negative/NaN `nok` falls back to a valid cached rate.
     - invalid cached rate is not used and no invalid rate is written.
     - stale valid cache is used with its date in the warning.
     - same-day cached rate avoids `requests.get`.
     - saving a new rate preserves unrelated keys such as `USD_NOK`.
   - No live network in tests; patch `requests.get` / the FX fetch seam and reset
     `_currency_rates` between tests.

## Acceptance criteria

- A non-JSON or malformed 200 never raises an uncaught exception or prints raw HTML.
- Bad offers/fares are skipped; valid offers in the same response still render, with at most
  one summary warning per parse call.
- `convert_prices()` never crashes on missing/non-numeric prices and warns at most once per
  call.
- FX failures are explicit, existing cache rows are preserved, and only positive finite rates
  are used.
- Archive re-parsing does not emit per-entry warning spam.
- All existing tests plus the new parsing/FX tests pass; `py_compile` is clean.

## Execution log

- Hardened `fetch_flights()` against non-JSON 200 responses; raises
  `RequestFailed(status, "Non-JSON response")` without caching the payload.
- Reworked `parse_results()` with top-level shape validation, defensive offer/fare/segment
  handling, numeric/date validation, normalized stops, and one summary warning per call;
  added `warn=False` for archive re-parsing.
- Made `extract_avios()` defensive without changing successful returns.
- Hardened `convert_prices()` and `get_conversion_rate()` for invalid prices, currencies,
  cache entries, and endpoint rates; stale valid rates are explicit fallbacks only.
- Added `RequestFailed` handling in `_fetch_and_parse()` and non-scan outbound/return paths;
  `_load_archive_prices()` now re-parses and converts with warnings suppressed.
- Added `tests/test_parsing.py` (9 tests) and `tests/test_currency.py` (11 tests).
- Verification: `python -m py_compile flights.py` OK; `python -m unittest discover -s tests`
  -> 98 tests OK. No live scans or network requests.

## Notes

- No code comments (repo rule).
- Do not delete/rename files; T10 owns quarantine/atomic writes.
- Do not weaken T05's abort taxonomy: malformed responses are `RequestFailed`, not edge/auth.
- The USD row is a cache entry, not a feature switch. Removing it only forces a later refetch;
  this task must not delete such rows during normal operation.
