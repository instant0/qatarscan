# T11 — Rate limiter and cooldown hardening

- **Required**: Yes
- **Status**: **Pending**
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

## Steps

1. **State read result**
   - Change `_read_state()` to distinguish:
     - missing/empty file -> normal empty state;
     - corrupt JSON or unreadable file -> `RateStateError`.
   - Do not silently return an empty state for corrupt/unreadable data.
   - `wait()` catches `RateStateError`, logs a clear warning, and raises `RateLimited` with a
     conservative `SCAN["block_minutes"]` fallback (in-memory only) so no live call is
     attempted. The message must tell the user to run `--clear-block` or `--force`
     deliberately.
   - `--clear-block` may recover a corrupt state by atomically writing a fresh empty state.
2. **No cooldown race**
   - `wait()` must not hold the lock while sleeping.
   - Loop: read state under the lock -> check cooldown/budget -> compute the required sleep
     from the latest recorded call -> release -> sleep -> re-read and recheck.
   - If a cooldown appears during the sleep, raise `RateLimited` immediately after waking.
   - If another process records a call during the sleep, recompute the target instead of
     sleeping a stale interval.
   - Keep cache hits free and keep budget checks before any sleep.
3. **Block recording**
   - `record_block(minutes=...)`: under the lock, load state; compute
     `candidate = now + minutes`; write `max(existing_blocked_until, candidate)`.
   - Never shorten a longer cooldown, including under `--force`.
   - Preserve `live_calls`.
   - On `RateStateError` or I/O failure, log a yellow one-line warning and **do not raise**;
     the abort path must still report the original edge block.
4. **Clear and record-call failures**
   - `clear_block()`: under the lock, write `blocked_until = None`; on corrupt state, write a
     clean empty state; on I/O failure, surface a clear `LocalStateError`/`RateStateError` so
     the CLI does not claim success.
   - `record_call()`: keep the existing no-raise behavior; on corrupt/unreadable state, warn
     and skip the write rather than masking the network result.
   - `_record_block_for_abort()`: keep edge-only behavior; warn on recording failure and
     continue rendering the abort/partials.
5. **Clock/force semantics**
   - Keep UTC-aware timestamps and the injected clock/sleep/rng seams.
   - `--force` remains run-scoped and must not clear or rewrite `blocked_until` by itself.
   - Budget behavior is unchanged: failed/timed-out POST attempts still count; cache hits do
     not.
6. **Tests**
   - Extend `tests/test_rate_limiter.py`:
     - a block written by another actor during `wait()`'s sleep is honored;
     - a concurrent call recorded during sleep causes recomputation, not an early POST;
     - corrupt/unreadable state fails closed (no live call) and suggests recovery;
     - `--clear-block` recovers a corrupt state;
     - `record_block()` preserves a longer existing cooldown;
     - `record_block()` write failure does not raise/mask an abort;
     - `clear_block()` write failure is reported, not silently swallowed;
     - missing/empty state still behaves as a first run;
     - cache hits still do not touch limiter state.
   - Use fake clocks/sleeps and temporary paths; no live network.

## Acceptance criteria

- No live call can start while an active cooldown is present, including one recorded during a
  sleep.
- Corrupt/unreadable limiter state fails closed with an actionable message.
- An abort-path block-recording failure never hides the original edge/auth/rate/budget cause.
- A longer existing cooldown is never shortened.
- Existing limiter and scan tests remain green.

## Notes

- No code comments (repo rule).
- Do not hold the lock while sleeping or scanning.
- Do not change the 45s/jitter default here; T06 owns calibration.
- Keep the four T05 abort reasons; limiter-state failure uses the existing `rate` abort path
  with an explicit recovery message, not a fifth reason.
