# T12 — CLI, output, and local-mode correctness

- **Required**: Yes
- **Status**: **Pending**
- **Depends on**: T03, T05
- **Blocks**: T06, T14
- **Reference**: `../task-data.md` §8 (errors), §10 (code map), §11 (tests);
  post-T05 review M1/M9/M10

## Objective

Make output and exit behavior predictable: API-controlled text must not be interpreted as Rich
markup, machine-readable JSON must stay valid when `--urls` is requested, scan modes must
honor `--urls`, local-only commands must not require `config.json`, and transient-failure runs
must not report success.

## Scope

`flights.py`: `print_json()`, `print_json_combined()`, table/list renderers,
`print_booking_links()`, `_fetch_and_parse()` failure accounting, scan helper plumbing,
`parse_args()`/`main()` dispatch, and `--max-price`/`--budget`/`--delay` validation. Tests and
the affected README sections. No endpoint or parsing changes (T09 owns those).

## Review findings addressed

- `console.print(json.dumps(...))` parses Rich markup from API values.
- `--json` followed by `--urls` mixes JSON and human-readable links on stdout.
- `--urls` is ignored in scan mode.
- Local-only `--price-map`/`--prune-cache`/`--calendar`/`--trends` exit before running when
  no `config.json` exists.
- An all-transient scan exits 0.
- Non-scan return `RequestFailed` renders the outbound side but does not update the price map.
- `--max-price` parsing can raise an uncaught `ValueError`.
- `--budget 0` is documented as unlimited but currently exhausts the budget immediately.
- Return `data is None` and return parse failures also fail to update the outbound price map.
- Dynamic values in price-map/trends/summary/link renderers also need literal-text handling.
- Drift/drift-ret silently ignore `--json`.

## Resolved decisions

- `--json --urls`: keep stdout as pure JSON; send booking links to **stderr**. Do not reject the
  combination.
- Human-readable mode: booking links stay on stdout.
- Drift/drift-ret remain human-readable. Reject `--json` with `--drift`/`--drift-ret` with a
  clear parser error (exit 2) instead of silently ignoring it.
- Scan/drift failure accounting uses a per-run mutable counter object threaded through the scan
  helpers; no module-global counters.
- `--budget 0` means unlimited, matching README. Negative budget is invalid.
- `--delay` must be positive. Defaults remain unchanged.
- `--max-price` cabins are the keys of `SCAN["max_price"]` (currently `ECONOMY`, `BUSINESS`),
  case-normalized to uppercase. Duplicate cabins use the last value, preserving current
  override behavior.

## Steps

1. **Rich markup safety**
   - Render JSON with markup disabled:
     `console.print(json.dumps(...), markup=False, highlight=False)` in both JSON renderers.
   - Render all user/API-derived text as literal text (`Text` or `rich.markup.escape`).
     At minimum cover:
     - `print_compare_table()`: dates, routes, durations, flight numbers, aircraft, layouts,
       times, fare labels/codes/prices, seat counts, and `filter_note`.
     - `print_drift_pair_table()`: date labels and every cell string.
     - `print_mixed_table()`: combo labels, date pairs, leg strings, and totals.
     - `print_drift_summary()`: dynamic values and booking URLs.
     - `print_booking_links()`: origin, destination, dates, and URL.
     - `print_price_map()`, `print_price_calendar()`, `print_price_trends()`: origin/destination
       labels, flight identifiers, fare-class names, and other stored/API-derived text.
   - Keep locally generated styling markup explicit; escape only interpolated dynamic values so
     table/status colors continue to work.
   - Error text continues to use the T05 sanitized/escaped logging path.
   - Add tests with values containing `[bold]`, `[/]`, `[red]`, an unknown bracket tag, and an
     unclosed `[`; assert literal output and no exception.
2. **JSON and URLs**
   - Add an output selector to `print_booking_links()` (for example `to_stderr=False`) that uses
     the stderr console (`log`) when requested.
   - Pass `to_stderr=True` whenever JSON is being emitted: single-request success,
     `display_outbound_partial()`, and scan full/partial output.
   - Never print table text or links on stdout after JSON.
   - Reject `--json` with `--drift`/`--drift-ret` in `parse_args()` via `parser.error(...)`.
   - Document the JSON/stderr and drift/JSON behavior in README.
3. **Scan URLs**
   - After scan output (including partial output before an abort log), call
     `print_booking_links(all_results)` when `--urls` is set and results exist.
   - In JSON mode, links go to stderr per step 2.
   - Ensure results carry `_dep_date`/`_ret_date`; keep the existing safe skip when either date is
     missing.
   - Add tests that scan results produce links, partial results produce links, JSON mode keeps
     stdout parseable, and missing return dates are skipped.
4. **Local modes without config**
   - Dispatch in this order: `--clear-block`; optional `--paste`; local-only modes; then
     `load_config()`/`validate_config()` for live calls.
   - Move `--price-map`, `--prune-cache`, `--calendar`, and `--trends` before `load_config()`.
   - `--prune-cache` must call `archive_stale_cache()` and log the result itself; keep the normal
     auto-archive behavior for live runs.
   - `--calendar`/`--trends` may combine and must not require browser headers/cookies.
   - Paste/scan/live modes continue to require a valid config.
   - Add tests for local dispatch when `load_config()` returns `None`, including prune creating
     the archive/pruning path and calendar/trends rendering.
5. **Exit status for transient failures**
   - Thread a per-run stats dict such as `{"calls": 0, "failures": 0}` through
     `_scan_dates()`, `scan_outbound()`, `scan_return()`, `drift_scan()`, and
     `drift_scan_return()`, then into `_fetch_and_parse(..., stats=None)`.
   - In `_fetch_and_parse()`, increment `calls` once per invocation. Increment `failures` on
     `RequestFailed`, `data is None`, and parse `RequestFailed`. Abort exceptions
     (`EdgeBlock`/`AppAuthError`/`RateLimited`/`BudgetExceeded`) propagate and are not counted
     as transient failures.
   - Cache hits count as successful calls, never failures. Skipped dates do not increment
     `calls`.
   - After a scan/drift run:
     - If `0 < failures < calls`: report `N/M requests failed` on stderr and keep exit 0 unless
       an abort already requires exit 1.
     - If `calls > 0 and failures == calls`: print a clear all-requests-failed warning and exit
       1. Do not print the normal “No results after scan + filtering” success path.
     - If `calls == 0`: preserve the current no-results/skipped-dates behavior.
   - Apply the same logic to outbound scan, return scan, drift, and drift-ret.
   - Add tests for all-failed scans/drifts (no abort), mixed success/failure, all-cache-hit, and
     all-skipped dates.
6. **Non-scan return failure**
   - On return `RequestFailed`, return `data is None`, or return parse `RequestFailed`, call
     `update_price_map(ob_results)` before rendering/exiting, matching the other abort paths.
   - Do not write a partial return result that was never parsed.
   - Keep edge/auth/rate/budget behavior unchanged and update the price map exactly once.
   - Add tests for all four failure paths.
7. **CLI validation**
   - Parse `--max-price` with a helper used as an argparse `type`, raising
     `argparse.ArgumentTypeError` for:
     - missing `=`, empty cabin, empty value, non-integer value, and value `<= 0`;
     - cabins outside `SCAN["max_price"]`.
   - Normalize cabin names to uppercase; let duplicate cabins use the last value.
   - Validate `--budget` as non-negative; treat `0` as unlimited when configuring the limiter.
     Update argparse help to say “0 = unlimited”.
   - Validate `--delay` as positive.
   - Invalid values must exit with argparse’s clear error (code 2), not a traceback.
   - Add tests for valid/invalid `--max-price`, negative/zero budget, zero/negative delay, and
     `--budget 0` reaching the limiter as unlimited.
8. **README**
   - Document JSON-on-stdout/links-on-stderr, scan `--urls`, local modes without config,
     transient-failure exit behavior, drift/JSON rejection, `--max-price` rules, and budget/delay
     validation.

## Acceptance criteria

- JSON output is valid and untouched by Rich markup or URLs; links are on stderr in JSON mode.
- All renderers show API-controlled bracket text literally.
- `--urls` works consistently in single, drift, and scan modes; scan partials are covered.
- `--json` with drift/drift-ret fails clearly instead of being ignored.
- Local-only commands run without `config.json`; paste/live modes still require a valid config.
- An all-transient scan or drift exits non-zero; mixed success/failure reports the failures;
  cache hits and skipped dates do not cause failure exits.
- Non-scan return failures contribute the outbound results to the price map.
- Invalid CLI values produce a clear error, not a traceback; `--budget 0` is unlimited.
- Existing tests plus the new CLI/output tests pass.

## Verification

```sh
python -m unittest discover -s tests -q
python -m py_compile flights.py
python flights.py --help
```

No live scans or network calls are required for T12.

## Notes

- No code comments (repo rule).
- Keep `_dep_date`/`_ret_date` annotations internal and excluded from JSON.
- Do not change the API payload or scan/abort semantics; this task is output/control-flow only.
- New tests should use patches/temp files and must not touch the real `config.json`, cache, or
  rate-state files.
