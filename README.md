# Qatar Airways Flight Scanner

Scrapes the Qatar Airways BFF API to find the cheapest flights between two
cities, with support for drift scans (varying dates), mixed-cabin
recommendations (economy one way, business the other), price calendar,
trend analysis, and historical cache archiving.

This project is an independent tool that automates requests made by an
authenticated user. Users must supply their own valid browser session and
are responsible for complying with the Terms of Service of the data
provider. This project does not include credentials, cookies, or access to
any proprietary data.

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
```

Notes:
  `--calendar` and `--trends` can be combined: `python flights.py --trends --calendar`

## Examples

### Basic Flight Search

Search for flights from a specific origin to a destination on given dates:

```
python flights.py --origin OSL --destination BKK --depart 2026-08-17 --ret 2026-09-04
```

Sample Result:

```
  Date Pair                 OB Econ   OB Biz   RB Econ   RB Biz
 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  2026-08-17 → 2026-09-04   M 5768    -        M 5339*   -
```

### Price map (`--price-map`)

Day-of-week price breakdown per direction:

```
BKK → OSL
Day  Flight  Dep   Arr    Xfer Stp          M          H          Y          P          R          D          J
Mon  QR837   02:30 14:30  215m   1  5198(10)  5981(10) 10860(10)         -  17205(3) 21631(10)         -
Mon  QR829   03:00 14:30  185m   1   5232(1)  7284(10) 10860(10)         -  17205(2) 21631(10)         -
Mon  QR835   20:00 14:30  605m   1  5222(10)  5981(10) 10860(10)         - 17159(10) 21631(10)         -
Tue  QR837   02:30 14:30  215m   1  5119(10)  5908(10) 10606(10)         -  17205(1) 21631(10)         -
...
```

### Calendar (`--calendar [MONTH YEAR]`)

Monthly grid of cheapest economy fares:

```
August 2026 — OSL → BKK
  Mon   Tue   Wed   Thu   Fri   Sat   Sun
                                5737  5717
  5675  5490  5490  5626  5651  5737  5717
  5675  5490  5490  5744  5833  5905  5780
  5695  5588  5539  5666  5617  5645  5717
  5675  5490  5490  5626  5651  5737  5717
  5675

August 2026 — BKK → OSL
  Mon   Tue   Wed   Thu   Fri   Sat   Sun
                                5177  5292
  5198  5013  5013  5106  5339  5177  5292
  5198  5013  5013  5106  5339  5177  9407
  7706  5258  5160  5249  6768  5466  5443
  5208  5151  5160  5200  5443  5450  5443
  5220
```

### Trend analysis (`--trends`)

Day-of-week averages, min/max, volatility, and fare class distribution:

```
Price Trends — OSL → BKK
   Cheapest day:  Sun (avg 11031)
   Most expensive: Fri (avg 11871)
   Volatility:     ±63.8%
   Fare classes:  BUSINESS_COMFORT, ECONOMY_CLASSIC, ECONOMY_COMFORT, ECONOMY_CONVENIENCE

  Day     Avg    Min     Max   Samples
 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Mon   11547   5675   23483        16
  Tue   11511   5490   23483        16
  Wed   11503   5490   23478        20
  Thu   11546   5626   23483        20
  Fri   11871   5617   27898        24
  Sat   11846   5645   25906        20
  Sun   11031   5717   27906        18
```

### Mixed-cabin recommendations (`--drift N --recommend`)

Drift scan followed by top-N mixed-cabin combos (economy one way, business the other — or all-economy if cheaper):

```
                    Drift scan — OSL→BKK (max 25h)

  Date Pair                 OB Econ   OB Biz   RB Econ   RB Biz
 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  2026-08-15 → 2026-09-02   M 5863    -        M 5106*   R 17205* Pod
  2026-08-16 → 2026-09-03   M 5717    -        M 5106*   R 17205* Pod
  2026-08-17 → 2026-09-04   M 5768    -        M 5339*   -
  2026-08-18 → 2026-09-05   M 5490    -        M 5339*   R 17205* Pod
  2026-08-19 → 2026-09-06   M 5490    -        M 5339*   R 17205* Pod

                Best Mixed-Cabin Combos (sorted by total price)

  #   Combo       Date Pair          OB                RB                Total
 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  1   Econ+Econ   2026-08-16 →       M 5717  17h 45m   M 5106*  16h      10823
                  2026-09-03                           20m
  2   Econ+Econ   2026-08-18 →       M 5490  16h 20m   M 5339*  16h      10829
                  2026-09-05                           20m
  3   Econ+Econ   2026-08-19 →       M 5490  17h 45m   M 5339*  23h      10829
                  2026-09-06                           30m
```
