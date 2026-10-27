import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

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
    def test_valid_paste_and_config_validation(self):
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
        cfg = {
            "headers": headers,
            "cookies": cookies,
            "source_endpoint": source,
        }
        self.assertTrue(flights.validate_config(cfg))
        self.assertFalse(flights.validate_config({"headers": {}, "cookies": {}}))

    def test_dropped_headers_and_cookie_allowlist(self):
        headers, cookies, _ = flights.parse_pasted_headers(PASTE)
        for name in (
            "Host",
            "Content-Length",
            "Connection",
            "TE",
            "Accept-Encoding",
            "Cookie",
        ):
            with self.subTest(header=name):
                self.assertNotIn(name, headers)
        for name in ("_abck", "bm_sz", "ak_bmsc", "AKA_A2", "TS01753a24"):
            with self.subTest(cookie=name):
                self.assertIn(name, cookies)
        self.assertIn(
            "BIGipServeroriginazqacwww11933911-dapi-https-pool", cookies
        )
        for name in ("_ga", "ai_session", "OptanonConsent"):
            with self.subTest(excluded_cookie=name):
                self.assertNotIn(name, cookies)
        self.assertEqual(flights._abck_state(cookies), "valid")

    def test_invalid_paste_inputs_rejected(self):
        cases = {
            "wrong_endpoint": PASTE.replace(
                "flight-search/flight-offers", "flight-search/fare-calendars"
            ),
            "wrong_host": PASTE.replace(
                "Host: www.qatarairways.com", "Host: example.com"
            ),
            "missing_nbx_fs_api_key": without_line(PASTE, "nbx_fs_api_key"),
            "missing_user_agent": without_line(PASTE, "User-Agent"),
        }
        for name, text in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(flights.PasteError):
                    flights.parse_pasted_headers(text)

    def test_headers_only_variants(self):
        with self.subTest("known host"):
            text = without_line(PASTE, "POST ")
            headers, _, source = flights.parse_pasted_headers(text)
            self.assertEqual(source, "unknown")
            self.assertIn("session-id", headers)
        with self.subTest("unknown host"):
            text = without_line(PASTE, "POST ").replace(
                "Host: www.qatarairways.com", "Host: example.com"
            )
            headers, _, source = flights.parse_pasted_headers(text)
            self.assertEqual(source, "unknown")
            self.assertNotIn("Host", headers)

    def test_absolute_url_provenance(self):
        text = PASTE.replace(
            "POST /dapi/public/bff/web/flight-search/flight-offers HTTP/2",
            "POST https://www.qatarairways.com/dapi/public/bff/web/"
            "flight-search/flight-offers HTTP/2",
        )
        text = without_line(text, "Host")
        _, _, source = flights.parse_pasted_headers(text)
        self.assertEqual(source, "flight-offers")
        bad_path = text.replace("flight-search/flight-offers", "flight-search/fare-calendars")
        with self.assertRaises(flights.PasteError):
            flights.parse_pasted_headers(bad_path)
        bad_host = text.replace("www.qatarairways.com", "example.com")
        with self.assertRaises(flights.PasteError):
            flights.parse_pasted_headers(bad_host)


class AbckValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config_path = os.path.join(self.tmp.name, "config.json")
        self.old_config_file = flights.CONFIG_FILE
        self.old_data_lock = flights.DATA_LOCK_FILE
        flights.CONFIG_FILE = self.config_path
        flights.DATA_LOCK_FILE = os.path.join(self.tmp.name, ".data_state.lock")
        self.addCleanup(setattr, flights, "CONFIG_FILE", self.old_config_file)
        self.addCleanup(setattr, flights, "DATA_LOCK_FILE", self.old_data_lock)
        self.headers = {name: "value" for name in flights.REQUIRED_HEADERS}

    def _cfg(self, abck=None, saved_at=None):
        cookies = {"bm_sz": "x", "ak_bmsc": "x"}
        if abck is not None:
            cookies["_abck"] = abck
        cfg = {
            "headers": dict(self.headers),
            "cookies": cookies,
            "source_endpoint": "flight-offers",
        }
        if saved_at is not None:
            cfg["saved_at"] = saved_at
        return cfg

    def _validate(self, cfg):
        with patch.object(flights.log, "print") as log_print:
            self.assertTrue(flights.validate_config(cfg))
        return " ".join(str(call) for call in log_print.call_args_list)

    def _write(self, cfg):
        with open(self.config_path, "w") as f:
            json.dump(cfg, f)

    def test_abck_validation_states(self):
        cases = (
            ("valid", self._cfg("VAL~0~rest"), (), ("invalid", "unknown", "stale")),
            ("invalid", self._cfg("VAL~-1~rest"), ("invalid", "--paste"), ()),
            ("unknown", self._cfg("something-else"), ("unknown", "--paste"), ()),
            ("missing", self._cfg(), ("_abck", "unknown", "--paste"), ()),
        )
        for name, cfg, expected_present, expected_absent in cases:
            with self.subTest(state=name):
                messages = self._validate(cfg)
                for text in expected_present:
                    self.assertIn(text, messages)
                for text in expected_absent:
                    self.assertNotIn(text, messages)

    def test_stale_valid_abck_warns_and_preserves_saved_at(self):
        saved_at = (datetime.now() - timedelta(hours=30)).isoformat()
        cfg = self._cfg("VAL~0~rest", saved_at=saved_at)
        self._write(cfg)
        messages = self._validate(cfg)
        self.assertIn("stale", messages)
        self.assertIn("--paste", messages)
        with open(self.config_path) as f:
            persisted = json.load(f)
        self.assertEqual(persisted["saved_at"], saved_at)
        self.assertEqual(persisted["last_abck_state"], "valid")

    def test_explicit_paste_replaces_quarantined_config(self):
        with open(self.config_path, "w") as f:
            f.write("{not json")
        with patch.object(flights.log, "print"):
            self.assertIsNone(flights.load_config())
        with patch.object(flights.log, "print"):
            with patch.object(flights.sys, "stdin", io.StringIO(PASTE)):
                cfg = flights.prompt_paste_headers()
        self.assertEqual(cfg["source_endpoint"], "flight-offers")
        with open(self.config_path) as f:
            saved = json.load(f)
        self.assertEqual(saved["headers"]["session-id"], cfg["headers"]["session-id"])
        self.assertEqual(saved["source_endpoint"], "flight-offers")
        self.assertEqual(saved["last_abck_state"], "valid")
        self.assertIn("saved_at", saved)
        corrupt = [n for n in os.listdir(self.tmp.name) if ".corrupt-" in n]
        self.assertEqual(len(corrupt), 1)


if __name__ == "__main__":
    unittest.main()
