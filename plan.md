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
- [x] `--recommend N`: wires mixed-cabin table into drift output
- [x] `--urls`: booking URL listing for date pairs
- [x] Cache archival (`--prune-cache`): stale entries (previous days) auto-archived to `cache_archive.json`; archive trimmed to 1 year
- [x] Price calendar (`--calendar [MONTH YEAR]`): monthly grid of min fares from price_map + archive; green=cheap, red=expensive
- [x] Trend analysis (`--trends`): cheapest/most expensive day, volatility, fare class distribution; fed by price_map + archive
- [x] Combined `--calendar --trends`: both views shown together
- [x] Archive-fed analytics: calendar/trends use `cache_archive.json` for per-date prices alongside price_map averages

## Removed

- [x] `--cabin` flag: API always sends `cabinClass: "ECONOMY"` which returns both economy + business fares
- [x] Bad-day filter (`--filter-bad`, `--flag-bad`, `--list-bad`, `bad_patterns.json`): routing filters (max stops, avoid airports) already handle this
