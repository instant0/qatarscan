# T14 — Documentation and test-contract cleanup

- **Required**: Yes
- **Status**: **Pending**
- **Depends on**: T09, T10, T11, T12, T13
- **Blocks**: T06
- **Reference**: `../task-data.md` §0 (index), §3–§11; post-T05 review doc/test findings

## Objective

Make the written contracts match the shipped behavior and close the remaining regression-test
gaps that are not owned by T09–T13, so T06 can sign off against accurate docs and a complete
suite.

## Scope

`README.md`, `TASK.md`, `task-data.md`, `tasks/*.md`, and the test suite. No new endpoint or
feature work. Behavior changes belong in T09–T13; this task documents and tests them.

## Review findings addressed

- README suggests “Copy as fetch” although `parse_pasted_headers()` expects raw
  `Header: value` lines / a request line.
- README says `--ob N`/`--rb N` scan “N dates”; code scans ±N days.
- README `--max-duration` default (24 vs 25), `--prune-cache` description (today’s cache is
  also truncated), and missing `--min-trip`/`--weight` flags.
- TASK.md has stale line references, an obsolete `HttpClient` return contract, a listed but
  unimplemented `--no-impersonate`, and issue #10 marked fixed before stale `_abck` handling.
- T01 has a contradiction: “skips reading” vs “load once at entry.”
- T04’s dependency line differs across documents.
- TASK.md’s related-documents line previously said `tasks/` was T01–T08; it must cover
  T09–T14 after this review pass.
- Historical test counts/line references are presented as current.
- Missing tests for some T03/T04/T05 edge cases.
- T10 adds `.data_state.lock` and `*.corrupt-*` files that must be documented/gitignored in
  the final file/state docs.

## Steps

1. **README**
   - Replace the “Copy as fetch” setup with the exact accepted format: raw request line +
     `Header: value` lines, or document the browser “Copy request headers” path. Include a
     short example and state that a fetch object/JSON is not parsed.
   - Correct `--ob`/`--rb` to “±N days around the fixed date,” not “N dates.”
   - Correct `--max-duration` default, `--prune-cache` behavior, and add missing flags
     (`--min-trip`, `--weight`, `--max-retries`, `--budget`, `--force`, `--clear-block`,
     `--all-fares`, `--nofilter`, local-only modes).
   - Document `--json` + `--urls` behavior from T12, FX cache/conversion behavior from T09,
     corrupt-state recovery from T10/T11, and local modes not requiring `config.json`.
   - Ensure examples do not imply live-calibration calls are safe by default.
2. **TASK.md**
   - Verify/revise the top status/revision history and the “post-T05 review remediation” table
     added in the review pass.
   - Replace stale line references with function names or current anchors.
   - Correct the `HttpClient` return contract, remove/implement `--no-impersonate`, and fix the
     “issue 9 matters immediately” historical wording.
   - Change issue #10 to “partially fixed (T02); hardened in T13” until T13 lands, then mark
     accurately.
   - Verify the related-documents line covers T01–T14.
3. **task-data.md**
   - Verify/complete the T09–T14 index rows and the T06 dependency row added in the review
     pass.
   - Verify `currency.json` is listed in §7 and note that it is a cache, not a feature switch.
   - Verify/update §8 with malformed-200 and local-state error behavior after T09–T13.
   - Note that exact test counts/line numbers in historical logs are historical, not current.
   - Add planned helpers to the code map only after they exist.
4. **Existing task docs**
   - Fix T01’s “skips reading”/“load once” contradiction.
   - Align T04’s “Depends on” line with task-data.
   - Verify/complete T06’s scope and dependency on the full prerequisite set; confirm the new
     sign-off checks (non-JSON 200, corrupt state, header pollution, JSON+URLs,
     all-transient exit).
   - Update T03/T05 wording where later tasks supersede or clarify it; add explicit
     “Superseded by T09/T11/T13” notes rather than silently rewriting history.
5. **Test-contract gaps**
   - Confirm each T09–T13 test list is present and runnable; add the remaining missing tests:
     - T05 non-raw abort result shape, end-to-end `update_price_map()` exactly once, scan
       partial rendering/zero-partial abort;
     - JSON/`--urls` partial-abort output; disk cooldown preservation; retry wait/record;
     - T04 `clear-block`/`--force` and corrupt-state recovery;
     - T03 cache-hit cookie/header behavior and polluted-config send-time filtering;
     - T02 non-`flight-offers` provenance and headers-only warning.
   - Keep all tests offline with temp paths and fake clocks/sessions.
   - Run `python -m unittest discover -s tests -v` and `python -m py_compile flights.py`.
6. **Final consistency pass**
   - Search for stale flag names, line refs, test counts, and contradictory statuses.
   - Ensure no doc claims a behavior that T09–T13 did not implement.
   - Verify T06’s sign-off checklist references the new tests.

## Acceptance criteria

- README setup instructions work with the actual parser.
- All flags/defaults/documented behaviors match the code.
- TASK.md/task-data.md/task docs have consistent dependencies, statuses, and current
  references.
- The full suite includes the listed T04/T05/T09–T13 edge-case tests and passes.
- T06 can run against accurate docs with no known documentation contradictions.

## Notes

- No code comments (repo rule).
- Do not delete historical execution logs; label them as historical where counts/line refs
  have drifted.
- Do not use git commands.
