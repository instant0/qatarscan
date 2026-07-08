# Qatar Airways BFF Flight Search API

## Endpoint

```
POST https://www.qatarairways.com/dapi/public/bff/web/flight-search/flight-offers
```

## Authentication

Required headers extracted from browser session:

| Header | Description |
|--------|-------------|
| `session-id` | UUID session identifier |
| `X-AssignedDeviceID` | Device fingerprint |
| `nbx_fs_api_key` | API key for the BFF |
| `Request-Id` | Request tracing ID |
| `traceparent` | W3C trace context |

Session expires after ~24h. Refresh via browser: DevTools → Network tab → find `flight-offers` request → right-click → Copy Request Headers → paste with `--paste`.

## Request Payload

### Structure

```json
{
  "channel": "WEB_DESKTOP",
  "itineraries": [
    {
      "origin": "OSL",
      "destination": "BKK",
      "departureDate": "2026-08-17",
      "isRequested": true
    }
  ],
  "cabinClass": "ECONOMY",
  "ignoreInvalidPromoCode": true,
  "passengers": [
    { "type": "ADT", "count": 1 }
  ]
}
```

### Key Observations

- **Only ONE itinerary per request.** Send outbound and return as **separate API calls**.
- `isRequested` must be `true` for the itinerary you want results for.
- Adding a second itinerary with `isRequested: false` does nothing — it's ignored.
- `cabinClass` doesn't filter the response. All fare families (Economy and Business) are returned regardless.
- Adding a second both-`true` itinerary returns empty results.

### Example: Two calls for a round trip

**Call 1 — Outbound (OSL → BKK):**
- `itineraries[0].origin = "OSL"`, `.destination = "BKK"`, `.departureDate = "2026-08-17"`, `.isRequested = true`

**Call 2 — Return (BKK → OSL):**
- `itineraries[0].origin = "BKK"`, `.destination = "OSL"`, `.departureDate = "2026-09-04"`, `.isRequested = true`

## Response Structure

```json
{
  "alertMessages": [ ... ],
  "flightOffers": [ ... ],
  "currencies": { ... },
  "dictionaries": { ... },
  "viaStations": [ ... ],
  "requestStationDetails": { ... }
}
```

### `flightOffers[]` — one entry per routing variant

Each offer represents a complete itinerary for the **requested direction only** (outbound or return, not both).

```json
{
  "origin": { "iataCode": "OSL" },
  "destination": { "iataCode": "BKK" },
  "duration": 57000,
  "arrivalDaysDifference": 0,
  "numberOfStops": 1,
  "segments": [ ... ],
  "fareOffers": [ ... ]
}
```

Each offer typically has 1–2 segments (direct or 1-stop via DOH).

### `segments[]` — individual flight legs

```json
{
  "flightId": "SEG-QR176-OSLDOH-2026-08-17-1605",
  "flightNumber": "QR176",
  "vehicle": {
    "code": "789",
    "name": "Boeing 787-9",
    "shortName": "B789",
    "type": "AIRCRAFT"
  },
  "departure": {
    "origin": { "iataCode": "OSL" },
    "dateTime": "2026-08-17T16:05:00"
  },
  "arrival": {
    "destination": { "iataCode": "DOH" },
    "dateTime": "2026-08-17T23:35:00",
    "daysDifference": 0
  },
  "duration": 23400,
  "connectionTime": 8700,
  "qSuiteEquipped": false,
  "stops": []
}
```

**Note:** `departure.location` and `arrival.location` are empty objects. Use `departure.origin.iataCode` / `arrival.destination.iataCode` instead.

### `fareOffers[]` — pricing per fare family

```json
{
  "boundId": "BC1-2-OFR-...",
  "availableSeats": 9,
  "price": {
    "base": 717,
    "total": 3707,
    "currencyCode": "NOK"
  },
  "fareFamilyCode": "ECONOMY_CLASSIC",
  "fareFamilyFeatures": [ ... ],
  "cabinType": "ECONOMY",
  "isLowestFare": true
}
```

### Fare Families

| Code | Cabin | Notes |
|------|-------|-------|
| `ECONOMY_LITE` | ECONOMY | Hand-baggage only |
| `ECONOMY_CLASSIC` | ECONOMY | Standard |
| `ECONOMY_CONVENIENCE` | ECONOMY | More flexibility |
| `ECONOMY_COMFORT` | ECONOMY | Premium economy-like |
| `BUSINESS_LITE` | BUSINESS | |
| `BUSINESS_CLASSIC` | BUSINESS | |
| `BUSINESS_COMFORT` | BUSINESS | |
| `BUSINESS_ELITE` | BUSINESS | |
| `FIRST_ELITE` | FIRST | First class (return only) |

### Currency Variation

Currency depends on **origin country**, not destination:
- OSL → BKK: prices in **NOK** (Norwegian Krone)
- BKK → OSL: prices in **THB** (Thai Baht)
- DOH → XXX: prices in **QAR** (Qatari Riyal)
- LHR → XXX: prices in **GBP** (British Pound)

### Fare Family Variation by Route

Not all fare families are available on all routes:
- BKK → OSL return offers include `FIRST_ELITE` (not seen on outbound)
- Some offers include `BUSINESS_OFFER` (a discounted business fare)
- LITE variants may be excluded on certain routes

## Aircraft Codes

| Code | Aircraft | Layout (Business) |
|------|----------|-------------------|
| `789` | Boeing 787-9 | Door (new business, privacy doors, not QSuite) |
| `788` | Boeing 787-8 | Pod (1-2-1 reverse herringbone) |
| `359` | Airbus A350-900 | Pod (1-2-1 reverse herringbone) |
| `35K` | Airbus A350-1000 | QSuite |
| `388` | Airbus A380-800 | Pod (1-2-1, no QSuite) |
| `77W` | Boeing 777-300ER | Old (2-2-2) or QSuite — depends on config |
| `77L` | Boeing 777-200LR | Old (2-2-2) or QSuite |
| `332` | Airbus A330-200 | Old (2-2-2) |
| `333` | Airbus A330-300 | Old (2-2-2) |

## Example: Complete Round Trip

### Outbound (OSL → BKK, Aug 17)

```
QR176  OSL 16:05 → DOH 23:35  (787-9)
QR834  DOH 02:00 → BKK 12:55  (A380-800)
       or
QR836  DOH 02:30 → BKK 13:25  (A380-800)
```

ECONOMY_CLASSIC: 3,707 NOK
BUSINESS_CLASSIC: 15,263 NOK

### Return (BKK → OSL, Sep 4)

```
QR835  BKK 20:00 → DOH 23:00  (A380-800)
QR175  DOH 08:40 → OSL 14:00  (787-9)
       or
QR839  BKK 09:55 → DOH 13:00  (A380-800)
QR175  DOH 08:40 → OSL 14:00  (787-9)  [next day]
```

ECONOMY_CLASSIC: 18,275 THB (~5,350 NOK)
BUSINESS_CLASSIC: 60,530 THB (~17,700 NOK)

## Observed Patterns (Aug 2026 data)

### Pricing Trends
- **Weekend effect**: Friday/Saturday return flights (BKK→OSL) tend to be 500–1,000 NOK more expensive than weekday returns
- **Business Classic return price floor**: ~16,400 NOK (converted from THB). Rarely dips below this.
- **Economy Classic outbound price range**: 5,000–6,000 NOK for OSL→BKK in August
- **Economy Classic return price range**: 5,000–6,500 THB converted (~5,100–6,200 NOK)
- **Business Comfort (D)** is the only business fare available on OSL→BKK outbound (~23,473 NOK). Business Classic (R) is never seen on outbound — only on return.

### Routing Patterns
- **OSL→BKK**: Always 1-stop via DOH (direct). Two daily options: evening OSL→DOH connecting to midnight/early-morning DOH→BKK
- **BKK→OSL**: Always 1-stop via DOH (direct). Multiple daily options with varying transfer times
- Multi-stop routes (flagged as indirect): typically involve LHR or HEL as additional stops
- **Tuesdays for BKK→OSL**: No specific bad pattern observed yet in this data set — all queried return dates had direct (DOH-only) options

### Layout Observations
- **OSL→DOH** leg is always 787-9 (Door layout). This is consistent across all dates queried.
- **DOH→BKK** leg varies: A380-800 (Pod) most common, occasionally A350-900 (Pod)
- **BKK→DOH** leg varies: A380-800 (Pod) or 787-9 (Door)
- **DOH→OSL** leg is always 787-9 (Door)
- **No QSuite** observed on any OSL or BKK route in the August data
- Business on outbound (OSL→BKK) is always **Business Comfort (D)** fare type
- Business on return (BKK→OSL) can be **Business Classic (R)** or **Business Comfort (D)**

### Cache & Performance
- First drift run: ~2 min for drift-ret 7 (15 date pairs, 30 API calls, 10s delays between non-cached calls)
- Subsequent runs: ~3s (all cached)
- Headers expire after ~24h; re-paste with `--paste`
