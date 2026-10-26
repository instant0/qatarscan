# DEFERRED RESEARCH: Qatar Airways `fare-calendars` endpoint

Status: **Deferred** — not implemented, not part of the active task.
Parent task: `TASK.md` (detail-first `flight-offers` scanning).
Captured: 2026-10-25.

## Why deferred

The app needs full detail per date: all flight combinations, fare families, cabins, layouts,
and mixed-cabin data. `fare-calendars` returns only a `lowestPrice` per date and cannot
supply any of that. A round-trip date pair still needs 2 `flight-offers` calls, which yield
more data. This file is retained only in case a price-only discovery mode is wanted later.

## Endpoint

```
POST https://www.qatarairways.com/dapi/public/bff/web/flight-search/fare-calendars
```

Same auth headers/cookies as `flight-offers`. Response covers a 7-date window.

## Request bodies

### Outbound calendar (standalone)

```json
{
  "channel": "WEB_DESKTOP",
  "itineraries": [
    {"origin": "OSL", "destination": "BKK", "departureDate": "2026-12-26", "isRequested": true},
    {"origin": "BKK", "destination": "OSL", "departureDate": "2027-01-05", "isRequested": false}
  ],
  "cabinClass": "ECONOMY",
  "ignoreInvalidPromoCode": true,
  "passengers": [{"type": "ADT", "count": 1}],
  "flexibility": 3
}
```

### Return calendar (requires a selected outbound fare)

```json
{
  "channel": "WEB_DESKTOP",
  "itineraries": [
    {"origin": "OSL", "destination": "BKK", "departureDate": "2026-12-26", "isRequested": false},
    {"origin": "BKK", "destination": "OSL", "departureDate": "2027-01-06", "isRequested": true}
  ],
  "cabinClass": "ECONOMY",
  "ignoreInvalidPromoCode": true,
  "passengers": [{"type": "ADT", "count": 1}],
  "selectedOutboundFareOfferId": "BC1-2-OFR-1681423349495410-3-3-1",
  "flexibility": 3
}
```

### Semantics

- `isRequested` selects which leg the calendar covers (outbound = first, return = second).
- `flexibility: 3` = **±3-day inclusive window** (`d−3 … d+3`, 7 dates), centered on the
  `isRequested` itinerary's `departureDate`.
- The return calendar requires `selectedOutboundFareOfferId`, which is a
  `fareOffers[].boundId` from a prior outbound `flight-offers` response. The outbound
  calendar does not need it.
- The body contains no `oBoundId`; that Referer parameter is separate page context.
- Always derive dates from the real request arguments, never from these samples.
- `moreDates` may contain additional dates; schema unverified.

## Response schema

### Outbound response (OSL→BKK)

```json
{
  "faresFromCalendar": [
    {"date": "2026-12-24", "lowestPrice": 9003, "currencyCode": "NOK"},
    {"date": "2026-12-25", "lowestPrice": 7835, "currencyCode": "NOK"},
    {"date": "2026-12-26", "lowestPrice": 9281, "currencyCode": "NOK"},
    {"date": "2026-12-27", "lowestPrice": 7835, "currencyCode": "NOK"},
    {"date": "2026-12-28", "lowestPrice": 10055, "currencyCode": "NOK"},
    {"date": "2026-12-29", "lowestPrice": 7533, "currencyCode": "NOK"},
    {"date": "2026-12-30", "lowestPrice": 6512, "currencyCode": "NOK"}
  ],
  "moreDates": [],
  "requestStationDetails": {"BKK": {"countryCode": "TH"}, "OSL": {"countryCode": "NO"}}
}
```

### Return response (BKK→OSL)

```json
{
  "faresFromCalendar": [
    {"date": "2027-01-03", "lowestPrice": 8653, "currencyCode": "NOK"},
    {"date": "2027-01-04", "lowestPrice": 9161, "currencyCode": "NOK"},
    {"date": "2027-01-05", "lowestPrice": 9161, "currencyCode": "NOK"},
    {"date": "2027-01-06", "lowestPrice": 8351, "currencyCode": "NOK"},
    {"date": "2027-01-07", "lowestPrice": 8351, "currencyCode": "NOK"},
    {"date": "2027-01-08", "lowestPrice": 8629, "currencyCode": "NOK"},
    {"date": "2027-01-09", "lowestPrice": 9439, "currencyCode": "NOK"}
  ],
  "moreDates": [],
  "requestStationDetails": {"BKK": {"countryCode": "TH"}, "OSL": {"countryCode": "NO"}}
}
```

A second return sample (base `2027-01-07`) returned `2027-01-04 … 2027-01-10`. A controlled
sample with base `2027-01-08` returned `2027-01-05 … 2027-01-11`; overlapping dates matched
exactly.

### Confirmed

- Response is a flat `faresFromCalendar[]` of `{date, lowestPrice, currencyCode}`.
- Window rule: 7 dates centered on the requested leg's `departureDate`.
- `lowestPrice` = cheapest **one-way** fare for that date/cabin. Cross-check:
  `2026-12-29` returned `7533`, matching the cheapest economy `flight-offers` fare
  (`ECONOMY_CONVENIENCE`, `total: 7533`, `isLowestFare: true`).
- Price basis: `lowestPrice` matches `price.total` (taxes included), not `price.base`.
- Currency: both directions returned `NOK`, even BKK→OSL. It follows the journey/session
  market, not the calendared leg's origin. Honor `currencyCode` and convert via the existing
  FX helper.
- Fare family is unknown; it may be a hidden LITE fare. Treat the price as a lower bound.
- `boundId` is opaque and short-lived; fetch and use it in the same run.

## Proposed builder

```python
def build_calendar_payload(origin, destination, departure_date, return_date,
                           flexibility=3, request_return=False,
                           selected_outbound_fare_offer_id=None):
    if request_return:
        req = {"origin": destination, "destination": origin,
               "departureDate": return_date, "isRequested": True}
        other = {"origin": origin, "destination": destination,
                 "departureDate": departure_date, "isRequested": False}
    else:
        req = {"origin": origin, "destination": destination,
               "departureDate": departure_date, "isRequested": True}
        other = {"origin": destination, "destination": origin,
                 "departureDate": return_date, "isRequested": False}
    payload = {
        "channel": "WEB_DESKTOP",
        "itineraries": [req, other],
        "cabinClass": "ECONOMY",
        "ignoreInvalidPromoCode": True,
        "passengers": [{"type": "ADT", "count": 1}],
        "flexibility": flexibility,
    }
    if request_return:
        if not selected_outbound_fare_offer_id:
            raise ValueError("return calendar requires selectedOutboundFareOfferId")
        payload["selectedOutboundFareOfferId"] = selected_outbound_fare_offer_id
    return payload
```

## Selecting the outbound fare

Pick the **minimum-total** fare for the desired cabin from `flight-offers` and use its
`boundId` as `selectedOutboundFareOfferId`. Example: `2026-12-29` cheapest economy is
`7533` NOK with `boundId` `BC1-2-OFR-...-1-1-1`.

## Integration notes (if revived)

Calendar rows are price-only and **cannot** feed `update_price_map()`: that function requires
`fare` and `segments` (`flights.py:452-457`).

Design:

- New `calendar_cache.json`:
  ```json
  {"OSL_BKK": {"2026-12-29": {"price": 7533, "currency": "NOK", "fetched_at": "ISO"}}}
  ```
- Load into the shape `print_price_calendar()` understands
  (`{date: {direction: {fare: price}}}`) with a synthetic `CALENDAR_LOWEST` fare key.
- Merge overlapping windows and keep the **minimum** per date.
- Cache key for return calendars must include the selected outbound `boundId` (otherwise a
  later baseline reuses stale prices).
- Handle clipped windows (fewer than 7 dates) and non-empty `moreDates`.
- Relax `print_price_calendar()`'s `price_map.json`-only early return
  (`flights.py:670-672`).
- TTL: current day (like `offers_cache.json`).
- Calendar data cannot populate the main results table, layout classification, or
  `--recommend`.

## Tests if revived

- Window rule: `d±3`, 7 dates.
- `lowestPrice` equals `price.total` for the cheapest fare.
- Merge/dedupe across overlapping windows keeps the minimum.
- `calendar_cache` maps into `print_price_calendar()`/`_min_fare_for_date()`.
- `update_price_map()` ignores calendar-only rows.
- Return calendar requires `selectedOutboundFareOfferId`.

## Risks / open questions

- Calendar calls consume the same Akamai budget as `flight-offers`; do not assume they are
  cheaper.
- Price-only; hidden fare family; no layout/segment data.
- `boundId` may expire.
- Does `cabinClass` filter the calendar (business calendar possible)?
- Are larger `flexibility` values accepted?
- Exact `moreDates` schema.

## Related

- Active task: `TASK.md`.
- API reference: `api.md`.
- Code: `flights.py` (`update_price_map`, `print_price_calendar`, `_min_fare_for_date`).
