# Qatar Airways Flight Scanner

Scrapes the Qatar Airways BFF API to find the cheapest flights between two
cities, with support for drift scans (varying dates), mixed-cabin
recommendations (economy one way, business the other), price calendar,
trend analysis, and historical cache archiving.

## Quick start

1. Install dependencies: `pip install requests rich`
2. Paste browser headers: `python flights.py --paste`
   (Open Chrome devtools on qatarairways.com, copy the `request.headers` from
   any flight search API call.)
3. Search a single pair: `python flights.py`
4. Scan ±N days around both dates: `python flights.py --drift 5`
5. See mixed-cabin recommendations: `python flights.py --drift 5 --recommend`

## Usage

```
python flights.py [flags]

Flags:
  -o, --origin CODE         Origin airport (default: OSL)
  -d, --destination CODE    Destination airport (default: BKK)
  --depart YYYY-MM-DD       Outbound date (default: 2026-08-17)
  --ret YYYY-MM-DD          Return date (default: 2026-09-04)
  --drift N                 Shift both dates ±N days, preserve trip length
  --drift-ret N             Shift return date ±N days, keep outbound fixed
  --scan                    Scan a range of outbound dates
  --ob N                    Scan N outbound dates
  --rb N                    Scan N return dates
  --top N                   Top N results per scan (default: 5)
  --max-duration HOURS      Max flight duration (default: 24)
  --max-price CABIN=VAL     Price cap, e.g. ECONOMY=8000,BUSINESS=14000
  --nofilter                Disable price filter
  --all-fares               Include BUSINESS_LITE / BUSINESS_ELITE fares
  --recommend [N]           Show top N mixed-cabin combos after drift
  --prune-cache             Archive stale cache entries + trim to 1 year
  --calendar [MONTH YEAR]   Show price calendar (e.g. "Aug 2026")
  --trends                  Show price trend analysis from collected data
  --price-map               Show day-of-week price table
  --paste                   Paste new browser headers
  --urls                    Show booking links
  --nocache                 Bypass offers cache
  --json                    Output raw JSON
  --delay SEC               Delay between API calls
  --help                    Show this help

Notes:
  --calendar and --trends can be combined: python flights.py --trends --calendar
```
