# T06 — Verification & rate calibration

- **Required**: Yes
- **Status**: **Pending**
- **Depends on**: T01–T05, T09–T15
- **Blocks**: release / sign-off
- **Reference**: `../task-data.md` §6 (pacing), §11 (tests); T14 inventory; T15 behavior fixes

> 2026-10-26 — post-T14/T15 review. T14 and T15 are Done; the current baseline is 115 tests
> after consolidating overlapping cases into parameterized tests. T06 consumes T14's final
> test inventory and verifies T15. Live calibration remains conditional, bounded, and opt-in.
> Do not run it against a stale or burned identity.

## Objective

Prove the required behavior works, calibrate the real request budget safely, and produce the
final sign-off record. T06 verifies the completed suite and docs; it does not re-implement
tasks or duplicate T14's test cleanup.

## Scope

Verification, a bounded manual calibration procedure (only with explicit consent and a fresh
warm paste), and final status/default documentation. No endpoint work and no new required
features. Optional `--dry-run`/`--doctor` diagnostics are out of T06 scope; if wanted later,
create a separate task with redaction, offline behavior, tests, and docs.

## Preconditions

1. T14 and T15 are Done; README/TASK/task-data statuses and dependencies are consistent.
2. The full suite passes and the current 115-test baseline is recorded. T06 verifies the
   completed suite; it should not need new tests.
3. The T14 inventory confirms the T09 non-JSON (T14), T09 malformed-JSON (T15), and T05
   retry-accounting (T14) tests. Do not duplicate them.
4. Live calibration has a fresh warm `--paste`, explicit user consent, a future date pair, a
   clean residential/non-datacenter egress, and a call/time bound. Do not use VPN/datacenter
   egress. Default to offline checks only.

## Steps

1. **Offline preflight.**
   - Run `python -m unittest discover -s tests -v`, `python -m py_compile flights.py`,
     `python -c "import flights"`, and `python flights.py --help`.
   - Record the exact suite count and confirm the T14 additions are present.
   - Confirm no docs still present contrary statuses/defaults/flags from the T14 sweep.

2. **Verification matrix.**
   - **CLI/exit codes**: invalid CLI = 2; `--json` + `--drift`/`--drift-ret` = 2; all-transient
     scan/drift = 1; mixed transient = 0; abort = 1; local-only success = 0.
   - **T09 parsing/FX**: non-JSON and malformed 200s are typed, no traceback, no cooldown;
     malformed payload cache behavior matches T15; invalid/stale FX cache is handled without
     a warning storm.
   - **T10 persistence**: atomic/locked writes, merge-on-save, quarantine, write-failure
     behavior, archive ordering, and corrupt read-only views.
   - **T11 limiter**: corrupt/unreadable state fails closed even with `--force`;
     `--clear-block` recovers; a new cooldown does not shorten an existing one; abort-path
     recording failures do not mask the abort.
   - **T12 output**: Rich literal rendering, JSON-on-stdout with links on stderr, scan/drift
     `--urls`, return-failure price-map updates, and typed CLI validation.
   - **T13 config/headers**: corrupt config quarantine and `--paste` replacement, skipped
     automatic cookie persistence, cookie allowlist, stale/invalid `_abck` warnings, bounded
     error bodies/references, and send-time forbidden-header filtering.
   - **T15 residuals**: FX failure memoization, nested-schema quarantine/fail-closed behavior,
     malformed-payload caching, direction-correct booking URLs, and numeric/default-date
     validation.

3. **Deterministic cache/exact-pair check.**
   - Choose a future date pair and set it in shell variables:
     ```sh
     DEPART=YYYY-MM-DD   # future date
     RET=YYYY-MM-DD      # future date, later than DEPART
     ```
   - Run with `--nocache` first so it makes exactly two live calls: outbound + return.
   - Snapshot `.rate_state.json` `live_calls` before the cached rerun; rerun the same pair
     without `--nocache` and require no added live-call timestamps, no rate-limit pause, and
     identical cached output. Do not rely on pre-existing cache files or claim cache-only
     behavior from stale state.
   - Confirm exit codes and partial-result behavior for failures.
   - Example:
     ```sh
     python flights.py --depart "$DEPART" --ret "$RET" --nocache --budget 2
     python flights.py --depart "$DEPART" --ret "$RET"
     ```

4. **Bounded calibration (opt-in).**
   - Start with a 45s base only. Test 60/90/120 only if 45s blocks, matching TASK's conditional
     procedure; do not deliberately burn four identities.
   - Use a fresh warm paste and a fresh identity per data point. Do not reuse a burned session.
     Stop at the first 403 and do not continue on the same identity.
   - Use clean residential/non-datacenter egress; do not use a VPN/datacenter address.
   - Force one live call with a future pair and `--min-trip 0` so the date is not skipped:
     ```sh
     python flights.py --scan --ob 0 --min-trip 0 \
       --depart "$DEPART" --ret "$RET" \
       --nocache --delay 45 --budget 1
     ```
   - Bound every experiment with `--budget` and/or a wall-clock limit. Stop at the first 403
     or the success cap, whichever comes first.
   - Record actual pause intervals, not just the base, because jitter is base−5..base+10.
   - For the scope test after a block, run `--clear-block` or use `--force`; otherwise the
     persisted cooldown prevents the network call. State that success after re-paste proves
     “session-resettable,” not exclusive session scope.
   - Either calibrate `block_minutes` or explicitly retain 20 minutes with rationale; do not
     claim cooldown calibration without data.
   - Append a T06 calibration table under `TASK.md` §17 (currently only historical phase-0.5
     rows). Columns: date/time, base T, observed interval min/mean/max, identity/egress, calls
     attempted/succeeded, first-403 call number, elapsed, `_abck` before/after, scope result,
     cooldown result, and notes.

5. **End-to-end checks.**
   - Exact pair: 2 live calls with `--nocache`; then immediate cache rerun = 0 added live
     calls (check `.rate_state.json`).
   - Scan validation: `--scan --ob 2 --rb 2 --nocache --budget 10` with the future pair; expect
     10 live calls (or document a skipped date).
   - Drift validation: `--drift 1 --nocache --budget 6`; expect 6 calls and check partial-result
     and abort behavior if a block occurs.
   - Do not run large scans; stop at the first 403.

6. **Post-calibration docs/defaults.**
   - If calibration changes spacing or cooldown, update README, TASK, and task-data after T14.
     T06 owns this final default-doc update.
   - Do not leave stale line references, historical counts presented as current, or old flag
     defaults.
   - Keep T07/T08 optional and Pending; they are not sign-off blockers.

7. **Sign-off/status.**
   - Mark T06 Done in this file, `task-data.md`, and `TASK.md` only after the calibration table
     or a documented skip rationale is in TASK §17; update issue #13 and revision history with
     the final test count and calibration result.
   - Record any residual optional work (T07/T08) separately; do not silently close them.

## Acceptance criteria

- Full suite green, exact count recorded (115-test baseline), `py_compile`/import/help checks
  clean.
- T14's final docs are accurate and T15 behavior is covered by tests.
- Verification matrix and T06 checklist are complete; no duplicated T14 work.
- Calibration is bounded, conditional, fresh-identity, and documented with observed intervals;
  if live calibration is intentionally skipped, record the rationale in TASK §17.
- Cache-only and exact-pair checks are deterministic and do not rely on stale files.
- Any default changes update all docs; T06 is marked Done and T07/T08 remain optional.

## Notes

- No code comments (repo rule).
- Do not run git commands.
- Do not run live calibration without explicit consent and a fresh warm paste.
- Never print full auth headers or cookie values; redact diagnostics.
- Do not treat current cache contents as proof of offline behavior; they may be stale.
