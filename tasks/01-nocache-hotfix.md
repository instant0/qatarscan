# T01 — Hotfix: `--nocache` `UnboundLocalError`

- **Required**: Yes
- **Status**: **Done** (2026-10-25)
- **Depends on**: —
- **Blocks**: T06 (diagnostics use `--nocache`)
- **Reference**: `../task-data.md` §10 (code map)

## Objective

Fix the crash where a successful fetch with `--nocache` raises `UnboundLocalError` because
`cache` is only assigned inside the `if not _nocache:` branch.

## Scope

Only `fetch_flights()` in `flights.py`. No behavior change for the cached path.

## Steps

1. Load the cache once at function entry for merge-on-write:
   - `cache = load_offers_cache()` before the `if not _nocache:` block.
   - Keep the cache-hit check inside `if not _nocache:`.
   - Do **not** initialize `cache = {}`: `save_offers_cache()` writes the whole dict, so an
     empty dict would erase previously cached dates.
2. Preserve intended semantics:
   - The cache is **always loaded at function entry** so a successful `_nocache` fetch can
     merge into existing entries. `_nocache` skips only the **cache-hit check**, not the load.
   - A successful response **still writes** to the cache (the `cache[key] = ...` /
     `save_offers_cache(cache)` path in `fetch_flights()`).
3. Keep the return contract `(data, status, error)` and the cache key format
   `{origin}_{destination}_{date}`.

## Acceptance criteria

- `--nocache` + HTTP 200 does not raise and updates `offers_cache.json`.
- Pre-existing cache entries survive a `--nocache` write.
- Normal cached path is unchanged.
- No changes to filtering, parsing, or output.

## Tests

- Unit: monkeypatch `requests.post` to return a 200 JSON payload; call `fetch_flights`
  with `_nocache=True`; assert no exception and the cache file contains the key.
- Unit: seed an unrelated cache entry; after a `--nocache` success, assert both entries remain.
- Unit: `_nocache=False` with a pre-seeded same-day entry; assert the network is not called.

## Notes

This is a prerequisite for the single-request diagnostic in T06 and for trustworthy cache
behavior while debugging.

## Execution log

- 2026-10-25 — moved `cache = load_offers_cache()` to function entry; the `_nocache` branch
  now only skips the hit check. Existing entries are preserved on write.
- Tests: `tests/test_nocache.py` (3 tests) pass via
  `python -m unittest discover -s tests -v`; `python -m py_compile flights.py` clean;
  `python flights.py --help` OK.
