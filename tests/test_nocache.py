import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import flights


class FakeResponse:
    status_code = 200
    text = ""

    def json(self):
        return {"flightOffers": [{"origin": {"iataCode": "OSL"}}]}


class NoOpLimiter:
    def wait(self):
        return None

    def record_call(self):
        return None


class NoCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache_path = os.path.join(self.tmp.name, "offers_cache.json")
        self.old_cache_file = flights.OFFERS_CACHE_FILE
        flights.OFFERS_CACHE_FILE = self.cache_path
        self.today = flights.datetime.now().strftime("%Y-%m-%d")
        flights.set_nocache(True)
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
        flights.set_nocache(False)
        flights.OFFERS_CACHE_FILE = self.old_cache_file
        self.tmp.cleanup()
        flights.reset_rate_limiter()

    def _seed(self, data):
        with open(self.cache_path, "w") as f:
            json.dump(data, f)

    def _read(self):
        with open(self.cache_path) as f:
            return json.load(f)

    def test_nocache_success_preserves_existing_cache(self):
        self._seed({"OSL_BKK_2026-12-20": {"date": self.today, "data": {"old": True}}})
        with patch.object(flights.HttpClient, "post_offers", return_value=FakeResponse()) as post:
            data, status, err = flights.fetch_flights({}, {}, "OSL", "BKK", "2026-12-29")
        self.assertEqual(status, 200)
        self.assertIsNone(err)
        self.assertEqual(data, {"flightOffers": [{"origin": {"iataCode": "OSL"}}]})
        cache = self._read()
        self.assertIn("OSL_BKK_2026-12-20", cache)
        self.assertIn("OSL_BKK_2026-12-29", cache)
        self.assertEqual(post.call_count, 1)

    def test_nocache_bypasses_hit(self):
        self._seed({"OSL_BKK_2026-12-29": {"date": self.today, "data": {"old": True}}})
        with patch.object(flights.HttpClient, "post_offers", return_value=FakeResponse()) as post:
            data, status, err = flights.fetch_flights({}, {}, "OSL", "BKK", "2026-12-29")
        self.assertEqual(post.call_count, 1)
        self.assertNotEqual(data, {"old": True})

    def test_cached_hit_skips_network(self):
        flights.set_nocache(False)
        self._seed({"OSL_BKK_2026-12-29": {"date": self.today, "data": {"cached": True}}})
        with patch.object(flights.HttpClient, "post_offers") as post:
            with patch.object(self.limiter, "wait") as wait:
                with patch.object(self.limiter, "record_call") as record_call:
                    data, status, err = flights.fetch_flights({}, {}, "OSL", "BKK", "2026-12-29")
        post.assert_not_called()
        wait.assert_not_called()
        record_call.assert_not_called()
        self.assertEqual(data, {"cached": True})

    def test_nocache_miss_calls_limiter(self):
        self._seed({"OSL_BKK_2026-12-20": {"date": self.today, "data": {"old": True}}})
        with patch.object(flights.HttpClient, "post_offers", return_value=FakeResponse()) as post:
            with patch.object(self.limiter, "wait") as wait:
                with patch.object(self.limiter, "record_call") as record_call:
                    flights.fetch_flights({}, {}, "OSL", "BKK", "2026-12-29")
        wait.assert_called_once()
        record_call.assert_called_once()


if __name__ == "__main__":
    unittest.main()
