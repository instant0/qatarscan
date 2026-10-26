# T04 — Persisted rate limiter & jittered pacing

- **Required**: Yes
- **Status**: **Done** (2026-10-26)
- **Depends on**: —
- **Blocks**: T05
- **Reference**: `../task-data.md` §6 (pacing), §7 (state files), §9 (definitions),
  §11 (test conventions)

## Objective

Add a cross-run, persisted throttle that spaces live calls at a 45s base with jitter, records
blocks, and refuses live calls during a cooldown. The limiter must be safe to import, easy to
test with a fake clock, and must not persist secrets.

## Scope

`flights.py`: new constants, exceptions, `RateLimiter`, singleton helpers, the pacing hook in
`fetch_flights()`, minimal user-facing error handling, and CLI flags. Also `.gitignore`,
`README.md` flag list, and tests.

**Out of scope (T05 owns these):** removing the legacy `time.sleep()` calls in
`_scan_dates()` / `drift_scan*()`, abort-on-403, `ScanAborted`, partial-result handling, and
rewriting scan-loop control flow. T04 must remain standalone-safe with the old scan sleeps
still present (the extra delay is harmless until T05 removes them).

## Design decisions

1. **Pacing lives in `fetch_flights()`.** It is the only function that knows both "cache miss"
   and "about to POST". Do not call `limiter.wait()` from `_fetch_and_parse()` or the scan
   loops. T05 removes the old scan `time.sleep()` calls and must **not** add a second
   `wait()`.
2. **Module singleton.** `get_rate_limiter()` returns a lazily created default limiter;
   `configure_rate_limiter(...)` replaces it (called once in `main()`); tests can patch
   `flights.get_rate_limiter`. This avoids threading a limiter through every call signature.
3. **Paths are module constants** next to the other file constants:
   `RATE_STATE_FILE` and `RATE_LOCK_FILE`, derived from the script directory. Tests patch
   these constants or construct `RateLimiter(state_file=..., lock_file=...)` with a temp dir.
4. **UTC everywhere.** Use timezone-aware `datetime.now(timezone.utc)`. Never use naive
   `datetime.now()` for rate state.
5. **Lock, not a second process guard.** `fcntl.flock` protects read/modify/write so state
   cannot be corrupted by concurrent writers. Do **not** hold the lock while sleeping.
   Sequential/back-to-back runs are guaranteed to respect spacing; two truly simultaneous
   runs are best-effort (same limitation documented in `TASK.md` §4.2).
6. **`--force` is run-scoped.** It bypasses an active cooldown for every live call in the
   current process but does **not** clear or rewrite `blocked_until`. A later run without
   `--force` is still blocked. `--clear-block` is the explicit persisted override.
7. **Budget counts POST attempts.** A failed/timed-out request still consumes budget, because
   `record_call()` runs in a `finally`. Cache hits never count.

## Steps

1. **State file `.rate_state.json`** (schema in task-data §7)
   - `live_calls`: list of UTC ISO-8601 strings (e.g. `2026-10-26T18:00:00+00:00`).
   - `blocked_until`: UTC ISO-8601 string or `null`.
   - Missing file, empty file, or corrupt JSON → treat as
     `{"live_calls": [], "blocked_until": None}`. Do not move/delete corrupt files; just
     overwrite them on the next write.
   - Ignore unknown keys when reading; write only the two known keys.
2. **Constants and imports**
   - Add `import fcntl`, `import random`, and `math` (or equivalent) at the top.
   - Import `timezone` from `datetime`.
   - Add `RATE_STATE_FILE` / `RATE_LOCK_FILE` beside `CONFIG_FILE` etc.
   - Change `SCAN["delay_seconds"]` from `20` to `45`.
3. **Locking and atomic writes**
   - Implement a private context manager that opens `RATE_LOCK_FILE`, takes an exclusive
     `fcntl.flock`, yields, and unlocks/closes in `finally`.
   - All reads/writes of `.rate_state.json` happen inside the lock.
   - Write to a temp file in the same directory, then `os.replace(temp, state_file)`.
   - `wait()` may read under the lock but must release it before sleeping.
4. **Exceptions**
   - `RateLimited(Exception)` with `remaining_seconds` and `blocked_until`; message includes
     the remaining minutes and the ISO timestamp.
   - `BudgetExceeded(Exception)` with `budget`; message states the per-run cap.
   - Keep messages one-line; no HTML, no traceback by default.
5. **`RateLimiter` API**
   - `__init__(state_file=None, lock_file=None, base_delay=None, budget=None, force=False,
     clock=None, sleep=None, rng=None)`.
     - Resolve `None` paths/values from the module constants and `SCAN["delay_seconds"]` at
       construction time (not as mutable default arguments).
     - `clock` defaults to `lambda: datetime.now(timezone.utc)`; `sleep` defaults to
       `time.sleep`; `rng` defaults to the `random` module. This makes fake-clock tests easy.
     - `self.calls_this_run = 0` (per-process budget counter).
   - `_required_interval()`: `base = max(1, int(base_delay))`;
     `low = max(1, base - 5)`; `high = max(low, base + 10)`;
     return `rng.randint(low, high)`. For 45 this is 40–55s.
   - `_read_state()`: resilient load, prune entries older than 24h, drop unparseable
     timestamps, return a normalized dict.
   - `record_call()`: under the lock, load, append the current UTC time, prune, write.
     Increment `calls_this_run`. **Must not raise on I/O failure**; print a yellow one-line
     warning via `log` and continue so it cannot mask an `EdgeBlock`.
   - `blocked_remaining() -> int`: seconds remaining, or 0 if no active block. Parse
     `blocked_until`; treat naive timestamps as UTC for forward compatibility.
   - `wait()`:
     1. If `blocked_remaining() > 0` and `not force`, raise `RateLimited(remaining,
        blocked_until)` immediately (no sleep).
     2. If `budget is not None` and `calls_this_run >= budget`, raise
        `BudgetExceeded(budget)`.
     3. Read the most recent live-call timestamp under the lock. Compute
        `target = last_call + seconds(_required_interval())`. If `now < target`, sleep
        `(target - now).total_seconds()`; otherwise return immediately.
     4. Do not append to `live_calls` in `wait()`; `record_call()` owns the write.
   - `record_block(minutes=20)`: under the lock set
     `blocked_until = now + timedelta(minutes=minutes)`, preserve `live_calls`, write.
     Keep the default in the 15–30 min band; callers (T05) may pass 15–30.
   - `clear_block()`: under the lock set `blocked_until = None` and write.
   - `is_blocked()`: convenience wrapper around `blocked_remaining() > 0`.
6. **Singleton helpers**
   - `_rate_limiter = None`, `get_rate_limiter()`, `configure_rate_limiter(**kwargs)`, and a
     test-friendly `reset_rate_limiter()` that clears the singleton.
   - Default construction must use the module path constants so tests can patch them.
7. **Wire `fetch_flights()`**
   - Keep the T01 cache logic and cache-hit return exactly as-is. Cache hits must not touch
     the limiter.
   - On a cache miss, after building the payload/client and immediately before
     `client.post_offers(...)`:
     ```python
     limiter = get_rate_limiter()
     limiter.wait()
     try:
         resp = client.post_offers(payload, referer)
     finally:
         limiter.record_call()
     _persist_cookies(cookies)
     ```
   - Do not move cookie persistence, response classification, cache writes, or the typed
     exceptions.
8. **Minimal error handling (T04 only)**
   - `_fetch_and_parse()`: catch `RateLimited` and `BudgetExceeded` alongside the existing
     typed errors, print one red line, and return `[]`. T05 will replace this with abort
     propagation.
   - The two single-request call sites in `main()`: add the two exceptions to their existing
     `except` tuples, print the one-line message, and `sys.exit(1)`.
   - Do not print raw HTML or tracebacks.
9. **CLI**
   - `--delay SEC`: keep the flag, default `None`; help text says base spacing (default 45,
     jittered −5/+10). Resolve to `SCAN["delay_seconds"]` when `None`.
   - `--budget N`: `type=int`, default `None`, metavar `N`, optional per-run cap on POST
     attempts (off by default).
   - `--force`: `store_true`; bypass an active cooldown for this run only.
   - `--clear-block`: `store_true`; clear persisted `blocked_until`, print a one-line
     confirmation, and return without scanning. Handle this before config validation so it
     works even with a broken/missing `config.json`.
   - In `main()`, configure the limiter once after `parse_args()`:
     `configure_rate_limiter(base_delay=args.delay if args.delay is not None else
     SCAN["delay_seconds"], budget=args.budget, force=args.force)`.
     If `--force` is set and a cooldown is active, print one yellow note that the cooldown is
     being ignored but remains on disk.
10. **Gitignore / docs**
    - Add `.rate_state.json` and `.rate_state.lock` to `.gitignore`.
    - Update the README flag list for `--delay`, `--budget`, `--force`, and `--clear-block`.
11. **Tests**
    - New `tests/test_rate_limiter.py` using `unittest`, `tempfile`, a mutable fake clock,
      and a fake `sleep` that advances the clock. Do not touch the repo's real
      `.rate_state.json`.
    - Required cases:
      - Jittered spacing: a second live call at `t0` sleeps the required interval; a call
        after the interval returns without sleeping.
      - Cross-run: limiter A records a call; a fresh limiter B on the same state file sleeps
        the remainder.
      - Pruning: entries older than 24h are dropped; recent entries stay.
      - `blocked_until` in the future raises `RateLimited` with the correct remaining time
        and does not sleep.
      - `--force`: bypasses the cooldown, leaves `blocked_until` unchanged on disk.
      - `record_block(minutes)` sets the correct UTC timestamp; `clear_block()` clears it.
      - Budget: after N `record_call()`s, the next `wait()` raises `BudgetExceeded`.
      - Corrupt/missing state is tolerated.
      - Lock serialization (best effort): concurrent `record_call()` writers produce a valid
        state file with no lost/corrupt entries.
    - Update the existing `FetchFlightsTests` in `tests/test_http_client.py` and
      `NoCacheTests` in `tests/test_nocache.py` to patch `flights.get_rate_limiter()` with a
      no-op fake in `setUp`/`tearDown`. They must never sleep or touch the real state file.
      Add assertions that a cache hit does not call `wait()` and a cache miss does.
    - Keep all existing tests green.
12. **Verification**
    - `python -m py_compile flights.py`
    - `python -m unittest discover -s tests -v`
    - `python flights.py --help` (flags present, exits 0)
    - `python -c "import flights"` (no state file created merely by importing)

## Acceptance criteria

- Consecutive live calls are separated by a jittered ~40–55s interval (base 45).
- Two separate process runs respect the stored spacing (state persists across runs).
- An active `blocked_until` refuses live calls, prints the remaining time, and does not sleep.
- Cache hits do not call `wait()`, do not update `live_calls`, and do not consume budget.
- A failed POST still records a live call (budget is consumed).
- `--budget` stops after N POST attempts in the current run.
- `--force` bypasses a cooldown for the run without clearing it on disk.
- `--clear-block` clears the persisted cooldown and exits.
- Legacy scan sleeps remain untouched for T05; no abort-on-403 logic is added here.
- No secrets, cookies, or raw HTML are written to `.rate_state.json`.

## Notes

- Use UTC everywhere. Do not store cookies or secrets in the rate state.
- No code comments (repo rule). Do not run git commands. Do not delete/rename files.
- T05 contract: `fetch_flights()` already paces; T05 must remove the old `time.sleep()`
  calls rather than adding another `wait()`.

## Execution log

- Added persisted `RateLimiter` with `.rate_state.json`/`.rate_state.lock`, `fcntl.flock`, UTC timestamps, atomic temp+`os.replace` writes, pruning, and injectable clock/sleep/rng.
- Added `RateLimited`/`BudgetExceeded`, singleton helpers (`get_rate_limiter`, `configure_rate_limiter`, `reset_rate_limiter`), constants `RATE_STATE_FILE`/`RATE_LOCK_FILE`, and changed `SCAN["delay_seconds"]` to 45 with jittered interval 40-55.
- Wired pacing only in `fetch_flights()` on cache miss before `client.post_offers(...)` with `wait()` and `record_call()` in `finally` (cache hits untouched). Updated `_fetch_and_parse()` and the two single-request sites in `main()` to catch `RateLimited`/`BudgetExceeded`.
- Added CLI `--budget`, `--force`, `--clear-block`; `--delay` help updated and limiter configured in `main()` after `parse_args()` (`--clear-block` exits before validation/config use where possible).
- Added `.rate_state.json` and `.rate_state.lock` to `.gitignore`, updated README flag list.
- Added `tests/test_rate_limiter.py` (jitter, cross-run, pruning, blocking/force, record/clear block, budget, corrupt/missing, concurrent writers). Patched `tests/test_http_client.py` and `tests/test_nocache.py` to use no-op `get_rate_limiter()` and added assertions for cache-hit/miss limiter behavior.
- Legacy scan-loop `time.sleep()` calls left in place (T05 owns removal). No abort-on-403 added.
- Verification: `python -m py_compile flights.py` OK; `python -m unittest discover -s tests -v` -> 35 tests OK; `python flights.py --help` shows new flags; `python -c "import flights"` created no `.rate_state.json`/`.rate_state.lock`.
