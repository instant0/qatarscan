# T11 — Rate limiter and cooldown hardening

- **Required**: Yes
- **Status**: **Done**
- **Depends on**: T04, T05
- **Blocks**: T06, T14
- **Reference**: `../task-data.md` §6 (pacing), §7 (state), §8 (errors), §11 (tests);
  post-T05 review M4/M10

## Objective

Close the cooldown race and failure modes in the persisted limiter so no live call can slip
past an active block, a corrupt state file cannot silently disable pacing, and an abort-path
disk error cannot mask the original block.

## Scope

`flights.py`: `RateLimiter._read_state()`, `wait()`, `record_call()`, `record_block()`,
`clear_block()`, `_record_block_for_abort()`, and the related `main()` error handling. Tests
and the rate-limiter section of README. T04's core 45s/jitter, UTC, lock, and atomic-write
decisions remain in force unless explicitly superseded here.

## Review findings addressed

- `wait()` releases the lock before sleeping and never rechecks `blocked_until`; a concurrent
  block can be ignored.
- `_read_state()` treats corrupt/unreadable state as empty, which fails open.
- `record_block()`/`clear_block()` I/O failures can raise and mask an `EdgeBlock`.
- A new edge block can shorten an existing longer cooldown.
- Some limiter paths lack regression tests for disk failure and concurrent state changes.

## Supersedes

- T04 step 1 says corrupt JSON becomes an empty state. T11 distinguishes **missing/empty**
  (normal first run) from **corrupt/unreadable** (fail closed with a clear recovery path).
- T04 says `record_block()` sets a new `blocked_until`. T11 requires never shortening an
  existing later `blocked_until`.

## Review revisions (2026-10-26)

- Treat schema-invalid state as unusable, not just invalid JSON: a non-object top-level
  payload or a present non-null `blocked_until` that cannot be parsed raises
  `RateStateError`.
- Corrupt/unreadable state fails closed **even with `--force`**. `--force` only bypasses a
  valid persisted cooldown; only `--clear-block` recovers an unusable state. This supersedes
  the ambiguous "`--clear-block` or `--force`" wording below.
- `RateLimited` gains optional `state_error=False`; `_abort_message()` uses it for an explicit
  `--clear-block` recovery message instead of suggesting `--force`.
- `is_blocked()` / `blocked_remaining()` must not raise from the `--force` pre-check. They
  return a fail-closed fallback; `wait()` owns the warning and the abort.
- The no-race guarantee is scoped to `wait()`: a block written during sleep is observed on
  wake. A block written after `wait()` returns but before the POST is outside the limiter's
  control because the lock is not held across calls; T04's cross-process best-effort note
  still applies.

## Steps

1. **State read result**
   - Add `RateStateError(LocalStateError)` so an uncaught read failure still reaches `main()`.
   - Change `_read_state()` to distinguish:
     - missing/empty file -> normal empty state;
     - corrupt JSON, non-object JSON, or unreadable file -> `RateStateError`;
     - present, non-null `blocked_until` that cannot be parsed -> `RateStateError`.
   - Invalid `live_calls` entries are dropped as before; an invalid `live_calls` container is
     treated as empty. A missing/invalid call history must not hide a valid cooldown.
   - Do not silently return an empty state for corrupt/unreadable data.
   - `wait()` catches `RateStateError`, logs a clear warning, and immediately raises
     `RateLimited(..., state_error=True)` with a conservative `SCAN["block_minutes"]`
     fallback (in-memory only) so no live call is attempted and no sleep occurs. The message
     must tell the user to run `--clear-block`; `--force` does not recover unusable state.
   - `_abort_message()` for a `state_error` rate abort must say the state is corrupt/unreadable
     and that `--clear-block` is the recovery, not claim a cooldown is merely active.
   - `--clear-block` recovers a corrupt state by atomically writing a fresh empty state.
2. **No cooldown race**
   - `wait()` must not hold the lock while sleeping.
   - Loop each iteration: check the per-run budget -> read state under the lock -> check the
     cooldown (unless `force`) -> compute the required sleep from the latest recorded call ->
     release -> sleep -> repeat.
   - If a cooldown appears during the sleep, raise `RateLimited` immediately after waking
     unless `force` is set. With `force`, continue to honor spacing but do not raise.
   - If another process records a call during the sleep, recompute the target instead of
     sleeping a stale interval. Recheck after every sleep, including short/early wakes.
   - Keep cache hits free and keep budget checks before any sleep.
3. **Block recording**
   - `record_block(minutes=...)`: under the lock, load state; compute
     `candidate = now + minutes`; write `max(existing_blocked_until, candidate)`.
   - Never shorten a longer cooldown, including under `--force`.
   - Preserve `live_calls`.
   - On `RateStateError`, I/O, or serialization failure, log a yellow one-line warning and
     **do not raise**; the abort path must still report the original edge block.
4. **Clear and record-call failures**
   - `clear_block()`: under the lock, load valid state and set `blocked_until = None`
     (preserving `live_calls`); on corrupt/unreadable state, write a clean empty state. On
     write failure, raise a clear `LocalStateError` with the cause so the CLI exits non-zero
     and never claims success.
   - `record_call()`: keep the existing no-raise behavior; on corrupt/unreadable state, warn
     (escaped) and skip the write rather than masking the network result.
   - `_record_block_for_abort()`: keep edge-only behavior; continue rendering the abort/partials
     because `record_block()` no longer raises on recording failure.
5. **Clock/force semantics**
   - Keep UTC-aware timestamps and the injected clock/sleep/rng seams.
   - `--force` remains run-scoped, bypasses only a valid active cooldown, and must not clear or
     rewrite `blocked_until` by itself.
   - Budget behavior is unchanged: failed/timed-out POST attempts still count; cache hits do
     not.
6. **Tests**
   - Extend `tests/test_rate_limiter.py`:
     - a block written by another actor during `wait()`'s sleep is honored;
     - a concurrent call recorded during sleep causes recomputation, not an early POST;
     - corrupt JSON, non-object JSON, unreadable state, and malformed `blocked_until` fail
       closed with no sleep and suggest `--clear-block`;
     - `--force` does not bypass corrupt/unreadable state;
     - `is_blocked()` returns the fail-closed fallback instead of raising;
     - `record_call()` on corrupt state warns and does not raise;
     - `--clear-block` recovers a corrupt state and preserves `live_calls` when valid;
     - `clear_block()` write failure raises `LocalStateError`;
     - `record_block()` preserves a longer existing cooldown and extends a shorter one;
     - `record_block()` read/write failure does not raise/mask an abort;
     - missing/empty state still behaves as a first run;
     - cache hits still do not touch limiter state.
   - Add an `_abort_message()` test for the limiter-state recovery path; keep the existing
     generic rate-message test.
   - Use fake clocks/sleeps and temporary paths; no live network.
7. **Docs and status**
   - Add a short README note: corrupt rate state fails closed, `--force` does not bypass it,
     and `--clear-block` resets it.
   - Mark T11 Done in this file and `task-data.md`; update `TASK.md` status, revision history,
     and the remediation table. Leave T12–T14 and T06 pending.

## Acceptance criteria

- No live call starts after `wait()` observes an active cooldown, including a cooldown
  recorded during a sleep. A block written after `wait()` returns is outside the limiter's
  lock boundary (T04 best-effort caveat).
- Corrupt/unreadable limiter state fails closed with an actionable `--clear-block` message,
  including under `--force`.
- An abort-path block-recording failure never hides the original edge/auth/rate/budget cause.
- A longer existing cooldown is never shortened.
- `--clear-block` recovers corrupt state; a write failure exits non-zero without a success
  message.
- Existing limiter and scan tests remain green.

## Verification

- `python -m py_compile flights.py`
- `python -m unittest discover -s tests -v`
- `python flights.py --help`
- `python -c "import flights"`
- Do not run live scans during this task.

## Notes

- No code comments (repo rule).
- Do not hold the lock while sleeping or scanning.
- Do not change the 45s/jitter default here; T06 owns calibration.
- Keep the four T05 abort reasons; limiter-state failure uses the existing `rate` abort path
  with an explicit recovery message, not a fifth reason.
- Do not delete/move/rename files; do not run git commands.

## Execution log

- Implemented in flights.py (RateLimiter hardening), added/updated tests, updated README, marked complete 2026-10-26.
