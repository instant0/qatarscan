import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import flights


PASTE = """POST /dapi/public/bff/web/flight-search/flight-offers HTTP/2
Host: www.qatarairways.com
User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:158.0) Gecko/20100101 Firefox/158.0
Accept: application/json, text/plain, */*
Accept-Language: en
Accept-Encoding: gzip, deflate, br, zstd
Content-Type: application/json
qr-lang: en
session-id: 6b0a6ca4-8105-45c4-8693-5f3c6a4c747a
X-AssignedDeviceID: 3rIRfGJdMPzXU8E9hqP57vXGQgP9UpPb
nbx_fs_api_key: 74f2474702784207a9785eb3ef8ae4e4
Request-Id: |d2eccacef76c42118de5028634895b72.c33df7deb26f4e1f
traceparent: 00-d2eccacef76c42118de5028634895b72-c33df7deb26f4e1f-01
Content-Length: 305
Origin: https://www.qatarairways.com
Sec-GPC: 1
Connection: keep-alive
Referer: https://www.qatarairways.com/app/booking/flight-selection?widget=QR
Cookie: TS01753a24=abc; BIGipServeroriginazqacwww11933911-dapi-https-pool=1.2.3.4; AKA_A2=A; _abck=VAL~0~rest; bm_sz=xyz; ak_bmsc=abc; _ga=GA1.1; ai_session=def; OptanonConsent=ghi
Sec-Fetch-Dest: empty
Sec-Fetch-Mode: cors
Sec-Fetch-Site: same-origin
Pragma: no-cache
Cache-Control: no-cache
TE: trailers"""


def without_line(text, prefix):
    return "\n".join(line for line in text.splitlines() if not line.lower().startswith(prefix.lower()))


class PasteTests(unittest.TestCase):
    def test_valid_paste(self):
        headers, cookies, source = flights.parse_pasted_headers(PASTE)
        self.assertEqual(source, "flight-offers")
        self.assertTrue(headers["User-Agent"].endswith("Firefox/158.0"))
        self.assertEqual(headers["session-id"], "6b0a6ca4-8105-45c4-8693-5f3c6a4c747a")
        self.assertEqual(headers["X-AssignedDeviceID"], "3rIRfGJdMPzXU8E9hqP57vXGQgP9UpPb")
        self.assertEqual(headers["nbx_fs_api_key"], "74f2474702784207a9785eb3ef8ae4e4")
        self.assertEqual(headers["Accept-Language"], "en")
        self.assertEqual(headers["Sec-GPC"], "1")
        self.assertEqual(headers["Sec-Fetch-Site"], "same-origin")
        self.assertEqual(headers["Pragma"], "no-cache")
        self.assertEqual(headers["Cache-Control"], "no-cache")

    def test_drops_non_browser_headers(self):
        headers, _, _ = flights.parse_pasted_headers(PASTE)
        for name in ("Host", "Content-Length", "Connection", "TE", "Accept-Encoding", "Cookie"):
            self.assertNotIn(name, headers)

    def test_cookie_allowlist(self):
        _, cookies, _ = flights.parse_pasted_headers(PASTE)
        for name in ("_abck", "bm_sz", "ak_bmsc", "AKA_A2", "TS01753a24"):
            self.assertIn(name, cookies)
        self.assertIn(
            "BIGipServeroriginazqacwww11933911-dapi-https-pool", cookies
        )
        for name in ("_ga", "ai_session", "OptanonConsent"):
            self.assertNotIn(name, cookies)
        self.assertEqual(flights._abck_state(cookies), "valid")

    def test_wrong_endpoint_rejected(self):
        text = PASTE.replace("flight-search/flight-offers", "flight-search/fare-calendars")
        with self.assertRaises(flights.PasteError):
            flights.parse_pasted_headers(text)

    def test_wrong_host_rejected(self):
        text = PASTE.replace("Host: www.qatarairways.com", "Host: example.com")
        with self.assertRaises(flights.PasteError):
            flights.parse_pasted_headers(text)

    def test_missing_required_header_rejected(self):
        with self.assertRaises(flights.PasteError):
            flights.parse_pasted_headers(without_line(PASTE, "nbx_fs_api_key"))
        with self.assertRaises(flights.PasteError):
            flights.parse_pasted_headers(without_line(PASTE, "User-Agent"))

    def test_headers_only_allowed(self):
        text = without_line(PASTE, "POST ")
        headers, _, source = flights.parse_pasted_headers(text)
        self.assertEqual(source, "unknown")
        self.assertIn("session-id", headers)

    def test_validate_config(self):
        headers, cookies, source = flights.parse_pasted_headers(PASTE)
        cfg = {
            "headers": headers,
            "cookies": cookies,
            "source_endpoint": source,
        }
        self.assertTrue(flights.validate_config(cfg))
        broken = {"headers": {}, "cookies": {}}
        self.assertFalse(flights.validate_config(broken))


if __name__ == "__main__":
    unittest.main()
