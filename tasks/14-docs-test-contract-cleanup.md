# T14 — Documentation and test-contract cleanup

- **Required**: Yes
- **Status**: **Done** (2026-10-26)
- **Depends on**: T09, T10, T11, T12, T13, T15
- **Blocks**: T06
- **Reference**: `../task-data.md` §0 (index), §3–§11; post-T05, post-T13, and post-T15 review findings

> 2026-10-26 — re-reviewed after T13 and the T14/T06 review. T15 is now Done and the full
> suite is at 200 tests. T15 shipped the residual behavior fixes (FX retry storm,
> schema-unsafe JSON, malformed-payload caching, return-link dates, CLI/default dates); T14
> now documents stable behavior. T14 remains a docs/contract sweep plus two test audits (T09
> main-level non-JSON response and T05 retry limiter accounting).
>
> This note records the pre-T14 baseline (200 tests); the completed sweep is logged below.

## Objective

Make the written contracts match the shipped behavior and close any remaining regression-test
gaps not already covered by T09–T13 or T15, so T06 can sign off against accurate docs and a
complete suite.

## Scope

`README.md`, `TASK.md`, `task-data.md`, `tasks/*.md`, and the test suite. No new endpoint or
feature work. Behavior changes belong in T09–T13 and T15; this task documents and tests the
final shipped behavior.

## Review findings

### Already resolved or owned elsewhere — verify, do not duplicate

- T11/T12 already documented and tested corrupt rate state, `--clear-block`, JSON-on-stdout
  with links on stderr, scan `--urls`, local-only modes without config, all-transient exit
  codes, return-failure price-map updates, and typed CLI validation.
- T13 already shipped send-time forbidden-header filtering, atomic/locked config writes with
  corrupt-file quarantine, `_abck` invalid/unknown/stale warnings, and bounded error-body/
  reference extraction. Its README/TASK/task-data updates are present; verify accuracy rather
  than reimplementing.
- T15 shipped the post-T13 behavior fixes: FX failure memoization/local-view safety, nested
  JSON schema validation, malformed-200 cache policy, direction-correct booking links, and
  numeric/default-date CLI hardening. T14 must not duplicate those fixes; verify their final
  docs and tests.
- Existing tests already cover:
  - T05 abort result shape, `update_price_map()` exactly once, zero-partial abort, and
    non-scan outbound-partial rendering (`tests/test_scan_abort.py`);
  - JSON/`--urls` partial output, all-transient scans/drifts, cache-hit/skipped-date
    accounting, and config-free local modes (`tests/test_cli_output.py`);
  - T04 `--force`, `--clear-block`, corrupt/disk-failure state, cooldown extension, and
    post-sleep rechecks (`tests/test_rate_limiter.py`);
  - T02 provenance rejection and headers-only acceptance (`tests/test_paste.py`);
  - T09 parsing/FX edge cases and T10 atomic/quarantine/merge behavior
    (`tests/test_parsing.py`, `tests/test_currency.py`, `tests/test_persistence.py`);
  - T13 header filtering, config quarantine, cookie persistence, `_abck` states, and bounded
    summaries (`tests/test_http_client.py`, `tests/test_paste.py`).
- T10's `.data_state.lock` and `*.corrupt-*` conventions are already in `task-data.md` §7 and
  `.gitignore`; verify no drift.
- TASK's related-documents line already covers T01–T15, and T04's “Depends on” line already
  matches `task-data.md`. Verify after T15; do not rewrite them unnecessarily.

### Remaining documentation contradictions

- **README setup vs parser.** README “Obtaining request headers” still says the
  `request.headers` object is available via “Copy as fetch” or the Headers panel.
  `parse_pasted_headers()` accepts a raw request line (optional) plus `Header: value` lines;
  it rejects non-POST/wrong-endpoint provenance and does not parse a fetch object/JSON.
  Document the exact accepted format with an example, and state that a fetch/JSON object is
  not accepted.
- **README flags/defaults.**
  - `--ob N`/`--rb N` still say “Scan N dates”; the code scans **±N days** around the fixed
    date.
  - `--min-trip` (default 8 days) and `--weight {price,duration,balanced}` are missing.
  - `--prune-cache` should state that entries dated before today are archived, current/future
    entries remain, and the archive is deduped/trimmed to 365 days.
  - Verify T15's numeric/default-date and FX local-view notes, plus the T11/T12 notes already
    present (JSON/URLs, local-only modes, CLI validation, corrupt rate state); add a brief
    state-file/corrupt-quarantine note if still absent.
- **TASK.md status/remediation drift.** The post-T05 remediation table still lists T09/T10 as
  “High” despite both being Done. Issue #10 is now correctly `Fixed (T13)`. Update the T09/T10
  severity/status cells, then verify the top status and revision history.
- **TASK.md stale implementation descriptions.**
  - Phase 1.0 cites `flights.py:158-191`; replace with function names/anchors.
  - `HttpClient` is described as returning `(status, json|text)`; it returns a
    `requests.Response`, while `fetch_flights()` raises typed errors or returns
    `(data, status, error)`.
  - `--no-impersonate` and the `impersonate` config field are T07-only optional design, not
    shipped behavior. Mark them optional/pending or remove them from the current contract.
  - TASK §6.4 lists `Accept-Encoding` as a browser-like header to send, while
    `task-data.md` §4 and `_sanitize_headers()` drop stored `Accept-Encoding`; point §6.4 at
    the library-default rule instead (`TASK.md:350-353`, `flights.py:302-328`).
  - TASK §7/§9 still describe `--budget` as a future optional cap, but it is shipped and
    defaults to unlimited; reword as shipped/default-off and let T06 calibrate only if
    evidence warrants.
  - TASK §12/§13 still describe the pre-T09–T12 design. Either update acceptance/test-plan
    wording to shipped behavior (JSON+URLs, corrupt local state, local modes, CLI validation,
    send-time header filtering) or label those sections historical and point to T06's
    checklist.
  - Historical test counts/line references in execution logs are historical; ensure none are
    presented as the current suite size.
- **T01 contradiction.** It says both “load the cache once at function entry” and “`_nocache`
  skips reading.” Clarify: the cache is always loaded for merge-on-write; `_nocache` skips
  only the hit check, and `cache = {}` would erase existing entries.
- **T03/T05 implementation logs superseded by T13.** T03's `_persist_cookies()` description
  and T05's 200-char summary/no-bound reference extraction predate T13's skip-on-corrupt
  behavior, 300-char summary, and 16K reference scan. Add concise “clarified by T13” notes;
  preserve the historical execution logs.
- **T04 implementation log superseded by T11.** T04's corrupt-state fallback and
  `record_block()` wording predate T11's fail-closed state handling and non-shortening
  cooldown behavior. Add a concise “clarified by T11” note; preserve the historical log.
- **T13 behavior after implementation.** T13 is Done. Verify README/TASK/task-data describe
  the shipped send-time forbidden-header filtering, atomic/quarantine-safe config persistence,
  stale `_abck` warning, and bounded error-body/reference handling. Check the exact bounds
  (8K scan prefix, 300-char summaries, 16K reference scan) and that staleness is described as
  config `saved_at`-based, not cookie-timestamp-based.
- **T06 coverage/status.** The T14/T06 review added `Status: Pending` and T14/T15/T01–T05/
  T09–T15 coverage to T06. Verify T06 consumes T14's final inventory and does not duplicate
  test additions.
- **task-data.md code map.** It now includes the T13 helpers but still omits shipped helpers
  such as `_atomic_write_json()`, `_read_json_file()`, `_quarantine_corrupt()`,
  `_locked_data()`, `LocalStateError`, `RateLimiter`, `HttpClient`, `make_trace_headers()`,
  `build_referer()`, `classify_block()`, `load_offers_cache()`/`save_offers_cache()`,
  `archive_stale_cache()`, `print_price_map()`, `print_json()`/`print_json_combined()`,
  `_report_scan_failures()`, `get_rate_limiter()`/`configure_rate_limiter()`/
  `reset_rate_limiter()`, and the T12 failure/JSON-output helpers. Add current helpers only.
- **task-data.md §7/§8.** The config schema still shows the unused `impersonate` field;
  clarify it is T07-only. The error taxonomy now includes corrupt-config quarantine; verify
  it also reflects bounded error summaries and local-state errors.

### Remaining test gaps to audit

T13 owns its header/config/cookie/provenance/bounded-body tests; T15 added its own. The
pre-T14 baseline was 200 tests (final post-T14 count is in the execution log). T14 should not
duplicate either set. The remaining unowned
gaps are:

- **T09 main-level non-JSON response.** Lower-level `fetch_flights()`/`_fetch_and_parse()`
  tests exist, but there is no non-scan `main()` test showing a 200 non-JSON outbound response
  exits 1 with no traceback, no cache write, and no cooldown. Add it.
- **T09 malformed JSON payload.** T15 already added the non-scan `main()` behavior and
  no-reuse test for malformed 200 payloads. Verify it covers exit 1, no traceback, no
  price-map write, no cooldown, and no cache write; do not assert the old cached-payload
  behavior.
- **T05 retry accounting.** `RetryTests` currently assert only `fetch_flights()` call counts;
  add a limiter-accounting assertion proving both live attempts go through `wait()`/
  `record_call()`, while hard edge/auth/rate/budget errors never retry.
- **T03 cache-hit side effects.** Covered by existing tests: `tests/test_http_client.py` and
  `tests/test_scan_abort.py` verify cache hits skip network, limiter waits/record-calls, and
  `_persist_cookies()`. Verify only; do not duplicate.

## Steps

1. **Preflight after T15.**
   - Confirm T15 is Done and read its execution log. Run
     `python -m unittest discover -s tests -q` and `python -m py_compile flights.py`.
   - Inventory tests against the T01–T13 and T15 test lists. Mark each as covered or missing
     before writing code; do not duplicate coverage or rewrite historical execution logs.
   - Record both the pre-T14 suite count and the final post-T14 count.
2. **README.**
   - Replace the “Copy as fetch” setup with the exact accepted raw-header format and a short
     example; note that headers-only paste is accepted but unverified, and that fetch/JSON
     objects are not parsed.
   - Correct `--ob`/`--rb` and `--prune-cache`; add `--min-trip` and `--weight`; verify
     T11/T12/T15 behavior notes and document corrupt local-state recovery briefly.
   - Keep examples offline/local-safe; do not imply live calibration calls are safe.
3. **TASK.**
   - Update the T09/T10 remediation rows to Done (issue #10 is already Fixed (T13)); verify
     T15's row/status and T06 dependency are already Done/consistent.
   - Replace stale line references with function/section anchors; fix the `HttpClient` return
     contract; mark `--no-impersonate`/`impersonate` as T07-only optional.
   - Correct §6.4 `Accept-Encoding` wording and §7/§9 `--budget` as shipped/default-off.
   - Update or explicitly historicalize §12/§13, and ensure old test counts/line refs are
     labelled historical.
4. **task-data.**
   - Verify the T09–T15 index and T06 dependency after T13/T14/T15 status changes.
   - Reconcile §7 (config/T07 field, lock/quarantine files, `currency.json` as cache) and §8
     (corrupt config, bounded summaries, local-state errors).
   - Add shipped helpers to §10, including the cache/archive/price-map/limiter/JSON helpers;
     verify §4/§5/§12 and test conventions.
5. **Existing task docs.**
   - Fix T01's cache-reading contradiction.
   - Verify T04's dependency line and T06's prerequisite/dependency references; leave the final
     T06 checklist content to T06. Add a T04 “clarified by T11” note.
   - Add concise “Superseded/clarified by T09/T11/T12/T13/T15” notes to T03/T05 where later
     tasks changed the contract, including T13's config-skip, bounded-summary, and
     reference-scan behavior; preserve historical execution logs.
6. **Test-contract gaps.**
   - Add the T09 non-JSON main-level test and the T05 retry limiter-accounting test. Verify
     T15's malformed-JSON main-level/no-reuse test instead of duplicating it. Do not add the
     T03 cache-hit test; existing tests already cover it.
   - Verify T06's dependency/reference lines include T14/T15 and the final suite count; leave
     the final checklist itself to T06.
   - Keep all tests offline with temp paths, fake clocks/sessions, and no real
     `config.json`/cache/price-map/rate-state access.
   - Run `python -m unittest discover -s tests -v`, `python -m py_compile flights.py`, and
     `python flights.py --help`.
7. **Final consistency and status.**
   - Search for stale flag names, line refs, test counts, `T13/T14/T15` statuses, and any claim
     of unimplemented behavior.
   - Confirm T06 owns the final sign-off checklist and references T14's inventory; do not
     duplicate that checklist here.
   - Mark T14 Done in this file, `task-data.md`, and `TASK.md`; leave T06 Pending.

## Acceptance criteria

- README setup instructions work with the actual parser.
- All flags/defaults/documented behaviors match the code, including T13 and T15 behavior.
- README/TASK/task-data/task docs have consistent statuses, dependencies, and current
  references; no unimplemented flag/config field is presented as shipped.
- The T09–T13 and T15 listed tests are present and runnable, plus the genuinely unowned T14
  gaps.
- The full suite passes and both the pre-T14 baseline and final post-T14 count are recorded;
  no live network is used.
- T06 can run against accurate docs with no known documentation contradictions.

## Notes

- No code comments (repo rule).
- Do not delete historical execution logs; label them as historical where counts/line refs
  have drifted.
- Do not use git commands.
- No behavior/feature changes here; T13 and T15 are Done, so reconcile their documented
  behavior rather than reimplementing it or re-asserting pending status.

## Execution log

- 2026-10-26 — completed the docs/test-contract sweep. Pre-T14 baseline: 200 tests passing;
  final post-T14 suite: 203 tests passing.
- README: replaced “Copy as fetch” with the accepted raw-header format (optional `POST`
  request line plus `Header: value`; allowlisted `Cookie`; headers-only paste unverified;
  fetch/JSON objects rejected) and an example; corrected `--ob`/`--rb` to ±N days; added
  `--min-trip` (default 8) and `--weight` (default price); corrected `--prune-cache`
  (pre-today entries archived, current/future retained, deduped/365-day trim); added a
  corrupt-state/quarantine note. Verified T15 rolling-date, numeric-validation, and stale-FX
  local-view notes.
- TASK: T09/T10 remediation rows now Done, T14 Done, T06 pending; replaced the stale
  `flights.py:158-191` reference with parser anchors; corrected the `HttpClient`
  `requests.Response` contract and `fetch_flights()` typed-error/`(data, status, error)`
  contract; marked `--no-impersonate`/`impersonate` T07-only optional/unshipped; corrected
  §6.4 `Accept-Encoding` to the library-default rule and `_sanitize_headers()` dropping;
  reworded §7/§9 `--budget` as shipped/default-off/unlimited; updated §12/§13 to shipped
  behavior with historical notes and a T06 pointer; labelled revision-history counts
  historical.
- task-data: verified the T09–T15 index and T06 dependency; annotated `impersonate` as
  T07-only; reconciled §6 budget, §7 state safety/schema guards, §8 bounded errors/local
  state/CLI taxonomy; added the shipped persistence, limiter, HTTP, JSON, cache/archive,
  failure-report, and singleton helpers to §10.
- Task docs: fixed T01 so the cache is always loaded for merge-on-write and `_nocache` skips
  only the hit check; added “clarified by T13” notes to T03 `_persist_cookies` and T05
  bounded-summary/reference wording; added a “clarified by T11” note to T04 corrupt-state/
  `record_block()` wording. Historical execution logs preserved; T06 checklist content not
  edited.
- Tests: added a non-scan main-level non-JSON 200 test proving exit 1, no traceback, no
  cache/price-map write, no cooldown/`record_block`, and one attempted POST; added T05 retry
  limiter-accounting tests using real `fetch_flights()` with fake sessions/temp cache,
  proving both transient attempts call `wait()`/`record_call()` and that edge/auth/rate/budget
  never retry. Did not duplicate T15's malformed-JSON test or T03 cache-hit coverage.
- Verification: `python -m unittest discover -s tests -v` -> 203 tests OK;
  `python -m py_compile flights.py` OK; `python flights.py --help` exit 0. Checksums for
  `config.json`, `offers_cache.json`, `price_map.json`, `currency.json`, and
  `cache_archive.json` were unchanged by the suite; no `.rate_state.json` was created. No git
  commands were run.
- Post-T14 follow-up: the test suite was consolidated from 203 to 115 tests by merging
  overlapping cases into parameterized/subTest groups; no behavior area was dropped. See the
  `TASK.md` revision history for the later count.
- Residual risks: the raw-header example is illustrative (the real session values must be
  pasted); live calibration/default changes remain T06 work; T07/T08 stay optional and
  Pending. T06 remains Pending.
