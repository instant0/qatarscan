# Qatar Airways Flight Scanner — Feature Plan

## Implemented

- [x] Single-request mode: fetch outbound + return, display as rich tables
- [x] Scan outbound mode (`--scan`, `--ob N`)
- [x] Scan return mode (`--rb N`)
- [x] Drift mode (`--drift N`): shift both dates together, preserve trip length
- [x] Drift-return mode (`--drift-ret N`): shift only return date, vary trip length
- [x] Duration filter (`--max-duration`), applied at parse time
- [x] Max price filter (`--max-price CABIN=VAL`, `--nofilter`)
- [x] Currency conversion (THB→NOK via CDN, daily cache)
- [x] Layout classification (QSuite > Door > Pod > Old)
- [x] Layout ranking in drift summary (best layout, best price, best duration)
- [x] Offers cache (`offers_cache.json`, `--nocache`)
- [x] Booking URL generation + drift summary URL
- [x] Fare filter (`--all-fares` for LITE/ELITE)
- [x] Avoid airports (LHR, LGW, HEL, KUL)
- [x] Date validation
- [x] `--paste` for browser header setup
- [x] Price map (`price_map.json`): day-of-week × flight pattern, tracks min/max/avg/samples per fare
- [x] `--price-map` flag: print analytical price trend table
- [x] Cache-aware delays: skip sleep when next API call is cached
- [x] Max stops filter (default 1, DOH-only)
- [x] Dirt-cheap business filter: BUSINESS_LITE/ELITE hidden unless priced near economy
- [x] Mixed-cabin matcher (`find_best_mixed` + `print_mixed_table`): groups drift results by date pair, finds cheapest per (direction, cabin), generates 4 combos, sorts by total
- [x] Bad-day pattern filter (`bad_patterns.json`, `is_bad_day()`, `--filter-bad`): pre-filters dates before API calls
- [x] `--recommend N`: wires mixed-cabin table into drift output
- [x] `--flag-bad DIR DAY REASON`: manual curation command
- [x] `--filter-bad`: activates bad-day pre-filter to skip known-bad day-of-week+direction combos

## Proposed — Mixed-Cabin Matcher

### Goal
Automatically compute what I did manually: for each date pair, find the cheapest mixed-cabin combo (one direction economy, other business) and rank by total price.

### Design

**When**: Automatically runs after any drift/scan that has both directions.

**Algorithm** (`find_best_mixed(results)`):
1. Group results by date pair `(_dep_date, _ret_date)`
2. For each pair, pick cheapest per (direction, cabin):
   - OB Economy (excl LITE)
   - OB Business (excl LITE/ELITE, prefer higher layout rank)
   - RB Economy
   - RB Business
3. Generate 4 combos per pair:
   - Econ OB + Biz RB (mixed A)
   - Biz OB + Econ RB (mixed B)
   - Econ OB + Econ RB (pure economy)
   - Biz OB + Biz RB (pure business)
4. Score: primary = total price, secondary = business layout rank (higher is better for ties)
5. Filter out combos where a required leg has no offer
6. Sort ascending by total price, show top N

**Output** — new table in drift mode after the pair overview:

```
Best Mixed-Cabin Combos (sorted by total price)
─────────────────────────────────────────────────────────────
Combo           Pair         OB               RB               Total
OB Econ+RB Biz  Aug 18→27    M 5,490  15h50m  R 16,411* Pod  21,901
OB Econ+RB Biz  Aug 22→Sep1  M 5,585  15h50m  R 16,751* Pod  22,336
OB Biz+RB Econ  Aug 18→Sep1  D 23,473 Pod     M 5,013*       28,486
```

**Booking URL**: show for top result per unique date pair.

### CLI
- `--recommend N` — override top N (default 5)
- `--mixed` — alias for `--recommend`

## Proposed — Bad-Day Filter (`--filter-bad`)

### Goal
Skip known-bad days/routes before making API calls, using accumulated price_map knowledge.

### Design

**Bad patterns** (stored in `bad_patterns.json`, curated manually or auto-detected):
- Day-of-week + direction = no direct (DOH-only) routing exists
  - e.g., `Tue BKK→OSL` → all routes have extra stop
- Day-of-week + direction = consistently expensive (avg > baseline + threshold)
- Specific flight numbers with undesirable layouts

**Filter logic** (`--filter-bad`):
1. Before each API call, check `bad_patterns.json`
2. If the date falls on a blocked day-of-week for that direction, skip it
3. Log: `[dim]Skipping Tue BKK→OSL: flagged as bad (no direct routing)[/dim]`

**Recording bad days**:
- `--flag-bad DATE DIRECTION REASON` — manual curation
- Auto-detect: after a drift, scan results for days where no offers passed filters → suggest flagging
