import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import flights


class FakeResponse:
    def __init__(self, status_code=200, text="", payload=None, json_error=None):
        self.status_code = status_code
        self.text = text
        self.payload = payload
        self.json_error = json_error

    def json(self):
        if self.json_error is not None:
            raise self.json_error
        return self.payload


class NoOpLimiter:
    def wait(self):
        return None

    def record_call(self):
        return None


class ParsingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_cache = flights.OFFERS_CACHE_FILE
        self.old_archive = flights.CACHE_ARCHIVE_FILE
        flights.OFFERS_CACHE_FILE = os.path.join(self.tmp.name, "offers_cache.json")
        flights.CACHE_ARCHIVE_FILE = os.path.join(self.tmp.name, "cache_archive.json")
        self.addCleanup(setattr, flights, "OFFERS_CACHE_FILE", self.old_cache)
        self.addCleanup(setattr, flights, "CACHE_ARCHIVE_FILE", self.old_archive)
        flights.set_nocache(True)
        self.addCleanup(flights.set_nocache, False)
        self.persist_patcher = patch.object(
            flights, "_persist_cookies", lambda cookies: None
        )
        self.persist_patcher.start()
        self.addCleanup(self.persist_patcher.stop)
        self.limiter = NoOpLimiter()
        self.limiter_patcher = patch.object(
            flights, "get_rate_limiter", return_value=self.limiter
        )
        self.limiter_patcher.start()
        self.addCleanup(self.limiter_patcher.stop)

    def _segment(self, **overrides):
        segment = {
            "flightNumber": "QR1",
            "vehicle": {
                "code": "788",
                "shortName": "788",
                "name": "Boeing 787-8",
            },
            "departure": {"dateTime": "2026-12-29T10:00:00"},
            "arrival": {
                "dateTime": "2026-12-29T20:00:00",
                "destination": {"iataCode": "BKK"},
            },
            "duration": 36000,
            "qSuiteEquipped": False,
        }
        segment.update(overrides)
        return segment

    def _fare(self, **overrides):
        fare = {
            "cabinType": "ECONOMY",
            "fareFamilyCode": "ECONOMY_CLASSIC",
            "price": {"total": 5000, "currencyCode": "NOK"},
            "fareFamilyFeatures": [
                {"type": "EARNED_MILES", "parameters": ["1234"]}
            ],
            "availableSeats": 3,
        }
        fare.update(overrides)
        return fare

    def _offer(self, **overrides):
        offer = {
            "duration": 36000,
            "numberOfStops": 0,
            "origin": {"iataCode": "OSL"},
            "destination": {"iataCode": "BKK"},
            "segments": [self._segment()],
            "fareOffers": [self._fare()],
        }
        offer.update(overrides)
        return offer

    def test_fetch_non_json_raises_and_is_not_cached(self):
        response = FakeResponse(
            200,
            "<html>Access Denied errors.edgesuite.net</html>",
            json_error=ValueError("not json"),
        )
        with patch.object(flights.HttpClient, "post_offers", return_value=response):
            with self.assertRaises(flights.RequestFailed) as ctx:
                flights.fetch_flights({}, {}, "OSL", "BKK", "2026-12-29")
        self.assertEqual(ctx.exception.status, 200)
        self.assertIn("Non-JSON response", str(ctx.exception))
        self.assertNotIn("Access Denied", str(ctx.exception))
        self.assertFalse(os.path.exists(flights.OFFERS_CACHE_FILE))

    def test_fetch_and_parse_returns_empty_for_malformed_payload(self):
        with patch.object(
            flights,
            "_fetch_with_retries",
            return_value=({"flightOffers": None}, 200, None),
        ):
            with patch.object(flights, "_log_error") as log_error:
                result = flights._fetch_and_parse(
                    {},
                    {},
                    "OSL",
                    "BKK",
                    "2026-12-29",
                    "2026-12-29",
                    "2027-01-15",
                    "OB",
                    None,
                )
        self.assertEqual(result, [])
        log_error.assert_called_once()

    def test_malformed_top_level_and_offer_list_raise(self):
        for payload in (
            None,
            [],
            "text",
            1,
            {"flightOffers": None},
            {"flightOffers": {}},
            {"flightOffers": "bad"},
        ):
            with self.assertRaises(flights.RequestFailed) as ctx:
                flights.parse_results(payload)
            self.assertEqual(ctx.exception.status, 200)
            self.assertIn("Malformed flight-offers payload", str(ctx.exception))

    def test_missing_or_empty_offers_is_valid_empty_result(self):
        self.assertEqual(flights.parse_results({}), [])
        self.assertEqual(flights.parse_results({"flightOffers": []}), [])

    def test_valid_payload_is_preserved(self):
        output = flights.parse_results({"flightOffers": [self._offer()]})
        self.assertEqual(len(output), 1)
        row = output[0]
        self.assertEqual(row["origin"], "OSL")
        self.assertEqual(row["destination"], "BKK")
        self.assertEqual(row["departure_date"], "2026-12-29")
        self.assertEqual(row["duration"], "10h 0m")
        self.assertEqual(row["duration_seconds"], 36000)
        self.assertEqual(row["stops"], 0)
        self.assertEqual(row["fare"], "ECONOMY_CLASSIC")
        self.assertEqual(row["cabin"], "ECONOMY")
        self.assertEqual(row["price"], 5000)
        self.assertEqual(row["currency"], "NOK")
        self.assertEqual(row["avios"], "1234")
        self.assertEqual(row["available_seats"], 3)
        self.assertEqual(row["segments"][0]["flight"], "QR1")

    def test_malformed_offers_fares_and_segments_are_skipped(self):
        offers = [
            "not-an-offer",
            self._offer(segments="bad"),
            self._offer(segments=[]),
            self._offer(segments=[None]),
            self._offer(segments=[self._segment(), None]),
            self._offer(
                segments=[self._segment(departure={"dateTime": "not-a-date"})]
            ),
            self._offer(fareOffers="bad"),
            self._offer(
                fareOffers=[
                    None,
                    {
                        "cabinType": "ECONOMY",
                        "fareFamilyCode": "ECONOMY_CLASSIC",
                        "price": None,
                    },
                ]
            ),
            self._offer(
                fareOffers=[self._fare(fareFamilyFeatures={"bad": True})]
            ),
            self._offer(
                fareOffers=[self._fare()],
                duration=float("nan"),
                numberOfStops="invalid",
            ),
            self._offer(
                fareOffers=[self._fare()],
                duration=30 * 3600,
                numberOfStops=0,
            ),
            self._offer(
                fareOffers=[self._fare()],
                duration=36000,
                numberOfStops=5,
            ),
        ]
        with patch.object(flights.log, "print") as log_print:
            output = flights.parse_results(
                {"flightOffers": offers}, max_duration_hours=25
            )
        self.assertEqual(len(output), 2)
        self.assertIsNone(output[0]["avios"])
        self.assertIsNone(output[1]["duration"])
        self.assertIsNone(output[1]["duration_seconds"])
        self.assertEqual(output[1]["stops"], 0)
        self.assertEqual(log_print.call_count, 1)
        self.assertIn("Skipped malformed", str(log_print.call_args))

    def test_warn_false_suppresses_skip_warning(self):
        with patch.object(flights.log, "print") as log_print:
            output = flights.parse_results(
                {"flightOffers": ["bad"]}, warn=False
            )
        self.assertEqual(output, [])
        log_print.assert_not_called()

    def test_extract_avios_is_defensive(self):
        for fare in (
            None,
            {},
            {"fareFamilyFeatures": None},
            {"fareFamilyFeatures": {}},
            {"fareFamilyFeatures": [None]},
            {
                "fareFamilyFeatures": [
                    {"type": "EARNED_MILES", "parameters": None},
                    {"type": "EARNED_MILES", "parameters": {}},
                ]
            },
        ):
            self.assertIsNone(flights.extract_avios(fare))
        self.assertEqual(flights.extract_avios(self._fare()), "1234")

    def test_load_archive_prices_skips_malformed_without_warning(self):
        archive = [
            "not-an-entry",
            {"key": 123, "entry": {"data": self._offer()}},
            {
                "key": "OSL_BKK_2026-12-29",
                "entry": {"data": {"flightOffers": None}},
            },
            {
                "key": "OSL_BKK_2026-12-29",
                "entry": {"data": {"flightOffers": ["bad"]}},
            },
            {
                "key": "OSL_BKK_2026-12-29",
                "entry": {"data": {"flightOffers": [self._offer()]}},
            },
        ]
        with open(flights.CACHE_ARCHIVE_FILE, "w") as f:
            json.dump(archive, f)
        with patch.object(flights.log, "print") as log_print:
            result = flights._load_archive_prices()
        self.assertEqual(
            result["2026-12-29"]["OSL_BKK"]["ECONOMY_CLASSIC"], 5000
        )
        log_print.assert_not_called()


if __name__ == "__main__":
    unittest.main()
