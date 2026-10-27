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
   (See [Obtaining request headers](#obtaining-request-headers) below.)
3. Search a single pair: `python flights.py`
4. Scan ±N days around both dates: `python flights.py --drift 5`
5. See mixed-cabin recommendations: `python flights.py --drift 5 --recommend`

## Obtaining request headers

Request headers **must** be captured from an **un-authenticated** page on the
Qatar Airways website flight scans — do not sign in to your Qatar Airways
account first.

Steps:

1. Open a suitable un-authenticated flight-scans page (example below).
2. Open Chrome devtools → **Network** tab.
3. Set the filter to **XHR**.
4. Run a flight search, then search the requests for `flight-offers`.
5. Select the `flight-offers` request and copy its request headers as raw
   text: the optional request line plus one `Header: value` line per header.

`--paste` accepts raw request headers, not a fetch/JSON object. The accepted
format is:

- An optional first line with the raw request line, e.g.
  `POST /dapi/public/bff/web/flight-search/flight-offers HTTP/2`. If present,
  the method must be `POST`, the path must be the `flight-offers` path, and a
  `Host` header must be `www.qatarairways.com` if supplied.
- Then one `Header: value` per line. A `Cookie: a=b; c=d` line is parsed into
  the scanner's cookie allowlist. Do not paste a `fetch(...)` call or a JSON
  object such as `{"headers": {...}}`; those are not parsed.

For example:

```
POST /dapi/public/bff/web/flight-search/flight-offers HTTP/2
Host: www.qatarairways.com
User-Agent: Mozilla/5.0 (...) Firefox/158.0
session-id: <session id>
X-AssignedDeviceID: <device id>
nbx_fs_api_key: <api key>
Cookie: _abck=...~0~...; bm_sz=...; BIGipServer...=...
```

A headers-only paste (no request line) is accepted, but the request source is
reported as unverified (`source=unknown`). A paste with a request line is
validated against the QR `flight-offers` endpoint before it is saved.

An example of a suitable un-authenticated flight-scans page:

```
https://www.qatarairways.com/app/booking/flight-selection?widget=QR&searchType=F&addTaxToFare=Y&minPurTime=0&selLang=en&tripType=R&fromStation=OSL&toStation=BKK&departing=2026-12-27&returning=2027-01-07&bookingClass=E&adults=1&children=0&infants=0&ofw=0&teenager=0&flexibleDate=off&allowRedemption=N
```

## Usage

```
python flights.py [flags]

Flags:
  -o, --origin CODE         Origin airport (default: OSL)
  -d, --destination CODE    Destination airport (default: BKK)
  --depart YYYY-MM-DD       Outbound date (default: today + 30 days)
  --ret YYYY-MM-DD          Return date (default: departure + 18 days)
  --drift N                 Shift both dates ±N days, preserve trip length (0 = exact pair)
  --drift-ret N             Shift return date ±N days, keep outbound fixed (0 = exact pair)
  --scan                    Scan a range of outbound dates
  --ob N                    Scan outbound ±N days (0 = exact date)
  --rb N                    Scan return ±N days (0 = exact date)
  --min-trip N              Minimum trip length in days (0 or greater; default: 8)
  --weight {price,duration,balanced}
                            Scan optimization weight (default: price)
  --top N                   Top N results per scan (positive; default: 5)
  --max-duration HOURS      Max flight duration in hours (positive; default: 25)
  --max-price CABIN=VAL     Price cap, e.g. ECONOMY=8000,BUSINESS=14000
                            (only ECONOMY/BUSINESS; integers > 0; case-insensitive)
  --nofilter                Disable price filter
  --all-fares               Include BUSINESS_LITE / BUSINESS_ELITE fares
  --recommend [N]           Show top N mixed-cabin combos after drift (positive; default: 10)
  --prune-cache             Archive entries dated before today and dedupe/trim the archive to 365 days (no config needed)
  --calendar [MONTH YEAR]   Show price calendar (e.g. "Aug 2026"; no config needed)
  --trends                  Show price trend analysis from collected data (no config needed)
  --price-map               Show day-of-week price table (no config needed)
  --paste                   Paste new browser headers
  --urls                    Show booking links (single, scan, and drift modes)
  --nocache                 Bypass offers cache
  --json                    Output raw JSON (links go to stderr with --urls)
  --delay SEC               Base spacing between live calls in seconds; must be positive (default 45, jittered -5/+10)
  --max-retries {0,1}       Retry transient 5xx/timeout failures once (default 0; edge/auth/rate/budget are never retried)
  --budget N                Max live POST calls per run; non-negative, 0 = unlimited. Failed/timed-out calls count; cache hits do not.
  --force                   Ignore an active rate-limit cooldown for this run
  --clear-block             Clear the persisted rate-limit cooldown and exit
  --help                    Show this help
```

Notes:
  Corrupt/unreadable rate-limit state fails closed. `--force` does not bypass unusable
  state; use `--clear-block` to reset it.
  `--calendar` and `--trends` can be combined: `python flights.py --trends --calendar`.
  `--price-map`, `--prune-cache`, `--calendar`, and `--trends` are local-only and do not
  require `config.json`; paste and live searches still need a valid config.
  If `config.json` is corrupt or unreadable it is quarantined and live scans require
  `--paste`; automatically refreshed cookies are not written over it. Forbidden transport
  headers in an old config (`Host`, `Content-Length`, `Connection`, `TE`,
  `Transfer-Encoding`, `Accept-Encoding`, `Cookie`) are dropped before sending. An invalid,
  unknown, or stale `_abck` prints a `--paste` hint.
  Corrupt offers-cache, archive, price-map, and currency JSON is copied to
  `<file>.corrupt-<timestamp>` and treated as empty/default when the file is read for update;
  read-only views ignore invalid files. Nested JSON shapes are validated before use.
  `--max-price` accepts only the cabins in the scanner's max-price map (currently
  `ECONOMY` and `BUSINESS`). Duplicate cabins use the last value. Empty, non-integer,
  unknown-cabin, and non-positive values are rejected.
  `--budget 0` means unlimited; negative budgets and non-positive delays are rejected.
  Numeric ranges are validated: `--ob`, `--rb`, `--drift`, `--drift-ret`, and
  `--min-trip` accept 0 or greater, while `--top`, `--max-duration`, and
  `--recommend` require positive integers. Invalid values exit with code 2 and a
  clear argparse error.
  Default dates are rolling: departure is today + 30 days and return is
  departure + 18 days. Explicit `--depart`/`--ret` override them.
  `--calendar` and `--trends` reuse a stale cached FX rate in-process when the FX
  endpoint fails, so local views do not retry once per offer.
  With `--json`, stdout contains only JSON. If `--urls` is also set, booking links are
  written to stderr so the JSON stays machine-readable. `--json` cannot be combined with
  `--drift` or `--drift-ret` (exit code 2).
  Scan and drift runs report partial transient failures as `N/M requests failed` while
  keeping exit code 0 (unless the run aborted). If every request fails transiently, the run
  exits 1 instead of reporting no results. Cache hits and skipped dates are not failures.

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
