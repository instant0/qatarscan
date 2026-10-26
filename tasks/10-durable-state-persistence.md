# T10 — Durable JSON state persistence

- **Required**: Yes
- **Status**: **Done** (2026-10-26)
- **Depends on**: T01, T05
- **Blocks**: T09, T13, T06, T14
- **Reference**: `../task-data.md` §7 (persisted files), §8 (errors), §11 (tests);
  post-T05 review H2

## Objective

Make local JSON caches durable and concurrency-safe: atomic writes, read-modify-write
locking, corruption quarantine, and merge-on-save. A crash or concurrent run must not silently
truncate `offers_cache.json`, `cache_archive.json`, `price_map.json`, or `currency.json`.

## Scope

`flights.py`: `load_offers_cache()`, `save_offers_cache()`, `archive_stale_cache()`,
`update_price_map()`, `load_currency_rates()`, `save_currency_rates()`, the read-only
`price_map.json`/`cache_archive.json` consumers (`print_price_map()`,
`print_price_trends()`, `print_price_calendar()`, `_load_archive_prices()`), the `main()`
handling for `LocalStateError`, and new private JSON persistence helpers. Also `.gitignore`
for any new lock/quarantine files and tests. T13 owns `config.json`; T09 owns FX
fetch/validation semantics.

## Review findings addressed

- Cache/archive/price-map/currency writes use direct whole-file `write_text()`.
- A corrupt cache raises during load; a corrupt price map is replaced with `{}`.
- Corrupt price-map/archive files also crash local calendar/trend/price-map views.
- Two runs can load the same file and overwrite each other's entries.
- A failed write can leave a truncated/empty JSON file.

## Design decisions

1. **One reusable atomic writer.** Add `_atomic_write_json(path, data)`:
   - write to a temp file in the same directory;
   - `flush()` and `os.fsync()`;
   - `os.replace(temp, path)`;
   - remove the temp file on failure;
   - never leave a partially written target.
2. **One data lock.** Add a private context manager backed by `fcntl.flock` and a single
   `DATA_LOCK_FILE` (for example `.data_state.lock`) for all read-modify-write operations in
   this task. The rate limiter keeps its own lock. Do not hold the data lock during network
   calls, sleeps, or rendering.
3. **Quarantine, do not silently discard.** On corrupt/unreadable JSON:
   - log a clear one-line warning;
   - copy the original to `<file>.corrupt-<UTC timestamp>` before replacing it;
   - continue with the documented empty/default value only if the caller can tolerate it.
   - If the copy fails, do not overwrite the original; raise a typed local-state error.
4. **Merge on save.** `save_offers_cache(cache)` and `update_price_map(results)` must reload
   the current file under the lock, merge the caller's entries over it, and write once.
   Existing unrelated keys survive.
5. **Archive ordering.** `archive_stale_cache()` writes the merged archive before rewriting
   the working cache, so a failure cannot lose the only copy of an evicted entry. Duplicate
   keys are overwritten deterministically.
6. **No silent corruption reset for price-map history.** Quarantine and warn; do not claim
   history was preserved. The new file starts from the current run only.
7. **Read vs. write failures.** A read failure that cannot quarantine is a hard
   `LocalStateError` and `main()` exits 1 before a live call. A write failure after a
   successful fetch/scan logs a warning, leaves the old file intact, and lets the current
   results continue; caching must not turn a successful API call into a failed run.

## Steps

1. **Helpers and constants**
   - Add `DATA_LOCK_FILE` beside the other path constants.
   - Add `_atomic_write_json()`, `_locked_data()`, `_read_json_file(path, default)`,
     `_quarantine_corrupt(path)`, and a typed `LocalStateError` if needed.
   - Use timezone-aware UTC timestamps in quarantine names.
   - Ensure temp files are created in the target directory so `os.replace()` is atomic.
2. **Offers cache**
   - `load_offers_cache()`: missing/empty -> `{}`; corrupt/unreadable -> quarantine, warn,
     return `{}`. If the quarantine copy itself fails, raise `LocalStateError` and let
     `main` print a clear error; do not overwrite the original.
   - `save_offers_cache(cache)`: under the data lock, reload the current cache, merge the
     caller's entries over it, and atomically write.
   - Preserve T01 semantics: `--nocache` skips the hit check but still merges/writes on a
     successful fetch; existing entries are not erased.
3. **Archive**
   - `archive_stale_cache()`: under the lock, load both files; move previous-day entries to
     the archive; prune archive entries older than 365 days; write archive then cache.
   - If either load is corrupt, quarantine and continue with the valid side; never delete a
     corrupt original before the quarantine copy exists.
   - A quarantine-copy failure is the only reason to abort the archive step; otherwise a
     corrupt file starts fresh and the copy preserves the old bytes.
   - Return the same count/behavior expected by `main()`.
4. **Price map**
   - `update_price_map(results)`: under the lock, load the current map with quarantine-aware
     handling, merge the new aggregate/ledger entries, and atomically write.
   - Do not call `save` when there is nothing to merge.
   - A corrupt map is quarantined and logged, then starts fresh; if quarantine fails, raise
     `LocalStateError` rather than silently replacing it.
5. **Currency cache**
   - `load_currency_rates()`: missing/empty -> `{}`; corrupt -> quarantine, warn, return `{}`;
     a failed quarantine raises `LocalStateError`.
   - `save_currency_rates(rates)`: under the lock, merge the supplied keys over the current
     file (do not drop unrelated keys) and atomically write.
   - Preserve `USD_NOK` and any future currency key; never delete rows during a write.
6. **Read-only consumers**
   - Route `print_price_map()`, `print_price_trends()`, `print_price_calendar()`, and
     `_load_archive_prices()` through `_read_json_file()` so a corrupt local file produces a
     warning/empty view instead of a traceback.
   - Do not quarantine on every read-only view; only the shared loader quarantines when it
     actually replaces the file. If read-only quarantine is needed, keep it one-time and
     logged.
   - Preserve the existing empty-data messages.
7. **Tests**
   - Add `tests/test_persistence.py`:
     - atomic write replaces the target and leaves no temp file;
     - a simulated write failure leaves the original intact and keeps scan results usable;
     - corrupt offers cache/price map/currency cache is quarantined and warned;
     - `save_offers_cache()` merges concurrent additions;
     - `update_price_map()` preserves unrelated dates/fares;
     - `save_currency_rates()` preserves `THB_NOK`, `VND_NOK`, and `USD_NOK`;
     - archive pruning keeps entries within 365 days and drops older ones;
     - archive write failure does not lose evicted entries;
     - corrupt price-map/archive local views do not raise.
   - Use temporary paths/constants; no live network.

## Acceptance criteria

- All cache/archive/price-map/currency writes are atomic and locked.
- Concurrent read-modify-write updates do not lose unrelated entries.
- Corrupt files are quarantined and visible in logs; no silent truncation.
- A failed write leaves the previous file usable and does not abort successful scan results.
- An unquarantinable read failure exits before any live call with a clear error.
- Existing T01/T05 behavior and all current tests remain green.

## Execution log

- Added `LocalStateError`, `_atomic_write_json()`, `_locked_data()`, `_quarantine_corrupt()`,
  `_read_json_file()`, `DATA_LOCK_FILE`, and UTC quarantine names.
- Made offers cache/archive/price-map/currency writes atomic and locked; saves reload and
  merge so concurrent/unrelated keys survive.
- Archive writes the merged archive before the cache, deduplicates by key, and prunes entries
  older than `ARCHIVE_MAX_DAYS`.
- Corrupt JSON is copied to `<file>.corrupt-<UTC timestamp>` before the caller resets to an
  empty/default value; quarantine failure raises `LocalStateError` and `main()` exits 1.
- Write failures warn and leave the old file intact; successful scan results continue.
- Routed read-only price-map/archive views through the shared loader with no repeated
  quarantine; they warn and render empty instead of tracing back.
- Added `tests/test_persistence.py` covering atomic/failed writes, quarantine, merge-on-save,
  price-map preservation, currency preservation, archive dedupe/prune/write failure,
  read-only corrupt views, and `main()` local-state exit.
- Verification: `python -m py_compile flights.py` OK; `python -m unittest discover -s tests`
  -> 78 tests OK. No live scans or network requests.

## Notes

- No code comments (repo rule).
- Do not delete/rename user files as part of the task; quarantine copies are created by code.
- Do not hold `DATA_LOCK_FILE` while scanning or calling the network.
- T13 may reuse `_atomic_write_json()` for `config.json`; do not duplicate the helper.
