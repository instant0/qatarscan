# T07 — Optional: TLS impersonation (`curl_cffi`)

- **Required**: Optional
- **Depends on**: T03
- **Blocks**: —
- **Reference**: `../task-data.md` §4 (headers), §8 (errors)

## Objective

Add an optional transport that matches a real browser's TLS/HTTP2 fingerprint, for cases
where a genuinely fresh, warm paste still gets a first-call 403.

## Scope

Transport layer only. No changes to payloads, parsing, scans, or rate policy.

## Steps

1. Add a backend abstraction to `HttpClient`: `backend="requests" | "curl_cffi"`.
2. `curl_cffi` path: `curl_cffi.requests.Session(impersonate="firefox")`.
3. Ensure the `User-Agent` matches the impersonation profile. Do not mix a Firefox UA with a
   Chrome profile.
4. Keep the same cookie/session persistence and trace logic; `curl_cffi` handles br/zstd.
5. Fallback: if `curl_cffi` is not installed, use `requests` and warn once.
6. CLI: `--no-impersonate` forces `requests`; default remains `requests` unless opted in
   (or auto-select if installed — document the choice).
7. Update README install line: `pip install requests rich curl_cffi`.

## Acceptance criteria

- With `curl_cffi` installed, a request succeeds with the same headers/cookies.
- UA and impersonation profile are consistent.
- Without `curl_cffi`, behavior is unchanged and a warning is printed once.
- No changes to cache/parse/scan behavior.

## Tests

- Unit: backend selection and fallback warning.
- Manual: one live request with each backend after a fresh paste; compare status.

## Notes

Only pursue this if T01–T05 do not solve first-call 403s. It adds a dependency and profile
maintenance.
