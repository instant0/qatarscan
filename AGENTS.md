# Project Overview

This is a **Qatar Airways flight price scanner**. It queries the QR BFF API to find cheap flights — primarily OSL↔BKK. Not a seat map project; seat maps are one small feature.

## Entry point

`python flights.py [flags]` — see README.md for all flags.

## Key concepts

- **API**: POST `https://www.qatarairways.com/dapi/public/bff/web/flight-search/flight-offers` — session auth via browser headers (refresh with `--paste`, expires ~24h)
- **Search**: outbound + return are **separate API calls** (one itinerary per request). API always sends `cabinClass: "ECONOMY"` — returns BOTH economy and business fares. No `--cabin` flag.
- **Cabin + fare filtering**: `PREFERENCES` dict controls which fare families/cabins are shown; LITE/ELITE hidden by `show_fares` + `filter_dirt_cheap()`
- **Drift scanning** (`--drift N`): shift both dates ±N days preserving trip length. `--drift-ret N`: shift only return date
- **Price map** (`--price-map`): day-of-week trends stored in `price_map.json`
- **Cache archival** (`--prune-cache`): stale entries (previous days) auto-archived to `cache_archive.json`; archive trimmed to 1 year
- **Price calendar** (`--calendar [MONTH YEAR]`): monthly grid of min fares from price_map + archive; green=cheap, red=expensive
- **Trend analysis** (`--trends`): cheapest/most expensive day, volatility, fare class distribution; fed by price_map + archive
- **Mixed-cabin recommendations** (`--recommend`): cheapest economy one way + business the other
- **Layout classification**: maps aircraft code → business class layout type for display in results

## Data files

| File | Purpose |
|------|---------|
| `config.json` | Browser session headers |
| `layouts.json` | Aircraft code → layout mapping |
| `price_map.json` | Day-of-week price trends |
| `offers_cache.json` | Cached API responses (current day only) |
| `cache_archive.json` | Historical API responses (append log, 1-yr retention) |
| `currency.json` | FX rates cache |
| `api.md` | API documentation |
| `plan.md` | Feature plan / backlog |

## Key code locations

- `classify_layout()`: maps aircraft code → layout ("QSuite", "Door", "Pod", "Old", "Unknown")
- `parse_results()`: parses API response into segment objects, calls classify_layout per segment
- `fetch_flights()`: makes HTTP request, handles offers cache
- `archive_stale_cache()`: moves previous-day entries to `cache_archive.json`, prunes >365d
- `_load_archive_prices()`: parses archive into `{date: {direction: {fare: price}}}` for calendar/trends
- `print_price_calendar()`: monthly grid with green/yellow/red color coding
- `print_price_trends()`: day-of-week breakdown with volatility stats
- `find_best_mixed()`: mixed-cabin matcher
- `update_price_map()`: aggregates per-day-of-week price data into `price_map.json`

---

# Seat Map Layout Cross-Reference (July 2026)

## Sources consulted
- seatmaps.com — full fleet seat maps for every QR type
- flightseatmap.com — fleet index, specific flight examples
- FlightQ, PlaneFYI, Flightradar24 — fleet composition data

## Per-type verification

| Code | Aircraft | Our mapping | Verified? | Notes |
|------|----------|-------------|-----------|-------|
| 789 | 787-9 | Door | ✓ | "Premium Suite" w/ privacy door, not full QSuite. `qSuiteEquipped` API flag handles overrides. |
| 35K | A350-1000 | QSuite | ✓ | All QSuite across both configs (46 or 24 J). |
| 359 | A350-900 | Pod | ✓ | Two configs: V.1 = Super Diamond pods (old), V.2 = QSuite (new). `qSuiteEquipped` API flag overrides for V.2. |
| 788 | 787-8 | Pod | ✓ | V.1 = 22 J standard pod, V.2 = 18 J ex-Oman Air Apex suites. |
| 388 | A380-800 | Pod | ✓ | 48 J 1-2-1 staggered + 8 F suites. No QSuite in J. |
| 77W | 777-300ER | Old | ✓ | Six configs — some QSuite, some Old 2-2-2, some mixed. Conservative default; `qSuiteEquipped` overrides. |
| 77L | 777-200LR | Old | ✓ | Two configs mixing Old/QSuite. Conservative default. |
| 332 | A330-200 | Old | ✓ | 2-2-2 angled lie-flat. |
| 333 | A330-300 | Old | ✓ | 2-2-2 angled lie-flat. |

## Missing types — not added
- **A339 (A330-900neo)**: Not in QR's active fleet as of 2026 (orders deferred/cancelled).
- **7M8 (737 MAX 8)**, **320 (A320)**, **32Q (A321neo)**: Narrow-body regional types with different J product. Return "Unknown" — acceptable.

## Code correctness
- `classify_layout()` checks `qSuiteEquipped` flag first (line 117–118), then `by_flight` dict (120–121), then `aircraft` map (122–123). Correct.
- `by_flight` dict is empty by design — populated per-flight when needed.
- `LAYOUT_RANK` ordering is appropriate.

## Conclusion
All current mappings are accurate. No changes needed.
