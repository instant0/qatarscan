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
`print_booking_links()`, `_fetch_and_parse()` failure accounting, `main()` dispatch, and
`--max-price` parsing. Tests and the affected README sections. No endpoint or parsing changes
(T09 owns those).

## Review findings addressed

- `console.print(json.dumps(...))` parses Rich markup from API values.
- `--json` followed by `--urls` mixes JSON and human-readable links on stdout.
- `--urls` is ignored in scan mode.
- Local-only `--price-map`/`--prune-cache`/`--calendar`/`--trends` exit before running when
  no `config.json` exists.
- An all-transient scan exits 0.
- Non-scan return `RequestFailed` renders the outbound side but does not update the price map.
- `--max-price` parsing can raise an uncaught `ValueError`.

## Steps

1. **Rich markup safety**
   - Render JSON with markup disabled, e.g.
     `console.print(json.dumps(...), markup=False, highlight=False)`.
   - Ensure API-derived table/list cells are rendered as literal text (`Text` or
     `rich.markup.escape`) and styled explicitly.
   - Error text continues to use the T05 sanitized/escaped logging path.
   - Add tests with values containing `[bold]`, `[/]`, and brackets in aircraft/airport names;
     assert literal output and no exception.
2. **JSON and URLs**
   - When `--json` and `--urls` are combined, keep stdout valid JSON and send booking links to
     stderr (or reject the combination with a clear argparse error if stderr is not desired).
   - Apply the same rule to partial/abort JSON output; never interleave table text on stdout.
   - Document the chosen behavior in README.
3. **Scan URLs**
   - After scan/partial output, call `print_booking_links()` when `--urls` is set.
   - Ensure results carry `_dep_date`/`_ret_date` (or make the link builder fall back safely).
   - In JSON mode, links go to stderr per step 2.
   - Add a test that scan results produce links and that missing return dates are skipped.
4. **Local modes without config**
   - Dispatch `--price-map`, `--prune-cache`, `--calendar`, and `--trends` before
     `load_config()`/`validate_config()`, or make config optional for those branches.
   - `--prune-cache` must still run archive/prune logic even when no browser config exists.
   - Paste/scan/live modes continue to require a valid config.
   - Add tests for local dispatch with `load_config` returning `None`.
5. **Exit status for transient failures**
   - Track attempted failed calls (at minimum `RequestFailed` and `data is None`) in the scan
     path.
   - If a scan produces no results and every attempted live call failed transiently, exit 1
     with a clear warning; do not print the normal “No results after scan + filtering” success
     path.
   - If some calls succeed, keep exit 0 but report the failed-call count.
   - Do not count cache hits or skipped dates as failures.
6. **Non-scan return failure**
   - On a return `RequestFailed`, call `update_price_map(ob_results)` before rendering/exiting,
     matching the other abort paths.
   - Do not write a partial return result that was never parsed.
7. **CLI validation**
   - Parse `--max-price` with a helper that validates `CABIN=positive_int` pairs, rejects
     unknown cabins/empty values, and exits with a clear message instead of an uncaught
     `ValueError`.
   - Validate `--budget`/`--delay` are non-negative/positive as appropriate; leave defaults
     unchanged.
   - Add unit tests for valid/invalid values.

## Acceptance criteria

- JSON output is valid and untouched by Rich markup or URLs.
- `--urls` works consistently in single, drift, and scan modes.
- Local-only commands run without `config.json`.
- An all-transient scan exits non-zero; mixed success/failure reports the failures.
- Non-scan return failures contribute the outbound results to the price map.
- Invalid CLI values produce a clear error, not a traceback.
- Existing tests plus the new CLI/output tests pass.

## Notes

- No code comments (repo rule).
- Keep `_dep_date`/`_ret_date` annotations internal and excluded from JSON.
- Do not change the API payload or scan/abort semantics; this task is output/control-flow only.
