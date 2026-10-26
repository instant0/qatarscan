# T08 — Optional: Playwright warm-up (cookie harvesting)

- **Required**: Optional
- **Depends on**: T02
- **Blocks**: —
- **Reference**: `../task-data.md` §4 (headers), §5 (cookies), §7 (config)

## Objective

Provide a small helper that uses a real browser to warm a QR session and export valid
headers/cookies into `config.json`, so `--paste` is not manual.

## Scope

A separate script/tool (e.g. `tools/warm_session.py`). **Not** part of the scan runtime; it
must not make bulk price requests.

## Steps

1. Open `https://www.qatarairways.com/app/booking/flight-selection?...` in Playwright
   (Firefox profile preferred).
2. Let the page load and Akamai sensor run; optionally perform one normal search.
3. Capture the `flight-offers` request headers + cookies from the browser context.
4. Write the same allowlisted headers/cookies to `config.json` (task-data §4/§5) and set
   `saved_at`, `user_agent`, `source_endpoint="flight-offers"`.
5. Exit. Do not loop searches in this tool.

## Acceptance criteria

- Running the helper produces a `config.json` that passes T02 validation.
- One subsequent live request succeeds.
- The helper performs no bulk scanning.

## Notes

Heaviest option; only if T01–T05 (and optionally T07) are insufficient. Keep it isolated from
the scanner.
