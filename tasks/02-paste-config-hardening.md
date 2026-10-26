# T02 — Paste parsing & config hardening

- **Required**: Yes
- **Status**: **Done** (2026-10-25)
- **Depends on**: —
- **Blocks**: T03
- **Reference**: `../task-data.md` §4 (headers), §5 (cookies), §7 (config), §10 (code map)

## Objective

Accept only a valid QR `flight-offers` paste, preserve the real browser headers and the
Akamai/session cookies, and record provenance in `config.json`. Stop silently substituting a
wrong User-Agent or dropping required headers.

## Scope

`parse_pasted_headers()`, `prompt_paste_headers()`, `load_config()`, `save_config()` in
`flights.py`, plus a small `validate_config()` call in `main()`.

## Steps

1. **New types/constants**
   - `PasteError(ValueError)` for invalid pastes.
   - `REQUIRED_HEADERS = ("session-id", "X-AssignedDeviceID", "nbx_fs_api_key", "User-Agent")`.
   - `CANONICAL_HEADERS` map (case-insensitive key → stored casing) for:
     `session-id`, `X-AssignedDeviceID`, `nbx_fs_api_key`, `User-Agent`, `Accept`,
     `Accept-Language`, `Sec-GPC`, `Sec-Fetch-Dest/Mode/Site`, `Pragma`, `Cache-Control`,
     `Origin`, `Referer`, `qr-lang`, `Content-Type`, `Request-Id`, `traceparent`.
   - Cookie allowlist: `_abck`, `bm_sz`, `bm_sv`, `ak_bmsc`, `AKA_A2`, plus prefixes
     `BIGipServer` and `TS0`.
2. **Parse provenance**
   - Detect the request line (`POST <path> HTTP/2`). Accept only `POST` to
     `/dapi/public/bff/web/flight-search/flight-offers`; if an absolute URL is pasted, extract
     its path/host.
   - If a `Host` line is present, require `www.qatarairways.com`.
   - `source_endpoint = "flight-offers"` on a match; `"unknown"` if no request line
     (headers-only paste, allowed).
   - Reject mismatches with `PasteError`.
3. **Headers**
   - Store only canonical whitelisted headers.
   - Ignore `Host`, `Content-Length`, `Connection`, `TE`, `Accept-Encoding`.
   - Hard-fail if any `REQUIRED_HEADERS` is missing.
   - Keep `Request-Id`/`traceparent` for now; T03 will regenerate them per request.
4. **Cookies**
   - Split the `Cookie` header on `;` and keep only the allowlist.
   - Drop analytics/marketing cookies.
5. **Return contract**
   - `parse_pasted_headers(text) -> (headers, cookies, source_endpoint)`.
   - Update the only caller, `prompt_paste_headers()`.
6. **Persist**
   - `prompt_paste_headers()` builds and saves:
     `headers`, `cookies`, `user_agent`, `source_endpoint`, `last_abck_state`, `saved_at`.
   - `_abck_state(cookies)` returns `valid` (`~0~`), `invalid` (`~-1~`), or `unknown`.
   - Warn (do not fail) if `_abck`, `bm_sz`, or `ak_bmsc` is missing.
7. **Saved-config validation**
   - Add `validate_config(cfg) -> bool`:
     - missing required header → red error + return False;
     - missing Akamai cookie → yellow warning (still True);
     - `source_endpoint` present and not `flight-offers`/`unknown` → yellow warning.
   - Call it in `main()` after the analytics early-returns and before the scan; exit 1 on
     False.
   - Use `cfg.get("headers", {})` / `cfg.get("cookies", {})` in `main()` for safety.
8. **Backward compatibility**
   - Older `config.json` without `user_agent`/`source_endpoint`/`last_abck_state` still loads.
   - Older headers are accepted if the required keys are present.

## Acceptance criteria

- Pasting a valid `flight-offers` request saves the real UA and browser headers.
- Pasting a non-QR or non-flight-search request is rejected with a clear message.
- Missing `session-id`/`X-AssignedDeviceID`/`nbx_fs_api_key`/`User-Agent` is rejected.
- Analytics cookies are not stored; Akamai/session cookies are.
- `Host`, `Content-Length`, `Connection`, `TE`, `Accept-Encoding` are not stored.
- `source_endpoint` and `last_abck_state` are recorded.
- An old config loads; an invalid saved config exits with a clear `--paste` hint.
- `python flights.py --help` still works; the app can still read the current `config.json`.

## Tests

- Unit: valid fixture → UA preserved, allowlisted cookies kept, analytics dropped, provenance
  `flight-offers`, `_abck_state == "valid"`.
- Unit: wrong path/host → `PasteError`.
- Unit: missing required header (including UA) → `PasteError`.
- Unit: headers-only fixture (no request line) → `source_endpoint == "unknown"`, no error.
- Unit: canonical casing for `X-AssignedDeviceID`.
- Unit: `validate_config()` for valid, missing-header, and non-flight-offers provenance.
- Integration: `python -c "import flights"` and `python flights.py --help`.

## Execution log

- 2026-10-25 — added `PasteError`, canonical header map, cookie allowlist, request-line/Host
  validation, `_abck` state, and `validate_config()`; `parse_pasted_headers()` now returns
  `(headers, cookies, source_endpoint)`.
- Tests: `tests/test_paste.py` (8 tests) pass via
  `python -m unittest discover -s tests -v` (11 total, including T01).
- Backward compatibility: current `config.json` loads and validates; `python flights.py --help`
  OK.
