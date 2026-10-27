# T13 — Request-header, config, and cookie hardening

- **Required**: Yes
- **Status**: **Done** (2026-10-26)
- **Depends on**: T02, T03, T10
- **Blocks**: T06, T14
- **Reference**: `../task-data.md` §4 (headers), §5 (cookies), §7 (config), §8 (errors);
  post-T05 review M5/M7/M8

## Objective

Enforce the header policy at request time, make `config.json` updates durable and
non-destructive, surface stale/invalid `_abck` state, and bound error-body handling so a
polluted/old config cannot send forbidden headers or wipe saved credentials.

## Scope

`flights.py`: `HttpClient`, `_persist_cookies()`, `load_config()`/`save_config()`,
`_abck_state()`/`validate_config()`, `_safe_body_summary()`, and related tests. T10 owns the
shared atomic JSON helper, `.data_state.lock`, and quarantine conventions; T09 owns response
parsing.

## Review findings addressed

- Banned headers are filtered at paste time but stored config headers are applied without
  re-filtering at request time.
- `save_config()` is a direct whole-file write; `_persist_cookies()` can replace headers with
  `{}` if config is corrupt.
- `_abck` state is recorded/printed but stale/invalid state is not warned about, despite
  issue #10 being marked fixed.
- `_safe_body_summary()` and `_edge_reference()` can process unbounded response bodies.
- T02 has no test asserting that non-`flight-offers` provenance is rejected/recorded.

## Steps

1. **Request-time header policy**
   - Define the forbidden-at-send set: `Host`, `Content-Length`, `Connection`, `TE`,
     `Transfer-Encoding`, `Accept-Encoding`, and any other T02-dropped header.
   - In `HttpClient.__init__`/`post_offers()`, re-filter stored headers, not just parsed
     headers. Keep the required app/auth and browser-like headers.
   - Drop non-string/empty values; do not mutate the caller's dict.
   - Add a test seeding `config.json` with `Host`, `Content-Length`, and `Accept-Encoding`,
     then assert they are absent from the outgoing session headers and UA/Referer remain.
2. **Config persistence**
   - Use T10's `_atomic_write_json()` and data lock for `save_config()`.
   - `_persist_cookies()` must preserve existing headers/other keys when config is valid;
     never write a headers-to-`{}` replacement.
   - If config is missing, a minimal cookie-only config may be created, but it must not be
     treated as a validated browser session.
   - If config is corrupt/unreadable, quarantine it via the T10 helper, warn, and skip
     automatic cookie persistence; do not replace it with a headers-less file. The next live
     run requires `--paste`.
   - `save_config()` from an explicit `--paste` may replace the quarantined config after
     writing a fresh complete header/cookie set.
   - Keep `saved_at`, `source_endpoint`, `last_abck_state`, and unknown keys where possible.
   - Do not log or print cookies/secrets.
3. **`_abck` state**
   - Keep the existing `valid|invalid|unknown` detection, but make `validate_config()` warn
     when state is `invalid` or `unknown` or when the saved `_abck` is stale; include an
     actionable `--paste` hint.
   - Persist the state on paste/validation without overwriting `saved_at` incorrectly.
   - Update the TASK.md issue-table wording after implementation (see T14 for the doc sweep).
   - Add tests for valid/invalid/unknown/missing `_abck`.
4. **Bounded error bodies**
   - Bound `_safe_body_summary()` to a small one-line summary (for example 300 chars) after
     HTML stripping/collapsing.
   - Bound the input scanned for an Akamai reference, or search only the bounded summary.
   - Ensure `RequestFailed` retains only the bounded message, not the full body.
   - Add tests with a very large HTML/JSON body and a reference number near the start/end.
5. **Paste provenance tests**
   - Add T02 coverage for a non-`flight-offers` path/host and for a headers-only paste with an
     unknown endpoint; assert the documented accept/warn/reject behavior and
     `source_endpoint`.
6. **Cookie policy**
   - Verify only allowlisted cookies are persisted; `_persist_cookies()` merges `Set-Cookie`
     updates without dropping unrelated config keys.
   - Cache hits still avoid cookie writes (T03 acceptance).
   - Add a test for a failed cookie write: scan continues with a warning, and the next run is
     not silently left with a truncated config.
   - Add a test for a corrupt config: it is quarantined, automatic cookie persistence is
     skipped, and an explicit `--paste` can replace it.

## Acceptance criteria

- Forbidden headers are never sent even if present in an old/polluted `config.json`.
- `config.json` writes are atomic; corrupt config cannot wipe saved headers.
- Stale/invalid/unknown `_abck` produces an actionable warning.
- Error summaries and reference extraction are bounded and sanitized.
- T02 provenance and T03 header/cookie acceptance gaps are covered by tests.
- Existing tests remain green.

## Notes

- No code comments (repo rule).
- Never print cookie values, tokens, or full request headers.
- Do not change the header allowlist semantics from T02; enforce the same policy at send time.
- T14 owns the final README/TASK wording; this task may update behavior docs it directly
  touches.

## Execution log

- Added a send-time header sanitizer (`FORBIDDEN_SEND_HEADERS`, `_sanitize_headers()`) used by
  `HttpClient.__init__()` and again in `post_offers()`. It drops `Host`, `Content-Length`,
  `Connection`, `TE`, `Transfer-Encoding`, `Accept-Encoding`, and `Cookie` case-insensitively,
  plus non-string/empty values, without mutating caller headers. Required auth/browser headers,
  fresh trace headers, and caller-supplied Referer behavior are preserved.
- `HttpClient` now seeds the session with only allowlisted cookies and merges only allowlisted
  response `Set-Cookie` values back into the caller cookie dict; `_persist_cookies()` also
  filters to the T02 allowlist before writing.
- `save_config()` now uses `_locked_data()` + `_atomic_write_json()`. `_persist_cookies()` reads
  and merges into a valid config, preserving headers, `source_endpoint`, `saved_at`, unknown
  keys, and refreshing `last_abck_state`; missing config creates a cookie-only file. Corrupt or
  unreadable config is quarantined, warned about, and automatic cookie persistence is skipped.
  Explicit saves (including `--paste`) replace the quarantined config with a fresh complete
  config. Failed cookie writes warn and leave the prior config intact.
- `load_config()` uses the shared guarded reader (corrupt config is quarantined, then treated as
  missing). `validate_config()` warns on invalid/unknown/stale `_abck` with a `--paste` hint and
  persists the detected state through `_save_abck_state()`, which updates only
  `last_abck_state` and preserves `saved_at`.
- Bounded body handling: `_safe_body_summary()` scans at most 8192 chars, detects HTML markers
  there, and returns at most 300 collapsed chars; `_edge_reference()` scans at most 16384 chars;
  `RequestFailed` now retains only the bounded summary (not the full body).
- Tests added/extended in `tests/test_paste.py` and `tests/test_http_client.py` for polluted
  stored headers at send time, non-mutating header filtering, allowlisted cookie merging,
  provenance (absolute URL, wrong path/host, headers-only unknown host), valid/invalid/unknown/
  missing/stale `_abck`, state persistence with unchanged `saved_at`, preserve/merge config,
  corrupt quarantine + skipped auto-persist, explicit `--paste` replacement, failed cookie writes,
  missing config, cache-hit no cookie write, and large HTML/JSON summary/reference bounds. All tests use
  temp paths/fake sessions and no network.
- Updated `README.md` where config/header behavior is user-facing, plus `TASK.md` and
  `task-data.md` status rows.
- Verification: `python -m unittest discover -s tests -v` -> 175 tests OK;
  `python -m py_compile flights.py` OK; `python flights.py --help` exits 0. No live scans or
  network calls.
