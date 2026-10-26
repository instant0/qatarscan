# T06 — Verification & rate calibration

- **Required**: Yes
- **Depends on**: T01–T05, T09–T14
- **Blocks**: release / sign-off
- **Reference**: `../task-data.md` §6 (pacing), §11 (tests)

> 2026-10-26 — post-T05 review found parsing, persistence, limiter, output, config, and
> doc/test gaps. T09–T14 must land before T06 sign-off. T06 now verifies the full
> prerequisite set, not just T01–T05.

## Objective

Prove the fix works and calibrate the real request budget. Produce the test suite and the
calibration data that sets the default spacing/cooldown.

## Scope

New `tests/` suite, optional diagnostics (`--dry-run`, `--doctor`), and a manual calibration
procedure. No new endpoint work.

## Steps

1. **Test suite** (`tests/`, stdlib `unittest`)
   - Cover each task's listed tests (T01–T05 and T09–T13); T14 closes the remaining gaps.
   - Run: `python -m unittest discover -s tests -v`
   - Compile check: `python -m py_compile flights.py`
2. **Diagnostics (recommended)**
   - `--dry-run`: print the computed payload, headers, trace pair, Referer, and dates;
     send nothing.
   - `--doctor`: validate `config.json`, required headers, Akamai cookie presence, `_abck`
     state, and the rate-limiter state; print actionable fixes.
3. **Calibration (manual, opt-in)**
   - For each spacing base T ∈ {45, 60, 90, 120}:
     - Start from a warm `--paste` (or a 30+ min cooldown).
     - Run single-date live calls at T until the first 403.
     - Record: calls before block, time to block, `_abck` state.
   - Test scope: after a block, re-paste and try one call. If it still 403s, the block is
     IP/time-scoped; if it succeeds, it is session-scoped.
   - Write results into `TASK.md` §17 (Results log) and update the defaults if needed.
4. **End-to-end**
   - Exact date pair: expect 2 live calls, full detail, no 403.
   - `--ob 2 --rb 2`: 10 live calls max, spaced ~45s, aborts cleanly if blocked.
   - Cache-only run: fast, no live calls.
5. **Sign-off checklist**
   - Unit suite green.
   - `--nocache` works.
   - Trace pair rotates per call.
   - Cookies persist across calls.
   - Abort-on-403 works and preserves partial results.
   - Non-JSON/malformed 200s are typed and do not traceback.
   - Corrupt cache/price-map/limiter state is handled without silent data loss.
   - Polluted config headers are not sent.
   - JSON output stays valid with `--urls`; local-only modes run without `config.json`.
   - Calibration recorded; defaults justified.

## Acceptance criteria

- All unit tests pass and `py_compile` is clean.
- Calibration table is filled in `TASK.md`.
- The recommended base (start 45s) is either confirmed or adjusted with evidence.

## Notes

Do not run large scans during calibration. Use one date per call and stop at the first 403.
