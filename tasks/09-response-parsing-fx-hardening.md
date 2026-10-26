# T09 — Response parsing & FX conversion hardening

- **Required**: Yes
- **Status**: **Pending**
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

`flights.py`: `_fetch_and_parse()`, `parse_results()`, `convert_prices()`,
`get_conversion_rate()`, and the non-scan parse call sites. T10 owns the atomic JSON helper,
corruption quarantine, `currency.json` persistence mechanics, and the read-only
price-map/archive consumers. T13 owns `config.json` and request-header handling.

## Review findings addressed

- A 200 response with HTML/non-JSON crashes at `resp.json()` / `parse_results()`.
- A malformed offer can crash on `segments[0]["departure"]` or `price_info["total"]`.
- `convert_prices()` crashes when `price` is `None` or non-numeric.
- FX fetch/cache failures are silent or can leave a stale rate without an explicit warning.
- A review probe mutated the `USD_NOK` cache row; the cache must preserve
  unknown/existing currency rows.

## Steps

1. **Non-JSON 200**
   - Wrap `resp.json()` in `_fetch_and_parse()`.
   - On `ValueError` / `requests.exceptions.JSONDecodeError`, raise
     `RequestFailed(resp.status_code, "Non-JSON response")` from the decode error.
   - Do not print raw HTML; use the existing sanitized one-line error path.
   - Preserve T05 behavior: `RequestFailed` is transient, does not record a cooldown, and is
     skipped by scan loops; non-scan callers render a clear error and exit non-zero.
2. **Payload shape validation**
   - Treat `200 + {}` or a missing/empty `flightOffers` list as a valid empty result.
   - Treat a non-dict payload, a non-list `flightOffers`, or a non-dict offer as malformed:
     raise `RequestFailed(200, "Malformed flight-offers payload")`.
   - Parse each offer/fare defensively:
     - skip offers with no valid segments or no departure timestamp;
     - skip fares with missing/empty price objects;
     - derive `departure_date` only from a valid timestamp;
     - never index a list without checking it;
     - log at most one warning per response with the number of skipped offers/fares.
   - Add a helper for safe nested lookups if it keeps `parse_results()` readable.
3. **Call-site handling**
   - In `_fetch_and_parse()`, catch `RequestFailed` from `parse_results()` and return `[]`
     after the existing one-line error log, matching the current transient-failure behavior.
   - In non-scan outbound/return paths, catch `RequestFailed` around `parse_results()` and
     exit 1 without a traceback; do not update `price_map.json` with unparsed data.
4. **Price conversion**
   - In `convert_prices()`, convert only finite, non-negative `int`/`float` prices (exclude
     `bool`).
   - If the price is missing/invalid, keep the original price/currency and log one warning;
     do not raise.
   - Validate that the returned FX rate is a positive finite number before multiplying.
   - Set `_converted = True` only when conversion succeeds.
5. **FX fetch/cache semantics**
   - Normalize `target_currency` to uppercase and validate it before building the URL/cache key.
   - Accept only a positive finite `data[base]["nok"]` rate from the FX endpoint.
   - On network, decode, or validation failure:
     - use the existing cached rate only if it is valid;
     - log that the stale rate is being used and its date;
     - otherwise return `None` and leave the fare unconverted.
   - Do not write an invalid rate and do not remove any existing keys from `currency.json`.
   - Preserve all keys on write, including `USD_NOK`; merging/atomic writes are T10.
   - Do not add FX calls to the live-call budget or rate limiter; they are a different host.
6. **Tests**
   - Add `tests/test_parsing.py`:
     - 200 HTML/non-JSON raises `RequestFailed` and is caught by `_fetch_and_parse()`.
     - non-dict/top-level malformed payloads raise `RequestFailed`.
     - missing segments/departure/price are skipped without crashing.
     - empty/missing `flightOffers` returns `[]`.
     - valid payload still parses and preserves existing fields.
   - Add `tests/test_currency.py`:
     - `price=None`/string/NaN/negative is left unchanged with a warning.
     - valid conversion rounds and sets `_converted`.
     - FX payload missing/invalid `nok` falls back to a valid cached rate.
     - invalid cached rate is not used and no invalid rate is written.
     - same-day cached rate avoids `requests.get`.
     - saving a new rate preserves unrelated keys such as `USD_NOK`.
   - No live network in tests; patch `requests.get` / the FX fetch seam.

## Acceptance criteria

- A non-JSON or malformed 200 never raises an uncaught exception or prints raw HTML.
- Bad offers/fares are skipped; valid offers in the same response still render.
- `convert_prices()` never crashes on missing/non-numeric prices.
- FX failures are explicit, existing cache rows are preserved, and only valid rates are used.
- All existing tests plus the new parsing/FX tests pass; `py_compile` is clean.

## Notes

- No code comments (repo rule).
- Do not delete/rename files; T10 owns quarantine/atomic writes.
- Do not weaken T05's abort taxonomy: malformed responses are `RequestFailed`, not edge/auth.
- The USD row is a cache entry, not a feature switch. Removing it only forces a later refetch;
  this task must not delete such rows during normal operation.
