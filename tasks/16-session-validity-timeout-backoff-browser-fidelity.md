# T16 — Session validity, timeout circuit-breaker, backoff, and browser fidelity

- **Required**: Yes (P0/P1); browser-fidelity experiments are bounded and opt-in
- **Status**: **Pending**
- **Depends on**: T05, T11, T13
- **Blocks**: T06 (calibration should not run until P0/P1 land)
- **Reference**: `../task-data.md` §4 (headers), §5 (cookies), §6 (pacing), §8 (errors);
  `tasks/07-optional-tls-impersonation.md`, `tasks/08-optional-playwright-warmup.md`;
  failed run evidence 2026-10-27

> 2026-10-27 — added after repeated `ReadTimeout` failures during a 5+5 scan. The run
> failed despite the same request shape that previously returned JSON. Investigation shows
> the decisive variable is **session validity**, not header count.

## Landed ahead of the plan

- `--paste` alone now saves `config.json` and exits without a live query; combining it with
  other arguments continues into the requested action (`flights.py:_paste_only()`).
  `--main`/`main()` now pass `sys.argv[1:]` explicitly so this can be detected reliably, and
  the README/help text say so. Tests: `tests/test_cli_output.py::PasteOnlyTests`.
- From the real Edge capture: `Sec-CH-UA`, `Sec-CH-UA-Mobile`, `Sec-CH-UA-Platform`, and
  `Priority` are now captured; `bm_*` (including `bm_mi`), `ASLBSA`, and `ASLBSACORS` are now
  in the cookie allowlist. Tests cover the new capture and keep analytics dropped.
  Docs updated in `task-data.md` §4/§5 and `tests/test_paste.py`.
- `parse_pasted_headers()` now also accepts the Edge/Chromium two-line header list
  (`name` line, value line), HTTP/2 pseudo-headers, and `Name:value` without a space; it
  rejects JSON/HAR with a targeted hint. README documents the accepted formats.

## Objective

Stop wasting live calls on a known-dead session, fail fast and cool down on repeated
read timeouts, add bounded adaptive backoff, and close the realistic browser-fidelity gaps
(TLS/HTTP2, stale sensor cookies, compression) without blindly stripping headers.

## Evidence

### Header history (git)

| Revision | Headers kept | Cookies | Transport / trace | Pacing |
|---|---|---|---|---|
| `c26cb7a` (initial) | 6 explicit (`session-id`, `x-assigneddeviceid`, `nbx_fs_api_key`, `request-id`, `traceparent`, `qr-lang`) + defaults (`User-Agent`, `Accept`, `Content-Type`, `Origin`, `Referer`) = ~11 | **all** cookies, no allowlist | raw `requests.post`, **reused stale** `Request-Id`/`traceparent` from the paste, static `Referer` | 10–20s sleep, no cooldown, no retry |
| current | **22** allowlisted headers (added `Sec-CH-UA*`, `Priority`); `Host`/`Content-Length`/`Connection`/`TE`/`Transfer-Encoding`/`Accept-Encoding`/`Cookie` filtered | allowlist (`_abck`, `bm_*`, `ak_bmsc`, `AKA_A2`, `ASLBSA*`, `TS0*`, `BIGipServer*`) | `requests.Session`, **fresh** trace pair per POST, date-accurate `Referer` | 45s base, 40–55s jitter, persisted across runs, 20-min cooldown on edge block |

Conclusion: we send more headers now than the initial version, but they are all standard
browser headers, and the older version was *less* realistic (stale trace IDs, stale `Referer`,
no session jar). There is no evidence that the extra headers caused the regression.

### Captured real request (Edge 151, 2026-10-27, HTTP/2, 200 OK)

The capture confirms the current model and identifies exactly what we were missing:

| Browser header | Ours before | Decision |
|---|---|---|
| `:authority`, `:method`, `:path`, `:scheme` | not stored | HTTP/2 pseudo-headers; transport generates them |
| `accept`, `accept-language`, `content-type`, `origin`, `pragma`, `cache-control`, `qr-lang`, `referer` | captured | keep |
| `sec-fetch-dest/mode/site` | captured | keep |
| `session-id`, `x-assigneddeviceid`, `nbx_fs_api_key`, `user-agent` | captured | keep; auth-critical |
| `request-id`, `traceparent` | regenerated per POST | keep (format matches capture) |
| `sec-ch-ua`, `sec-ch-ua-mobile`, `sec-ch-ua-platform` | **dropped** | now captured (Chromium/Edge sessions only) |
| `priority: u=1, i` | **dropped** | now captured (meaningful on h2) |
| `accept-encoding: gzip, deflate, br, zstd` | dropped | capability-gated / `curl_cffi` |
| `content-length` | dropped | transport computes |
| `cookie` | allowlist subset | see below |

Payload: the capture sends both itineraries (return `isRequested: false`). `api.md` confirms a
second non-requested itinerary is ignored, so our one-itinerary payload is functionally
identical; adding the second is optional fidelity only.

Referer: the capture omits `sort=duration`; our `build_referer()` adds it. Harmless, but a
visible mismatch if the server ever inspects it.

Cookies in the capture: `TS01753a24`, `AKA_A2`, `ak_bmsc`, `country`, `ai_user`, `drcookie`,
`accepted_functional`, `accepted_marketing`, `country_geo`, `OptanonAlertBoxClosed`,
`at_check`, `AMCV_*`, `OptanonConsent`, `mbox`, `compStartSrc`, `ASLBSA`, `ASLBSACORS`,
`bm_mi`, `bm_sz`, `_abck`, `ai_session`, `bm_sv`. The important previously-dropped cookie is
**`bm_mi`** (Akamai Bot Manager). `_abck` starts `~0~` and also contains trailing `~-1`
segments; the server still returns 200, so the current "`~0~` present ⇒ valid" rule is correct.

Response confirms: `_abck`/`bm_sz`/`bm_sv` are refreshed via `Set-Cookie`, so persisting the
allowlisted jar is the right behavior; `content-encoding: br` only appears because the browser
offered `br`.

Profile note: the saved `config.json` UA is Firefox 158, while this capture is Edge 151. Do not
mix them. Re-paste from the browser/profile that is actually producing 200s, and keep
`User-Agent`, `Sec-CH-UA*`, and cookies from that same session.

### Failed-run state (local, 2026-10-27)

- `config.json`: `last_abck_state = "invalid"`, `saved_at = 2026-10-27T01:46:30`.
- `.rate_state.json`: calls around 00:08–00:14 succeeded at ~54s spacing; calls at 02:17 and
  02:19 are the timeout run. No active cooldown at that point.
- `ReadTimeout` means the TCP connection and request succeeded but the server withheld the
  response. That is a tarpit / bot-score / rate decision, not header rejection (which would be
  401/403 JSON) or an Akamai edge block (403 HTML). Header volume cannot explain it.
- `validate_config()` only prints a yellow warning for `invalid` `_abck` and proceeds to scan
  (`flights.py:634-655`). The run therefore burned its full call budget on a dead session.
- `saved_at` is rewritten by every `save_config()` call, including `_persist_cookies()`
  (`flights.py:358-362`, `flights.py:2400-2437`), so it is not the paste/session start time.
  The `>24h` staleness warning is therefore unreliable for an actively used config.

### Why a human in a browser appears faster

A human search reuses a live browser session whose `_abck`/`bm_sz` sensor state is
continuously refreshed by page JS and sensor beacons, over a browser TLS/HTTP2 fingerprint.
Our snapshot has an already-invalid `_abck`, no sensor refresh, and an OpenSSL/HTTP1.1
fingerprint. The server tarpits that session regardless of how few headers we send.

## Hypotheses, ranked

1. **Confirmed:** invalid/stale `_abck` + no hard gate. Fix first.
2. **Likely:** a frozen sensor snapshot decays; browser refreshes it continuously.
3. **Likely:** TLS/JA3/JA4 + HTTP/2 fingerprint mismatch (`requests` vs Firefox). T07.
4. **Possible:** per-session call-rate limit lower than our 45s base; needs bounded calibration.
5. **Possible:** cookie allowlist drops a functional (non-analytics) cookie the browser sends.
6. **Unlikely:** header count/volume. Trimming headers is expected to make bot scores *worse*,
   not better.
7. **Minor:** we strip `Accept-Encoding`; `requests` sends `gzip, deflate` while the UA claims
   Firefox 158 (which sends `gzip, deflate, br, zstd`).

## Scope

`flights.py`, tests, README/task-data where behavior changes. Optional fidelity work stays
behind flags and may reference T07/T08. No endpoint changes, no payload changes unless a
diagnostic diff proves a mismatch, no live experiments without a fresh valid paste and consent.

## Steps

### P0 — Hard session gate (do first)

1. Split `saved_at` semantics:
   - keep `saved_at` as "config last written" only;
   - add `pasted_at` (set by `--paste` only) and optionally `cookies_updated_at`;
   - base the `>24h` staleness warning on `pasted_at`, not `saved_at`; migrate old configs
     (`pasted_at` missing → fall back to `saved_at`).
2. Make `validate_config()` return a structured result instead of only mutating/warning.
3. In live paths (`--scan`, `--drift*`, single pair, `--recommend`, anything that can POST),
   **refuse to start** when `last_abck_state == "invalid"`:
   - message: saved session is invalid; open qatarairways.com, run a search that returns
     results, then `--paste` the new `flight-offers` request;
   - allow `--force` to override for diagnostics, with a loud warning;
   - keep offline commands (`--calendar`, `--trends`, `--price-map`, `--prune-cache`,
     `--clear-block`, cache-only views) working without a live session.
4. Treat `unknown` `_abck` as warn-only; treat missing `bm_sz`/`ak_bmsc` as warn-only unless a
   live call is about to run (then require an explicit `--force` or fresh paste).
5. Require a verified source for live use: either the pasted request line resolved to
   `flight-offers`, or the user passes `--force` for a headers-only paste. Warn that a paste
   from a sensor/analytics request is not a valid flight-offers session.
6. `--paste` should print the `_abck` state prominently and refuse to *save-and-use* an
   `invalid` paste unless `--force`; offer to re-copy from a working search.
   `--paste` alone now saves and exits (landed); the validity gate must still run on later
   live invocations.

### P1 — Timeout circuit-breaker

7. Distinguish failure modes in `RequestFailed`:
   - `status=0` currently covers connect/read timeout and connection errors; add a reason
     (`timeout`, `connect`, `network`) and whether it was a **read** timeout.
   - Use a `(connect, read)` timeout tuple in `HttpClient.post_offers()` rather than a scalar
     `timeout=30`: e.g. `(10, 30)`.
8. Add `SCAN["max_consecutive_timeouts"]` (default 2) and `--max-timeouts N`:
   - count consecutive `RequestFailed(status=0, reason=timeout)`;
   - on reaching N, abort the run using the existing abort machinery, preserve partial results,
     and print "server stopped responding (likely tarpit/invalid session); re-paste before
     retrying";
   - do not record a 20-min cooldown on a single timeout (avoid punishing flaky networks);
     after N consecutive timeouts, record a shorter cooldown (e.g. 5–10 min) or none, and make
     it configurable; decide in implementation and cover with tests;
   - reset the counter on any HTTP response (including 4xx/5xx).
9. Honor `Retry-After` for 429/503 (including HTML 503):
   - parse seconds or HTTP-date; record a cooldown of at least that value, capped (e.g. 30 min);
   - classify 429 distinctly from 5xx; never retry 429 in a tight loop.
10. Keep `--max-retries` semantics (default 0, one retry on 5xx/timeout) but ensure a retry
    still passes through `limiter.wait()` and the new backoff.

### P1 — Adaptive backoff

11. Add per-run exponential backoff with jitter for consecutive transient failures
    (timeouts/5xx/429): 60s, 120s, 240s cap (base configurable in `SCAN`).
    Apply `max(spacing, transient_backoff)` so it composes with the 45s limiter and is visible
    in the logs ("transient backoff 120s").
12. Do not change the 45s default in this task. T06 owns calibration; T16 only makes failure
    handling stop hammering.

### P2 — Browser fidelity (bounded, opt-in, evidence-driven)

13. **Header matrix experiment (diagnostic only).** With a fresh, verified-valid paste, run the
    same date pair three times under `--budget 1`:
    - A: current 18 headers;
    - B: required core (`Content-Type`, `Origin`, `Referer`, `qr-lang`, `nbx_fs_api_key`,
      `session-id`, `X-AssignedDeviceID`, `User-Agent`) + `Accept`;
    - C: core only.
    Record status, latency, and body class. Expected result: B/C do not reduce timeouts and may
    increase 403. Do **not** ship a smaller default unless the data says otherwise (it likely
    won't). Document the result; keep A as default.
14. **Cookie allowlist audit.** Cookies are already sent (the `Cookie` header is filtered from
    pasted headers, then `requests.Session` adds the allowlisted jar automatically; verify this
    with a unit test that inspects the outgoing prepared request). Capture the full `Cookie`
    list from a working browser `flight-offers` request and diff against `COOKIE_ALLOWLIST`.
    Add any functional (non-analytics) cookie needed for the session (e.g. `bm_mi`,
    `akavpau_*`, `JSESSIONID`, other `TS*`); keep analytics/marketing dropped per task-data
    §5. Add a unit test for the new allowlist entries.
15. **TLS/HTTP2 impersonation (T07).** Implement `backend="requests" | "curl_cffi"` with
    `curl_cffi.requests.Session(impersonate="firefox")`, UA matched to the profile, watch-only
    warning when the package is absent, and a `--impersonate` opt-in flag. This is the
    highest-value fidelity fix after P0. Do not mix a Firefox UA with a Chrome profile.
16. **Compression consistency.** When `brotli`/`zstandard` are installed (or on the
    `curl_cffi` backend), allow the browser-like `Accept-Encoding: gzip, deflate, br, zstd`;
    otherwise keep the library default. Add a test for the capability check.
17. **Playwright warm-up (T08).** Keep as a separate optional `tools/warm_session.py` that
    harvests fresh headers/cookies after one real search and writes a valid `config.json`;
    never part of the scan runtime, never bulk-scans.
18. **Reuse one `HttpClient`/`Session` per run.** `fetch_flights()` currently constructs a new
    `HttpClient` (and therefore a new TCP/TLS connection) for every date (`flights.py:2455`).
    Reuse a run-scoped session so cookies and connection state carry across calls, matching a
    browser tab. Keep per-request trace headers and date-accurate `Referer`.
19. **Payload parity (optional diagnostic).** Capture one real browser `flight-offers` POST
    body for the same route/date pair and diff it against `build_payload()`. If fields differ,
    file a follow-up; do not change the payload in this task without evidence.

### P3 — Docs

20. README: a short "timeouts vs 403 vs invalid session" troubleshooting section; explain
    `_abck` states, the new hard gate, `--force`, `--max-timeouts`, `--timeout`,
    `--impersonate`, and that header trimming is not a fix.
21. Update `task-data.md` §5/§6/§8 and T07/T08 status notes where behavior changes. Leave T06
    as the final calibration/sign-off owner.

## Acceptance criteria

- An invalid `_abck` in `config.json` blocks live calls before the first POST, with a clear
  paste recovery path; `--force` overrides; offline commands still work.
- A run with N consecutive read timeouts aborts, preserves partial results, and does not keep
  issuing calls; the message names the likely tarpit/invalid session.
- 429/503 `Retry-After` is honored and produces a cooldown, not a tight retry.
- Transient backoff is observable in logs and bounded; the 45s default is unchanged.
- `pasted_at` is stable across cookie persistence; the staleness warning is meaningful.
- Header-set/cookie-fingerprint experiments are documented; defaults change only if data
  supports it.
- `--impersonate` (if shipped here) uses a consistent UA/profile and falls back cleanly when
  `curl_cffi` is absent.
- Existing 200-test baseline remains green; new tests cover the added paths.

## Tests

- `validate_config()` / live-gate: invalid `_abck` blocks scan/drift/single-pair, allows
  offline views, `--force` overrides, headers-only paste warns, `flight-offers` paste passes.
- `pasted_at` migration and stability across `_persist_cookies()`.
- Timeout classification: read vs connect, consecutive counter reset on HTTP response,
  abort after N, partial-results preservation, no cooldown on a single timeout.
- `Retry-After` seconds and HTTP-date parsing, cap, and cooldown recording; 429 not retried.
- Backoff schedule with injected clock/rng; `max(spacing, backoff)` composition; logs.
- Timeout tuple plumbing (connect vs read) via a fake session.
- Cookie allowlist additions and forbidden-header filtering unchanged.
- Capability check for brotli/zstd and backend selection/fallback warning.
- No live network in tests; fake sessions and temp files only.

## Verification

- `python -m unittest discover -s tests -v`
- `python -m py_compile flights.py`
- `python -c "import flights"`
- `python flights.py --help`
- Live checks only with explicit consent, a fresh valid paste, `--budget`, and a stop at the
  first timeout/403. Never print full headers or cookie values.

## Notes

- No code comments (repo rule).
- Do not delete/move/rename files; do not run git commands while implementing (the history
  above is already captured).
- Do not strip `Sec-Fetch-*`, `Accept-Language`, or other browser-like headers as a "fix";
  that is expected to worsen bot scores.
- Do not raise the default timeout to mask a tarpit; a longer wait just burns wall-clock.
- Do not add a long cooldown on every timeout; only on repeated/consecutive timeouts or an
  explicit `Retry-After`.
- Do not ship a smaller header set without experiment data.
- Prefer reusing the existing `rate` abort path for the timeout breaker over adding a fifth
  abort reason, unless the scan-abort contract makes a distinct reason clearly better.
