import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import flights


class FakeCookieJar(dict):
    def get_dict(self):
        return dict(self)


class FakeResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text

    def json(self):
        return json.loads(self.text)


class FakeSession:
    def __init__(self):
        self.cookies = FakeCookieJar()
        self.last = None
        self.calls = 0

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls += 1
        self.last = {
            "url": url,
            "headers": dict(headers),
            "json": json,
            "timeout": timeout,
        }
        self.cookies.update({"BIGipServernew": "5.6.7.8"})
        return FakeResponse(200, '{"flightOffers": []}')


class AnalyticsSession(FakeSession):
    def post(self, url, headers=None, json=None, timeout=None):
        response = super().post(url, headers=headers, json=json, timeout=timeout)
        self.cookies.update({"_ga": "GA1.1", "OptanonConsent": "yes"})
        return response


class TraceHeaderTests(unittest.TestCase):
    def test_trace_pair_matches_and_rotates(self):
        first = flights.make_trace_headers()
        second = flights.make_trace_headers()
        self.assertNotEqual(first, second)
        trace_id, span_id = first["Request-Id"].lstrip("|").split(".")
        self.assertEqual(len(trace_id), 32)
        self.assertEqual(len(span_id), 16)
        self.assertEqual(
            first["traceparent"], f"00-{trace_id}-{span_id}-01"
        )


class RefererTests(unittest.TestCase):
    def test_build_referer_contains_stations_and_dates(self):
        url = flights.build_referer(
            "OSL", "BKK", "2026-12-29", "2027-01-15"
        )
        parsed = urlparse(url)
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "www.qatarairways.com")
        self.assertEqual(parsed.path, "/app/booking/flight-selection")
        query = parse_qs(parsed.query)
        self.assertEqual(query["fromStation"], ["OSL"])
        self.assertEqual(query["toStation"], ["BKK"])
        self.assertEqual(query["departing"], ["2026-12-29"])
        self.assertEqual(query["returning"], ["2027-01-15"])
        self.assertEqual(query["tripType"], ["R"])
        self.assertEqual(query["bookingClass"], ["E"])


class ClassifyBlockTests(unittest.TestCase):
    def test_classify_block_cases(self):
        html = "<html><title>Access Denied</title>errors.edgesuite.net</html>"
        cases = (
            ("success", 200, "{}", None),
            ("edge", 403, html, "edge"),
            ("expired", 401, '{"error": "session expired"}', "auth"),
            ("forbidden", 403, '{"error": "forbidden"}', "auth"),
            ("server", 500, "server error", "other"),
            ("plain", 403, "plain text", "other"),
        )
        for name, status, body, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(flights.classify_block(status, body), expected)


class HttpClientTests(unittest.TestCase):
    def test_post_offers_uses_session_and_merges_cookies(self):
        caller_cookies = {"_abck": "old"}
        stored_headers = {
            "User-Agent": "Mozilla/5.0 Firefox/158.0",
            "session-id": "session",
            "Content-Type": "application/json",
        }
        session = FakeSession()
        with patch.object(flights.requests, "Session", return_value=session):
            client = flights.HttpClient(stored_headers, caller_cookies)
            client.post_offers({"origin": "OSL"}, "https://example.test/referer")

        first = session.last
        self.assertEqual(first["url"], flights.API_URL)
        self.assertEqual(first["json"], {"origin": "OSL"})
        self.assertEqual(first["timeout"], 30)
        self.assertEqual(
            first["headers"]["User-Agent"], stored_headers["User-Agent"]
        )
        self.assertEqual(first["headers"]["Referer"], "https://example.test/referer")
        for name in ("Host", "Content-Length", "Accept-Encoding"):
            self.assertNotIn(name, first["headers"])
        trace_id, span_id = first["headers"]["Request-Id"].lstrip("|").split(".")
        self.assertEqual(
            first["headers"]["traceparent"], f"00-{trace_id}-{span_id}-01"
        )
        self.assertEqual(caller_cookies["BIGipServernew"], "5.6.7.8")
        self.assertEqual(caller_cookies["_abck"], "old")

        client.post_offers({"origin": "BKK"}, "https://example.test/other")
        second = session.last
        self.assertNotEqual(
            first["headers"]["Request-Id"], second["headers"]["Request-Id"]
        )
        self.assertEqual(session.calls, 2)

    def test_forbidden_stored_headers_are_dropped_at_send_time(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config_path = os.path.join(tmp.name, "config.json")
        old_config_file = flights.CONFIG_FILE
        flights.CONFIG_FILE = config_path
        self.addCleanup(setattr, flights, "CONFIG_FILE", old_config_file)
        stored_headers = {
            "Host": "evil.example",
            "Content-Length": "42",
            "Connection": "keep-alive",
            "TE": "trailers",
            "Transfer-Encoding": "chunked",
            "Accept-Encoding": "gzip, br",
            "Cookie": "_abck=secret",
            "Empty": "",
            "Blank": "   ",
            "NonString": 5,
            "User-Agent": "Mozilla/5.0 Firefox/158.0",
            "session-id": "session",
            "X-AssignedDeviceID": "device",
            "nbx_fs_api_key": "key",
            "Origin": "https://www.qatarairways.com",
            "Referer": "https://old.example/referer",
        }
        with open(config_path, "w") as f:
            json.dump({"headers": stored_headers, "cookies": {}}, f)
        cfg = flights.load_config()
        caller_cookies = cfg["cookies"]
        original = dict(cfg["headers"])
        session = FakeSession()
        with patch.object(flights.requests, "Session", return_value=session):
            client = flights.HttpClient(cfg["headers"], caller_cookies)
            client.post_offers({"origin": "OSL"}, "https://example.test/referer")

        outgoing = session.last["headers"]
        for name in flights.FORBIDDEN_SEND_HEADERS:
            self.assertNotIn(name, outgoing)
        for name in ("Empty", "Blank", "NonString"):
            self.assertNotIn(name, outgoing)
        self.assertEqual(outgoing["User-Agent"], stored_headers["User-Agent"])
        self.assertEqual(outgoing["session-id"], "session")
        self.assertEqual(outgoing["X-AssignedDeviceID"], "device")
        self.assertEqual(outgoing["nbx_fs_api_key"], "key")
        self.assertEqual(outgoing["Origin"], "https://www.qatarairways.com")
        self.assertEqual(outgoing["Referer"], "https://example.test/referer")
        self.assertEqual(cfg["headers"], original)

        with self.subTest("mutated client headers"):
            session = FakeSession()
            with patch.object(flights.requests, "Session", return_value=session):
                client = flights.HttpClient({"User-Agent": "Firefox"}, {})
                client.headers["Host"] = "evil.example"
                client.headers["Accept-Encoding"] = "br"
                client.post_offers(
                    {"origin": "OSL"}, "https://example.test/referer"
                )
            self.assertNotIn("Host", session.last["headers"])
            self.assertNotIn("Accept-Encoding", session.last["headers"])
            self.assertEqual(session.last["headers"]["User-Agent"], "Firefox")

    def test_only_allowlisted_session_cookies_are_merged(self):
        caller_cookies = {"_abck": "old"}
        session = AnalyticsSession()
        with patch.object(flights.requests, "Session", return_value=session):
            client = flights.HttpClient({"User-Agent": "Firefox"}, caller_cookies)
            client.post_offers({"origin": "OSL"}, "https://example.test/referer")
        self.assertEqual(caller_cookies["BIGipServernew"], "5.6.7.8")
        self.assertEqual(caller_cookies["_abck"], "old")
        self.assertNotIn("_ga", caller_cookies)
        self.assertNotIn("OptanonConsent", caller_cookies)


class PersistCookiesTests(unittest.TestCase):
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

    def _write(self, data, raw=False):
        with open(self.config_path, "w") as f:
            if raw:
                f.write(data)
            else:
                json.dump(data, f)

    def _read(self):
        with open(self.config_path) as f:
            return json.load(f)

    def test_preserves_unknown_keys_and_abck_state_on_cookie_write(self):
        self._write(
            {
                "headers": {"session-id": "s"},
                "cookies": {"old": "1", "_ga": "GA1.1"},
                "saved_at": "2026-10-25T10:00:00",
                "source_endpoint": "flight-offers",
                "last_abck_state": "invalid",
                "custom": {"keep": True},
            }
        )
        flights._persist_cookies({"_abck": "VAL~0~rest", "BIGipServerx": "1"})
        saved = self._read()
        self.assertEqual(saved["headers"], {"session-id": "s"})
        self.assertEqual(
            saved["cookies"], {"_abck": "VAL~0~rest", "BIGipServerx": "1"}
        )
        self.assertEqual(saved["source_endpoint"], "flight-offers")
        self.assertEqual(saved["last_abck_state"], "valid")
        self.assertEqual(saved["custom"], {"keep": True})
        self.assertIn("saved_at", saved)

    def test_corrupt_config_is_quarantined_and_not_replaced(self):
        self._write("{not json", raw=True)
        with patch.object(flights.log, "print") as log_print:
            flights._persist_cookies({"_abck": "VAL~0~rest"})
        corrupt = [n for n in os.listdir(self.tmp.name) if ".corrupt-" in n]
        self.assertEqual(len(corrupt), 1)
        with open(os.path.join(self.tmp.name, corrupt[0])) as f:
            self.assertEqual(f.read(), "{not json")
        with open(self.config_path) as f:
            self.assertEqual(f.read(), "{not json")
        messages = " ".join(str(call) for call in log_print.call_args_list)
        self.assertIn("quarantined", messages)
        self.assertIn("skipped", messages)

    def test_explicit_save_replaces_quarantined_config(self):
        self._write("{not json", raw=True)
        with patch.object(flights.log, "print"):
            flights._persist_cookies({"_abck": "VAL~0~rest"})
        fresh = {
            "headers": {
                "User-Agent": "Firefox",
                "session-id": "s",
                "X-AssignedDeviceID": "d",
                "nbx_fs_api_key": "k",
            },
            "cookies": {"_abck": "VAL~0~rest"},
            "source_endpoint": "flight-offers",
            "last_abck_state": "valid",
        }
        flights.save_config(fresh)
        saved = self._read()
        self.assertEqual(saved["headers"], fresh["headers"])
        self.assertEqual(saved["source_endpoint"], "flight-offers")
        self.assertEqual(saved["last_abck_state"], "valid")
        self.assertIn("saved_at", saved)
        corrupt = [n for n in os.listdir(self.tmp.name) if ".corrupt-" in n]
        self.assertEqual(len(corrupt), 1)

    def test_failed_cookie_write_warns_and_keeps_existing_config(self):
        original = {"headers": {"session-id": "s"}, "cookies": {"old": "1"}}
        self._write(original)
        with patch.object(
            flights, "_atomic_write_json", side_effect=OSError("disk full")
        ):
            with patch.object(flights.log, "print") as log_print:
                flights._persist_cookies({"_abck": "VAL~0~rest"})
        self.assertEqual(self._read(), original)
        messages = " ".join(str(call) for call in log_print.call_args_list)
        self.assertIn("continuing", messages)

    def test_missing_config_creates_cookie_only_config(self):
        flights._persist_cookies({"_abck": "VAL~0~rest"})
        saved = self._read()
        self.assertEqual(saved["cookies"], {"_abck": "VAL~0~rest"})
        self.assertNotIn("headers", saved)
        self.assertIn("saved_at", saved)


class BodySummaryTests(unittest.TestCase):
    def test_body_summary_is_bounded_or_omitted(self):
        json_body = '{"error":"' + ("x" * 100000) + '"}'
        html_body = "<!DOCTYPE html><html>" + ("x" * 100000) + "</html>"
        cases = (
            ("json", json_body, json_body[: flights.BODY_SUMMARY_LIMIT]),
            ("html", html_body, "HTML response body (omitted)"),
        )
        for name, body, expected in cases:
            with self.subTest(name=name):
                summary = flights._safe_body_summary(body)
                self.assertEqual(summary, expected)
                self.assertLessEqual(len(summary), flights.BODY_SUMMARY_LIMIT)

    def test_edge_reference_and_request_failed_are_bounded(self):
        with self.subTest("edge reference near start"):
            start_body = "Reference #18.abc.123 " + ("x" * 100000)
            self.assertEqual(flights._edge_reference(start_body), "18.abc.123")
        with self.subTest("edge reference beyond scan"):
            end_body = ("x" * 100000) + " Reference #18.abc.123"
            self.assertIsNone(flights._edge_reference(end_body))
        with self.subTest("request failed text"):
            body = "x" * 100000
            exc = flights.RequestFailed(500, body)
            self.assertEqual(exc.body, exc.body_summary)
            self.assertLessEqual(len(exc.body), flights.BODY_SUMMARY_LIMIT)
            self.assertNotIn("x" * (flights.BODY_SUMMARY_LIMIT + 1), str(exc))


class NoOpLimiter:
    def wait(self):
        return None

    def record_call(self):
        return None


class FetchFlightsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cache_file = flights.OFFERS_CACHE_FILE
        flights.OFFERS_CACHE_FILE = os.path.join(self.tmp.name, "offers_cache.json")
        self.today = flights.datetime.now().strftime("%Y-%m-%d")
        flights.set_nocache(False)
        self.persist_patcher = patch.object(
            flights, "_persist_cookies", lambda cookies: None
        )
        self.persist_patcher.start()
        self.limiter = NoOpLimiter()
        self.limiter_patcher = patch.object(
            flights, "get_rate_limiter", return_value=self.limiter
        )
        self.limiter_patcher.start()

    def tearDown(self):
        self.limiter_patcher.stop()
        self.persist_patcher.stop()
        flights.OFFERS_CACHE_FILE = self.old_cache_file
        flights.set_nocache(False)
        self.tmp.cleanup()
        flights.reset_rate_limiter()

    def _seed(self, data):
        with open(flights.OFFERS_CACHE_FILE, "w") as f:
            json.dump(data, f)

    def _read(self):
        with open(flights.OFFERS_CACHE_FILE) as f:
            return json.load(f)

    def test_typed_errors_raise(self):
        cases = (
            (
                "edge",
                403,
                "<html>Access Denied errors.edgesuite.net</html>",
                flights.EdgeBlock,
            ),
            ("auth", 401, '{"error": "session"}', flights.AppAuthError),
            ("request", 500, "server error", flights.RequestFailed),
        )
        for name, status, body, expected in cases:
            with self.subTest(name=name):
                response = FakeResponse(status, body)
                with patch.object(flights, "_persist_cookies") as persist:
                    with patch.object(self.limiter, "record_call") as record_call:
                        with patch.object(
                            flights.HttpClient, "post_offers", return_value=response
                        ):
                            with self.assertRaises(expected) as ctx:
                                flights.fetch_flights(
                                    {}, {}, "OSL", "BKK", "2026-12-29"
                                )
                if expected is flights.EdgeBlock:
                    persist.assert_called_once()
                    record_call.assert_called_once()
                if expected is flights.RequestFailed:
                    self.assertEqual(ctx.exception.status, 500)
                    self.assertIn("server error", str(ctx.exception))

    def test_successful_payloads_are_cached(self):
        payloads = (
            {"flightOffers": [{"id": 1}]},
            {},
            {"flightOffers": []},
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                if os.path.exists(flights.OFFERS_CACHE_FILE):
                    os.remove(flights.OFFERS_CACHE_FILE)
                response = FakeResponse(200, json.dumps(payload))
                with patch.object(
                    flights.HttpClient, "post_offers", return_value=response
                ):
                    data, status, err = flights.fetch_flights(
                        {}, {}, "OSL", "BKK", "2026-12-29"
                    )
                self.assertEqual(status, 200)
                self.assertIsNone(err)
                self.assertEqual(data, payload)
                cache = self._read()
                self.assertIn("OSL_BKK_2026-12-29", cache)
                self.assertEqual(cache["OSL_BKK_2026-12-29"]["date"], self.today)
                self.assertEqual(cache["OSL_BKK_2026-12-29"]["data"], payload)

    def test_cache_hit_and_miss_limiter(self):
        with self.subTest("hit"):
            self._seed(
                {
                    "OSL_BKK_2026-12-29": {
                        "date": self.today,
                        "data": {"cached": True},
                    }
                }
            )
            with patch.object(flights.HttpClient, "post_offers") as post:
                with patch.object(self.limiter, "wait") as wait:
                    with patch.object(self.limiter, "record_call") as record_call:
                        with patch.object(flights, "_persist_cookies") as persist:
                            data, status, err = flights.fetch_flights(
                                {}, {}, "OSL", "BKK", "2026-12-29"
                            )
            post.assert_not_called()
            wait.assert_not_called()
            record_call.assert_not_called()
            persist.assert_not_called()
            self.assertEqual(data, {"cached": True})
        with self.subTest("miss"):
            if os.path.exists(flights.OFFERS_CACHE_FILE):
                os.remove(flights.OFFERS_CACHE_FILE)
            response = FakeResponse(200, '{"flightOffers": []}')
            with patch.object(
                flights.HttpClient, "post_offers", return_value=response
            ):
                with patch.object(self.limiter, "wait") as wait:
                    with patch.object(self.limiter, "record_call") as record_call:
                        flights.fetch_flights({}, {}, "OSL", "BKK", "2026-12-29")
            wait.assert_called_once()
            record_call.assert_called_once()

    def test_malformed_json_payload_raises_and_is_not_cached(self):
        payloads = (
            None,
            [],
            "text",
            1,
            {"flightOffers": None},
            {"flightOffers": {}},
            {"flightOffers": "bad"},
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                if os.path.exists(flights.OFFERS_CACHE_FILE):
                    os.remove(flights.OFFERS_CACHE_FILE)
                response = FakeResponse(200, json.dumps(payload))
                with patch.object(
                    flights.HttpClient, "post_offers", return_value=response
                ):
                    with self.assertRaises(flights.RequestFailed) as ctx:
                        flights.fetch_flights(
                            {}, {}, "OSL", "BKK", "2026-12-29"
                        )
                self.assertEqual(ctx.exception.status, 200)
                self.assertIn(
                    "Malformed flight-offers payload",
                    str(ctx.exception),
                )
                self.assertFalse(
                    os.path.exists(flights.OFFERS_CACHE_FILE)
                )

    def test_non_dict_cache_entry_is_tolerated(self):
        response = FakeResponse(200, '{"flightOffers": []}')
        bad_cache = {"OSL_BKK_2026-12-29": ["not-an-entry"]}
        with patch.object(
            flights, "load_offers_cache", return_value=bad_cache
        ):
            with patch.object(flights, "save_offers_cache") as save:
                with patch.object(
                    flights.HttpClient, "post_offers", return_value=response
                ) as post:
                    data, status, err = flights.fetch_flights(
                        {}, {}, "OSL", "BKK", "2026-12-29"
                    )
        self.assertEqual(status, 200)
        self.assertIsNone(err)
        post.assert_called_once()
        saved = save.call_args[0][0]
        self.assertEqual(
            saved["OSL_BKK_2026-12-29"]["data"], {"flightOffers": []}
        )


if __name__ == "__main__":
    unittest.main()
