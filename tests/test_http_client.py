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
    def test_success(self):
        self.assertIsNone(flights.classify_block(200, "{}"))

    def test_edge(self):
        html = "<html><title>Access Denied</title>errors.edgesuite.net</html>"
        self.assertEqual(flights.classify_block(403, html), "edge")

    def test_auth(self):
        self.assertEqual(
            flights.classify_block(401, '{"error": "session expired"}'), "auth"
        )
        self.assertEqual(flights.classify_block(403, '{"error": "forbidden"}'), "auth")

    def test_other(self):
        self.assertEqual(flights.classify_block(500, "server error"), "other")
        self.assertEqual(flights.classify_block(403, "plain text"), "other")


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


class PersistCookiesTests(unittest.TestCase):
    def test_merges_cookies_into_existing_config(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config_path = os.path.join(tmp.name, "config.json")
        old_config_file = flights.CONFIG_FILE
        flights.CONFIG_FILE = config_path
        self.addCleanup(setattr, flights, "CONFIG_FILE", old_config_file)
        with open(config_path, "w") as f:
            json.dump(
                {"headers": {"session-id": "s"}, "cookies": {"old": "1"}}, f
            )
        with patch.object(flights, "save_config") as save:
            flights._persist_cookies({"new": "2"})
        saved = save.call_args[0][0]
        self.assertEqual(saved["headers"], {"session-id": "s"})
        self.assertEqual(saved["cookies"], {"new": "2"})


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

    def test_edge_block_raises(self):
        response = FakeResponse(403, "<html>Access Denied errors.edgesuite.net</html>")
        with patch.object(flights, "_persist_cookies") as persist:
            with patch.object(self.limiter, "record_call") as record_call:
                with patch.object(
                    flights.HttpClient, "post_offers", return_value=response
                ):
                    with self.assertRaises(flights.EdgeBlock):
                        flights.fetch_flights({}, {}, "OSL", "BKK", "2026-12-29")
        persist.assert_called_once()
        record_call.assert_called_once()

    def test_auth_error_raises(self):
        response = FakeResponse(401, '{"error": "session"}')
        with patch.object(flights.HttpClient, "post_offers", return_value=response):
            with self.assertRaises(flights.AppAuthError):
                flights.fetch_flights({}, {}, "OSL", "BKK", "2026-12-29")

    def test_request_failed_raises(self):
        response = FakeResponse(500, "server error")
        with patch.object(flights.HttpClient, "post_offers", return_value=response):
            with self.assertRaises(flights.RequestFailed) as ctx:
                flights.fetch_flights({}, {}, "OSL", "BKK", "2026-12-29")
        self.assertEqual(ctx.exception.status, 500)
        self.assertIn("server error", str(ctx.exception))

    def test_success_writes_cache(self):
        response = FakeResponse(200, '{"flightOffers": [{"id": 1}]}')
        with patch.object(flights.HttpClient, "post_offers", return_value=response):
            data, status, err = flights.fetch_flights(
                {}, {}, "OSL", "BKK", "2026-12-29"
            )
        self.assertEqual(status, 200)
        self.assertIsNone(err)
        self.assertEqual(data, {"flightOffers": [{"id": 1}]})
        cache = self._read()
        self.assertIn("OSL_BKK_2026-12-29", cache)
        self.assertEqual(cache["OSL_BKK_2026-12-29"]["date"], self.today)

    def test_cache_hit_skips_network(self):
        self._seed(
            {"OSL_BKK_2026-12-29": {"date": self.today, "data": {"cached": True}}}
        )
        with patch.object(flights.HttpClient, "post_offers") as post:
            with patch.object(self.limiter, "wait") as wait:
                with patch.object(self.limiter, "record_call") as record_call:
                    data, status, err = flights.fetch_flights(
                        {}, {}, "OSL", "BKK", "2026-12-29"
                    )
        post.assert_not_called()
        wait.assert_not_called()
        record_call.assert_not_called()
        self.assertEqual(data, {"cached": True})

    def test_cache_miss_calls_limiter(self):
        response = FakeResponse(200, '{"flightOffers": []}')
        with patch.object(flights.HttpClient, "post_offers", return_value=response):
            with patch.object(self.limiter, "wait") as wait:
                with patch.object(self.limiter, "record_call") as record_call:
                    flights.fetch_flights({}, {}, "OSL", "BKK", "2026-12-29")
        wait.assert_called_once()
        record_call.assert_called_once()


if __name__ == "__main__":
    unittest.main()
