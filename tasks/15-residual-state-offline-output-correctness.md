# T15 — Residual state, offline, and output correctness

- **Required**: Yes
- **Status**: **Done** (2026-10-26)
- **Depends on**: T09, T10, T12, T13
- **Blocks**: T14, T06
- **Reference**: `../task-data.md` §4 (headers), §7 (files/schemas), §8 (errors), §10 (code map), §11 (tests); post-T13 audit findings

> 2026-10-26 — added from the T14/T06 review. These are confirmed behavior gaps that do
> not belong in T14 (documentation/test-contract cleanup) or T06 (verification/calibration).
> T15 must land before T14's final docs/test sweep and before T06 sign-off.

## Objective

Close the remaining required correctness gaps found after T13: avoid FX retry storms in local
views, make persisted/source JSON schema-safe, stop caching unparseable payloads, generate
direction-correct booking links, and harden numeric/default-date CLI behavior.

## Scope

`flights.py` and its tests. Update README/TASK/task-data only where behavior changes, leaving
the final consistency sweep to T14. No endpoint changes, no optional T07/T08 work, and no live
network in tests.

## Review findings

### 1. FX failure can storm the network in local views

- `_load_archive_prices()` calls `convert_prices()` for every archived offer
  (`flights.py:1092`). For non-NOK fares, `get_conversion_rate()` retries the FX endpoint on
  every call when a stale fallback exists (`flights.py:658-690`), even after logging that it
  is using the cached rate.
- The current archive can therefore produce hundreds of `requests.get()` attempts and
  hundreds of warning lines for one `--calendar` or `--trends` invocation. With a 15s timeout,
  a supposedly local view can hang for a long time and contradicts the practical expectation
  of `--calendar`/`--trends` being local-only (`README.md:87`).
- T09 validated stale-cache fallback but did not prevent repeated refetches within a process.

### 2. Valid JSON with the wrong shape can crash state and source paths

- `_read_json_file()` checks only JSON validity and the top-level container type
  (`flights.py:190-210`). Nested schema drift is not caught.
- Confirmed crash paths include:
  - `update_price_map()` can raise `KeyError` on malformed fare stats; the surrounding handler
    catches `OSError/TypeError/ValueError`, not `KeyError` (`flights.py:982-993`).
  - `print_price_map()` can raise `ValueError` on a direction key without `_`
    (`flights.py:1018-1019`).
  - `print_price_trends()`/calendar can raise `KeyError` or `ValueError` on malformed fare or
    date data (`flights.py:1248-1270`).
  - `fetch_flights()` can raise `AttributeError` for a non-dict cache entry
    (`flights.py:2163-2169`).
  - `validate_config()` can raise `AttributeError`/`TypeError` for non-dict cookies
    (`flights.py:474-491`).
  - `layouts.json` is loaded at import with no validation; invalid JSON or a valid-wrong-shape
    file can crash `classify_layout()` and even `--help` (`flights.py:116-134`). Validate
    container shapes only (`aircraft`/`by_flight` maps), not layout value domains, so existing
    data is preserved.
- A corrupt offers cache can be quarantined twice: once by `load_offers_cache()` and again by
  `save_offers_cache()` (`flights.py:1532`), creating duplicate `.corrupt-*` files.

### 3. Structurally malformed 200 payloads are cached

- `fetch_flights()` writes the cache immediately after `resp.json()` succeeds
  (`flights.py:2202-2203`). `main()`/`_fetch_and_parse()` only parse/validate afterward
  (`flights.py:2252-2260`, `flights.py:3246-3250`).
- A valid-JSON response with a malformed top-level payload is therefore cached. Same-day
  retries without `--nocache` keep returning the bad payload and failing parse.
- T09 explicitly avoids caching non-JSON responses but does not cover valid-JSON/wrong-shape.

### 4. Return-direction booking links use the wrong departure date

- Scan/drift RB rows carry the outbound date in `_dep_date` so OB/RB pairs can be matched;
  that is correct for pairing but not for a standalone return link.
- `print_booking_links()` builds a round-trip URL from each row's `origin`, `destination`,
  `_dep_date`, and `_ret_date` (`flights.py:2913-2925`). For BKK→OSL rows this produces
  `fromStation=BKK&toStation=OSL&departing=<outbound date>`, which is not the originating
  OSL→BKK round trip. Drift summary uses the same reversed-route pattern
  (`flights.py:2909`).

### 5. Numeric flags and default dates are weakly validated

- `--top`, `--max-duration`, `--drift`, `--drift-ret`, `--ob`, `--rb`, `--min-trip`, and
  `--recommend` are plain `int` arguments with no range checks (`flights.py:2365-2383`).
  Negatives can cause empty/inverted scans or dropped results; some zero values silently fall
  back to defaults (`flights.py:3045-3046`).
- README claims broadly that invalid CLI values exit 2 (`README.md:98`), which is not true for
  these flags.
- `SEARCH` defaults are hardcoded past dates (`flights.py:31-32`), and a bare live run searches
  them. Date validation checks format/order only (`flights.py:3021-3030`).

## Steps

1. **FX/offline safety.**
   - Memoize a failed FX fetch in-process when a stale fallback is used (for example, mark
     the in-memory `_currency_rates[key]` entry with a failure sentinel). Check the sentinel
     before disk/FX lookups; subsequent calls for that currency reuse the fallback without
     network access and without rewriting `currency.json`.
   - Ensure `_load_archive_prices()`/local views emit at most one warning per currency per run
     and do not retry a known-failing endpoint per row.
   - Keep the existing stale-rate fallback behavior and successful same-day cache behavior.
   - Do not memoize “no fallback” failures when doing so would break existing tests that
     simulate sequential failed payloads; clear the sentinel when `_currency_rates` is
     cleared.
   - Tests: stale `currency.json` + blocked FX endpoint; assert one attempt per currency,
     fallback reuse, successful local view completion, and no warning flood.

2. **Schema-safe persisted/source JSON.**
   - Add narrow validators for offers cache, archive entries, price map, currency cache,
     config headers/cookies, and `layouts.json`.
   - On invalid nested schema, quarantine when appropriate or fail closed with a clear
     `LocalStateError`; never crash with `KeyError`/`AttributeError`/`ValueError` from display
     or update paths.
   - Validate `layouts.json` at load and fall back to empty maps with a warning instead of
     crashing import/`--help`; validate only container types and preserve existing entries.
   - Prevent double quarantine of the same offers-cache file, for example by not
     re-quarantining the merge read in `save_offers_cache()` after `load_offers_cache()` has
     already handled it.
   - Tests: valid-JSON/wrong-shape cases for each file, including `update_price_map()`,
     `print_price_map()`, `print_price_trends()`, `fetch_flights()`, `validate_config()`, and
     `layouts.json`; assert quarantine/fail-closed behavior and single quarantine copies.

3. **Malformed 200 cache policy.**
   - Do not cache a response unless its payload passes the parser's minimum top-level shape:
     a dict whose `flightOffers` value, if present, is a list. Keep caching accepted responses,
     including `{}`/missing `flightOffers` and an empty list, because `parse_results()` treats
     those as valid empty results.
   - Ensure a malformed payload still raises a typed `RequestFailed`, remains uncached, and
     does not trigger a cooldown or price-map update.
   - Tests: valid JSON with malformed top-level shapes; assert exit 1 at `main()` level, no
     cache write, no price-map write, no cooldown, no traceback; also assert later same-day
     runs do not reuse the bad payload.

4. **Direction-correct booking links.**
   - Keep `_dep_date`/`_ret_date` as the pair keys for OB/RB matching; do not change that
     pairing contract.
   - Build every booking link from the current search route (`SEARCH["origin"]` →
     `SEARCH["destination"]`) plus the pair's `_dep_date`/`_ret_date`. Do not use an RB row's
     reversed `origin`/`destination`, which produces BKK→OSL links for an OSL→BKK trip.
   - Apply the same rule to `print_drift_summary()` and mixed-cabin link output.
   - Tests: assert `fromStation=OSL`, `toStation=BKK`, `departing=<outbound>`, and
     `returning=<return>` for OB, RB, scan, drift, and non-scan output; explicitly assert that
     RB rows do not generate BKK→OSL links.

5. **CLI numeric and default-date hardening.**
   - Use the existing `_non_negative_int` for `--ob`, `--rb`, `--drift`, `--drift-ret`, and
     `--min-trip`; use `_positive_int` for `--top`, `--max-duration`, and `--recommend`.
     Preserve zero only where it is meaningful (`--ob 0`, `--rb 0`, `--drift 0`,
     `--drift-ret 0`, `--min-trip 0`).
   - Replace the hardcoded past default dates with runtime-derived rolling defaults (for
     example, departure = today + 30 days, return = departure + 18 days). Explicit
     `--depart`/`--ret` still override. Update `--help`/README to describe the rolling
     defaults rather than fixed dates.
   - Update the broad “invalid CLI values exit 2” claim to match the validated numeric flags
     and date behavior.
   - Tests: negative/zero flag cases, rolling default freshness (patch the clock), explicit
     date success, and help text consistency. Use temp/offline fakes only.

6. **Documentation and status.**
   - Update README/TASK/task-data for changed behavior and defaults; leave the final
     cross-document sweep to T14.
   - Record the final test count and mark T15 Done in this file, `task-data.md`, and `TASK.md`.
   - Keep T14 and T06 statuses unchanged until their own work is complete.

## Acceptance criteria

- A stale FX cache with an unreachable endpoint does not cause repeated network attempts or a
  warning flood; local views complete promptly.
- Valid-JSON/wrong-shape state/config/layouts inputs do not crash; they are quarantined or
  fail closed with a clear message, and only one corrupt copy is produced.
- Structurally malformed 200 payloads are not cached and produce a typed transient failure.
- Booking links always use the search route and the pair's outbound/return dates; RB rows do
  not generate reversed-route links.
- Numeric flags reject invalid values consistently, and rolling default dates cannot be in
  the past.
- All tests are offline and the full suite passes; docs reflect the shipped behavior.
- T14 can perform its final docs/test sweep against stable behavior, and T06 can sign off.

## Notes

- No code comments (repo rule).
- Do not run git commands.
- Do not delete/rename files.
- No live network, FX fetches, or flight scans in tests.
- Prefer minimal validation and bounded retries over broad rewrites.

## Execution log

- FX: `_currency_rates` now stores a private fallback-failure marker when a stale rate is used
  after a failed fetch. Later calls for that currency reuse the stale rate without disk/network
  retries or rewriting `currency.json`; clearing `_currency_rates` clears the memo. No-fallback
  failures remain unmemoized. Tests cover one FX attempt per currency, local `--calendar`/
  `--trends` archive loading, one warning per currency, and unchanged stale cache files.
- Schema safety: added narrow container validators for offers cache, currency cache, and price
  map; defensive nested checks for archive, price-map update/display, and trend/calendar views;
  and config header/cookie shape checks. `layouts.json` is validated at import (container types
  only), falls back to empty maps with a warning, and `classify_layout()` tolerates invalid
  globals. Quarantine copies are deduplicated by file signature, and `save_offers_cache()` no
  longer re-quarantines its merge read.
- Malformed 200s: `_valid_offers_payload()` requires a dict with a list-valued `flightOffers`
  when present; accepted `{}`/missing or empty lists still cache. Malformed payloads raise
  `RequestFailed(200, ...)` before any cache write. Tests cover lower-level fetch and non-scan
  `main()`, including no traceback/cache/price-map/cooldown writes and later same-day retries.
- Links: `print_booking_links()` and `print_drift_summary()` now always use
  `SEARCH["origin"]`/`SEARCH["destination"]` with the pair's `_dep_date`/`_ret_date`;
  `merge_cabins()` preserves those pair keys for drift summary. Tests assert URL query values
  for OB, RB, scan, drift, and non-scan output and that RB rows never reverse the route.
  The existing Rich-markup booking-link test was updated to put markup in the rendered dates,
  because route-derived row text is intentionally no longer part of the link line.
- CLI: `--ob`, `--rb`, `--drift`, `--drift-ret`, and `--min-trip` use non-negative validation
  (zero preserved); `--top`, `--max-duration`, and `--recommend` require positive values.
  Defaults are rolling (`today + 30` and `departure + 18`), with explicit dates overriding.
  Help/README describe the rolling defaults and actual numeric ranges.
- Docs/status: README, TASK, task-data, and this task are updated. T15 is Done; T14 and T06
  remain Pending.
- Verification: `python -m unittest discover -s tests -v` -> 200 tests OK;
  `python -m py_compile flights.py` OK; `python flights.py --help` exits 0. All tests use
  temp paths/fakes; no live network, FX, or flight scans.
