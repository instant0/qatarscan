# T05 — Scan integration: abort-on-block, partial results, UX

- **Required**: Yes
- **Status**: **Done** (2026-10-26)
- **Depends on**: T03, T04
- **Blocks**: T06
- **Reference**: `../task-data.md` §6 (pacing), §8 (errors), §9 (definitions),
  §10 (code map), §11 (tests)

## Objective

Wire the typed HTTP errors and the rate limiter into the scan paths so the tool stops on the
first hard failure, records a cooldown only for an edge block, prints a clear message, and
preserves/renders the results already collected.

## Scope

`flights.py`: `fetch_flights()`, `_fetch_and_parse()`, a transient-retry helper, the three scan
functions, the non-scan single-request branch, abort helpers, and `main()`. Also CLI flags,
README, tests, and status docs. No new endpoint or payload.

## Review decisions (supersede earlier wording)

1. **Abort is unconditional.** Do not add `--abort-on-block` / `--no-abort-on-block`. Any
   edge block, app auth error, active cooldown, or exhausted budget always stops the run. A
   flag that can disable this is unsafe and encourages extending the block. This supersedes
   the `--abort-on-block` bullet in `TASK.md` §8.3.
2. **Four abort reasons**: `edge`, `auth`, `rate`, `budget`. Only `edge` records a new
   cooldown. `rate` means a cooldown is already active; do not overwrite it.
3. **Transient failures do not abort.** `RequestFailed` (HTTP 5xx / timeout / network) is a
   failed call: optionally retry once, then skip that date/leg and continue. It never triggers
   a cooldown.
4. **`--max-retries` is transient-only.** Default 0, allowed values 0 or 1. Never retry
   `EdgeBlock`, `AppAuthError`, `RateLimited`, or `BudgetExceeded`. This replaces the earlier
   "retry after `blocked_until`" wording, which would either sleep through a 15–30 min
   cooldown or ignore it.
5. **Scan functions own one `update_price_map()` call** and must hand `main` the same result
   shape they would return normally. On abort they finalize the partial results exactly like
   the normal tail, then raise `ScanAborted` with those finalized partials. `main` never calls
   `update_price_map()` a second time.
6. **Pacing is already inside `fetch_flights()` (T04).** Remove the old scan sleeps and their
   cache-hit guards, but do not add another `wait()`.
7. **Errors are sanitized.** Never print raw HTML. `EdgeBlock` carries an optional edge
   reference number; `RequestFailed` stores a one-line, HTML-stripped summary. Escape rich
   markup when logging error text.

## Steps

### 1. Exceptions and abort helpers

- Extend `EdgeBlock`:
  ```python
  class EdgeBlock(Exception):
      def __init__(self, message, status=None, reference=None):
          self.status = status
          self.reference = reference
          super().__init__(message)
  ```
- Add `ScanAborted(Exception)`:
  - `__init__(self, results, reason, cause=None)`
  - `self.results = list(results or [])`, `self.reason`, `self.cause`.
- Add `_abort_reason(exc) -> str` mapping:
  `EdgeBlock -> "edge"`, `AppAuthError -> "auth"`, `RateLimited -> "rate"`,
  `BudgetExceeded -> "budget"`; re-raise anything else.
- Add `_abort_message(aborted) -> str` with these one-line messages:
  - edge: `"Blocked at Akamai edge (<ref if present>) — wait 15–30 min or refresh with
    `--paste`."`
  - auth: `"Session rejected by the API — refresh with `--paste`."`
  - rate: `"Rate-limit cooldown active — about N min remaining. Use `--force` for this run
    or `--clear-block` to reset."` (read `cause.remaining_seconds` when available)
  - budget: `"Live-call budget exhausted (N calls). Re-run later or raise `--budget`."`
  - fallback: `"Scan aborted."`
- Add `_record_block_for_abort(aborted)`:
  - only when `aborted.reason == "edge"` → `get_rate_limiter().record_block(SCAN["block_minutes"])`
  - no-op for auth/rate/budget.
- Add `_log_error(text)` using `rich.markup.escape` so a body containing `[...]` cannot inject
  markup.
- Add `SCAN["block_minutes"] = 20` (15–30 band; T06 may calibrate).

### 2. `fetch_flights()` hardening

- Keep T01 cache semantics, T03 cookie persistence, trace headers, classification, and the
  `(data, status, error)` success contract.
- Wrap the POST:
  ```python
  try:
      resp = client.post_offers(payload, referer)
  except requests.RequestException as e:
      raise RequestFailed(0, str(e)) from e
  finally:
      limiter.record_call()
  _persist_cookies(cookies)
  ```
  `record_call()` still runs for timeouts/failures. Do not call `_persist_cookies()` when
  there is no response.
- On an edge block, extract the reference before raising:
  - `_edge_reference(body)` uses `re.search(r"Reference #([A-Za-z0-9.]+)", body or "", re.I)`.
  - Message: `"Akamai edge block (HTTP 403, ref <ref>)"` or without the ref when absent.
  - Pass `status` and `reference` to `EdgeBlock`. Never include the body.
- Sanitize `RequestFailed`:
  - `_safe_body_summary(body)` collapses whitespace, truncates to 200 chars, and returns
    `"HTML response body (omitted)"` when the body looks like HTML (`<html`, `<!doctype`,
    `Access Denied`, `edgesuite`).
  - For `status == 0`, use `"Network error: <summary>"` rather than `"HTTP 0"`.
- Add `import re` and `from rich.markup import escape`.

> Clarified by T13: the shipped bounds are an 8K scan prefix (`BODY_SCAN_LIMIT`), a 300-char
> summary (`BODY_SUMMARY_LIMIT`), and a 16K reference scan (`EDGE_REFERENCE_LIMIT`); the
> original 200-char/unbounded wording above is historical.

### 3. Transient retries and `_fetch_and_parse()`

- Add `SCAN["max_retries"] = 0`; `main()` sets it from the CLI (0 or 1).
- Add `_fetch_with_retries(headers, cookies, origin, destination, departure_date,
  referer=None)`:
  - Call `fetch_flights()` up to `1 + SCAN["max_retries"]` times.
  - Retry only `RequestFailed` with `status == 0` or `status >= 500`.
  - Never retry edge/auth/rate/budget; let those propagate.
  - Log one dim/yellow line before each retry.
- `_fetch_and_parse()`:
  - Use `_fetch_with_retries()`.
  - Catch only `RequestFailed`; print one sanitized red line and return `[]` (the scan
    continues).
  - Let `EdgeBlock`, `AppAuthError`, `RateLimited`, and `BudgetExceeded` propagate.
  - Keep parsing, FX conversion, `_dep_date`/`_ret_date`/`_direction`, and the offer count log
    unchanged.

### 4. Scan-function refactor (`_scan_dates`, `drift_scan`, `drift_scan_return`)

Use one pattern in all three functions so the normal and aborted paths share the same tail:

```python
aborted = None
try:
    for ...:
        ...
        results = _fetch_and_parse(...)
        all_results.extend(results)
except (EdgeBlock, AppAuthError, RateLimited, BudgetExceeded) as exc:
    aborted = exc

update_price_map(all_results)          # exactly once
all_results = filter_dirt_cheap(all_results)

# normal finalization, identical for normal and aborted paths
final = ...
if aborted is not None:
    log.print(f"[yellow]Scan aborted; showing {len(final)} partial result(s).[/yellow]")
    raise ScanAborted(final, _abort_reason(aborted), aborted) from aborted
return final
```

- `_scan_dates`: `final` is `all_results` when `raw=True`; otherwise it is
  `filter_results(...)` + `score_results(...)[:top_n]`. Keep the existing empty-filter
  behavior, but suppress the "No results passed filters" message when aborting.
- `drift_scan` / `drift_scan_return`: `final` is `[]` when there are no results; otherwise
  `all_results` when `args.nofilter`, else `filter_results(...)`. Suppress the empty-filter
  message when aborting.
- On abort, the raised `ScanAborted.results` must have the **same shape** the function would
  return normally. This lets `main` render partials without re-running `update_price_map()`.
- Do not catch `RequestFailed` here; `_fetch_and_parse()` already turns it into `[]`.
- Do not include an aborted date's partial results if `_fetch_and_parse()` returned `[]`.

### 5. Pacing cleanup and UX

- Delete the legacy `time.sleep(delay)` blocks and the `delay` locals in `_scan_dates()`,
  `drift_scan()`, and `drift_scan_return()`.
- Delete the now-unused `_is_cache_hit()` helper and its cache-hit delay guards after
  confirming with `grep` that no caller remains.
- T04 remains the only pacing point (`fetch_flights()` on a cache miss).
- Add minimal UX inside `RateLimiter.wait()` and `record_call()` (small T04 adjustment):
  - Before sleeping, log `[dim]Rate-limit pause Ns[/dim]`.
  - When `budget` is set, log `[dim]Live calls: X/N[/dim]` after recording an attempt.
- `--delay` continues to configure the limiter only.

### 6. `main()` abort handling

- Configure `SCAN["max_retries"]` from `args.max_retries` (clamp/validate 0–1).
- **Drift branches** (`--drift`, `--drift-ret`):
  - Wrap the scan call in `try/except ScanAborted`; use `e.results` as `all_results`.
  - If `e.reason == "edge"`, call `_record_block_for_abort(e)` **before** rendering.
  - Render any partial results with the existing drift rendering path
    (`print_drift_pair_table`, `print_drift_summary`, `find_best_mixed`, `print_booking_links`
    as applicable).
  - If aborted, print `_abort_message(e)` via `_log_error()` and `sys.exit(1)` **even when
    there are zero partial results**. Do not early-return with exit 0.
- **Scan branches** (`--scan`, `--ob`, `--rb`):
  - Wrap the outbound/return calls in one `try/except ScanAborted`.
  - On abort, `all_results.extend(e.results)`; do not attempt the second direction if the
    first aborted.
  - Record a block for `edge` before rendering.
  - Render the normal filtered/scored table (or JSON) with the partials. If there are no
    partials, still print the abort message and exit 1.
- **Non-scan single-request branch** (no scan/drift):
  - Replace the direct `fetch_flights()` calls with `_fetch_with_retries()`.
  - On outbound `EdgeBlock`/`AppAuthError`/`RateLimited`/`BudgetExceeded`: assign
    `aborted = ScanAborted([], _abort_reason(e), e)`, call
    `_record_block_for_abort(aborted)` (no-op except edge), print `_abort_message(aborted)`,
    exit 1.
  - On outbound `RequestFailed`: print the sanitized transient message, exit 1; no cooldown.
  - On return hard failure: call `update_price_map(ob_results)` once (the normal combined call
    has not run), then build/record `aborted` as above, render the outbound results (table:
    outbound table only; JSON: `print_json_combined({"outbound": ..., "return": []})`;
    `--urls`: outbound links only), then print `_abort_message(aborted)` and exit 1.
  - On return `RequestFailed`: render the successful outbound partials, print the sanitized
    transient message, exit 1; no cooldown.
  - On success: keep the existing combined render and the single
    `update_price_map(ob_results + rb_results)` call.
- Never print raw HTML or a traceback. Use `_log_error()` for all typed-error messages.
- Do not call `update_price_map()` a second time for partials; the scan function already did.

### 7. CLI and docs

- Add `--max-retries {0,1}`, default 0, help: "Retry transient 5xx/timeout failures once
  (edge/auth/rate/budget are never retried)."
- Do **not** add `--abort-on-block`.
- Keep T04's `--delay`, `--budget`, `--force`, `--clear-block`.
- Update README with `--max-retries`.
- Verify `TASK.md` §8.3 no longer lists `--abort-on-block` and describes the transient-only
  retry (this review already updated it).
- Add `_fetch_with_retries`, `ScanAborted`, and `_record_block_for_abort` to the
  `task-data.md` §10 code map if that map is kept current.
- After implementation, mark T05 Done in `task-data.md` and this file, add an execution log,
  and mark TASK.md issues #6, #7, #8, #14 as `**Fixed (T05)**`.

### 8. Cookie/cache guarantees

- Keep `_persist_cookies(cookies)` after every response, before classification/raising, so a
  block response still persists refreshed cookies.
- Successful calls cache in `fetch_flights()` immediately; a later block must not undo them.
- Partial results are written to the price map exactly once by the scan function.

## Acceptance criteria

- A simulated edge block on call 3 stops the run; calls 4+ are not attempted.
- Results from calls 1–2 remain visible and are written to the price map/cache.
- A simulated JSON 401/403 aborts with `reason="auth"` and does **not** set a cooldown.
- An active cooldown aborts with `reason="rate"` and does **not** overwrite `blocked_until`.
- An exhausted budget aborts with `reason="budget"` and does **not** set a cooldown.
- No raw Akamai HTML appears in output; edge messages contain at most a reference number.
- A run with only cache hits does not consume budget, sleep, or touch the rate state.
- A transient 5xx/timeout does not abort the scan; with `--max-retries 1` it is retried once,
  and edge/auth/rate/budget are never retried.
- Partial results are rendered and the process exits non-zero after any abort, including when
  zero results were collected.
- The old scan-loop sleeps and `_is_cache_hit()` are gone; `fetch_flights()` is the only
  pacing point.

## Tests (new `tests/test_scan_abort.py`)

- Edge block on the 3rd fake `_fetch_and_parse` call: assert the loop stops at 3 calls,
  `ScanAborted.results` has the first two results, `reason == "edge"`, and
  `update_price_map` ran exactly once. Repeat for `drift_scan` and `drift_scan_return`.
- Same setup with `AppAuthError` / `RateLimited` / `BudgetExceeded`: assert the correct
  reason and that `_record_block_for_abort` records a block only for edge (never for
  auth/rate/budget).
- Main-level abort behavior: edge records `record_block` and exits 1; auth/rate/budget do not
  record; zero partial results still exits 1 after printing the message.
- Non-scan path: outbound success + return `EdgeBlock` renders the outbound partials and calls
  `update_price_map(ob_results)` exactly once; outbound `RequestFailed` exits 1 with no
  cooldown.
- `_abort_message()` for each reason, including rate remaining minutes and budget count.
- `_fetch_with_retries()`: transient 500 retried once then succeeds; edge/auth/rate/budget
  are never retried; non-retryable 4xx is not retried; a transient failure keeps the scan
  going when retries are exhausted.
- `fetch_flights()` wraps `requests.Timeout` in `RequestFailed(status=0)` and still calls
  `record_call()`.
- Edge response containing HTML and `Reference #18.abc.123`: `EdgeBlock.reference` is set and
  neither `str(e)` nor the logged output contains `<html`.
- `RequestFailed` with an HTML body logs only `"HTML response body (omitted)"`.
- Cache-hit path does not call `limiter.wait()` / `record_call()` (keep the T04 assertions).
- No scan function contains a live `time.sleep()` call (grep or source assertion is
  acceptable).
- Keep all existing tests green.

## Verification

- `python -m py_compile flights.py`
- `python -m unittest discover -s tests -v`
- `python flights.py --help` (new flag present; no abort-on-block flag)
- `python -c "import flights"`
- Do not run live scans during this task.

## Notes

- The goal is a clean abort, not bypassing Akamai. No immediate retries of edge/auth/rate.
- Use UTC for any timestamps; do not persist secrets in logs or state.
- No code comments (repo rule). Do not run git commands. Do not delete/rename files.
- T04 contract: `fetch_flights()` already paces; T05 removes the old sleeps and must not add
  another `wait()`.

## Execution log

- Added `ScanAborted`, extended `EdgeBlock` with `status`/`reference`, `_abort_reason`, `_abort_message`, `_record_block_for_abort`, `_log_error`, `_edge_reference`, `_safe_body_summary`, and `SCAN["block_minutes"]`/`SCAN["max_retries"]`.
- Hardened `fetch_flights()`: timeout/network `requests.RequestException` -> `RequestFailed(0, ...)`, `record_call()` always in `finally`, cookie persistence only when a response exists, edge reference extraction, and sanitized `RequestFailed` summaries (HTML omitted).
- Added `_fetch_with_retries()` (transient 0/5xx only) and updated `_fetch_and_parse()` to catch only `RequestFailed`, log one sanitized line, and return `[]`; typed aborts propagate.
- Refactored `_scan_dates`, `drift_scan`, and `drift_scan_return` to finalize partials once (`update_price_map`, dirt-cheap/filter/scoring) and raise `ScanAborted` with the same shape as a normal return. Removed legacy sleeps, delay locals, and `_is_cache_hit()`.
- Added limiter UX: pause line before sleeping and budget usage after `record_call()`.
- Restructured `main()`: unconditional abort handling for drift/scan/non-scan paths, edge-only cooldown, outbound partial rendering on return failure, exit 1 even with zero partials, and no duplicate `update_price_map()` for scan partials.
- Added `--max-retries {0,1}` (default 0); no `--abort-on-block`. Updated README and `task-data.md` §10. Marked TASK.md issues #6/#7/#8/#14 fixed.
- Added `tests/test_scan_abort.py` covering scan/drift aborts, reasons, cooldown scope, main exits, non-scan partials, retries, timeout wrapping, reference extraction, HTML omission, cache-hit limiter behavior, and scan-loop sleep removal.
- Verification: `python -m py_compile flights.py` OK; `python -m unittest discover -s tests -v` -> 64 tests OK; `python flights.py --help` shows `--max-retries` and no abort-on-block flag; `python -c "import flights"` OK. No live scans run.
