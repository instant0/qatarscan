import json
import math
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import flights


class FakeFXResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class CurrencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_currency_file = flights.CURRENCY_FILE
        self.old_lock_file = flights.DATA_LOCK_FILE
        flights.CURRENCY_FILE = os.path.join(self.tmp.name, "currency.json")
        flights.DATA_LOCK_FILE = os.path.join(self.tmp.name, ".data_state.lock")
        self.addCleanup(setattr, flights, "CURRENCY_FILE", self.old_currency_file)
        self.addCleanup(setattr, flights, "DATA_LOCK_FILE", self.old_lock_file)
        flights._currency_rates.clear()
        self.addCleanup(flights._currency_rates.clear)

    def _today(self):
        return datetime.now().strftime("%Y-%m-%d")

    def _write_currency(self, data):
        with open(flights.CURRENCY_FILE, "w") as f:
            json.dump(data, f)

    def _read_currency(self):
        with open(flights.CURRENCY_FILE) as f:
            return json.load(f)

    def test_invalid_prices_and_currencies_are_unchanged_with_one_warning(self):
        with self.subTest(kind="prices"):
            values = [None, "5000", float("nan"), float("inf"), -1, True]
            results = [{"price": value, "currency": "THB"} for value in values]
            with patch.object(flights.log, "print") as log_print:
                returned = flights.convert_prices(results)
            self.assertIs(returned, results)
            for row, value in zip(results, values):
                if isinstance(value, float) and math.isnan(value):
                    self.assertTrue(math.isnan(row["price"]))
                else:
                    self.assertEqual(row["price"], value)
                self.assertEqual(row["currency"], "THB")
                self.assertNotIn("_converted", row)
            self.assertEqual(log_print.call_count, 1)
            self.assertIn("Skipped", str(log_print.call_args))
        with self.subTest(kind="currencies"):
            results = [
                {"price": 1000, "currency": currency}
                for currency in (None, "", "   ", 123)
            ]
            with patch.object(flights.log, "print") as log_print:
                flights.convert_prices(results)
            for row in results:
                self.assertEqual(row["price"], 1000)
                self.assertNotIn("_converted", row)
            self.assertEqual(log_print.call_count, 1)
        with self.subTest(kind="warn_false"):
            results = [{"price": None, "currency": "THB"}]
            with patch.object(flights.log, "print") as log_print:
                flights.convert_prices(results, warn=False)
            self.assertIsNone(results[0]["price"])
            log_print.assert_not_called()

    def test_valid_conversion_and_lowercase_normalization(self):
        flights._currency_rates["THB_NOK"] = {
            "rate": 0.3,
            "date": self._today(),
        }
        with self.subTest(kind="rounding"):
            results = [{"price": 1001, "currency": "THB"}]
            with patch.object(flights.requests, "get") as get:
                flights.convert_prices(results)
            get.assert_not_called()
            self.assertEqual(results[0]["price"], 300)
            self.assertEqual(results[0]["currency"], "NOK")
            self.assertTrue(results[0]["_converted"])
        with self.subTest(kind="lowercase"):
            results = [
                {"price": 1000, "currency": "thb"},
                {"price": 1000, "currency": "nok"},
            ]
            flights.convert_prices(results)
            self.assertEqual(results[0]["price"], 300)
            self.assertEqual(results[0]["currency"], "NOK")
            self.assertTrue(results[0]["_converted"])
            self.assertEqual(results[1]["price"], 1000)
            self.assertEqual(results[1]["currency"], "NOK")
            self.assertNotIn("_converted", results[1])

    def test_invalid_codes_and_endpoint_rates_are_rejected(self):
        for code in (None, "US", "US1", "ABCD", "ÅÅÅ"):
            with self.subTest(code=code):
                with patch.object(flights.requests, "get") as get:
                    with patch.object(flights.log, "print") as log_print:
                        self.assertIsNone(
                            flights.get_conversion_rate(code)
                        )
                get.assert_not_called()
                self.assertEqual(log_print.call_count, 1)
        payloads = [
            "not-a-dict",
            {},
            {"thb": None},
            {"thb": {}},
            {"thb": {"nok": -1}},
            {"thb": {"nok": float("nan")}},
            {"thb": {"nok": float("inf")}},
            {"thb": {"nok": "0.3"}},
            {"thb": {"nok": True}},
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                if os.path.exists(flights.CURRENCY_FILE):
                    os.remove(flights.CURRENCY_FILE)
                flights._currency_rates.clear()
                with patch.object(
                    flights.requests,
                    "get",
                    return_value=FakeFXResponse(payload),
                ):
                    with patch.object(flights.log, "print"):
                        self.assertIsNone(
                            flights.get_conversion_rate("THB")
                        )
                self.assertNotIn("THB_NOK", flights._currency_rates)
                self.assertFalse(os.path.exists(flights.CURRENCY_FILE))

    def test_invalid_cached_rates_are_not_used(self):
        today = self._today()
        for entry in (
            {"rate": -1, "date": today},
            {"rate": float("nan"), "date": today},
            {"rate": "0.3", "date": today},
            {"rate": 0.3, "date": None},
        ):
            with self.subTest(entry=entry):
                self._write_currency({"THB_NOK": entry})
                flights._currency_rates.clear()
                with patch.object(
                    flights.requests,
                    "get",
                    side_effect=Exception("network down"),
                ):
                    with patch.object(flights.log, "print"):
                        self.assertIsNone(
                            flights.get_conversion_rate("THB")
                        )
                stored = self._read_currency()["THB_NOK"]
                self.assertEqual(stored["date"], entry["date"])
                if isinstance(entry["rate"], float) and math.isnan(entry["rate"]):
                    self.assertTrue(math.isnan(stored["rate"]))
                else:
                    self.assertEqual(stored["rate"], entry["rate"])

    def test_same_day_disk_cache_avoids_requests_get(self):
        self._write_currency(
            {"THB_NOK": {"rate": 0.3, "date": self._today()}}
        )
        with patch.object(flights.requests, "get") as get:
            self.assertEqual(flights.get_conversion_rate("THB"), 0.3)
        get.assert_not_called()
        self.assertEqual(
            flights._currency_rates["THB_NOK"]["rate"], 0.3
        )

    def test_saving_rate_preserves_unrelated_currency_keys(self):
        yesterday = (
            datetime.now() - timedelta(days=1)
        ).strftime("%Y-%m-%d")
        original = {
            "USD_NOK": {"rate": 9.2, "date": yesterday},
            "THB_NOK": {"rate": 0.3, "date": yesterday},
            "VND_NOK": {"rate": 0.0004, "date": yesterday},
        }
        self._write_currency(original)
        payload = {"thb": {"nok": 0.31}}
        with patch.object(
            flights.requests,
            "get",
            return_value=FakeFXResponse(payload),
        ):
            self.assertEqual(flights.get_conversion_rate("THB"), 0.31)
        data = self._read_currency()
        self.assertEqual(set(data), {"USD_NOK", "THB_NOK", "VND_NOK"})
        self.assertEqual(data["USD_NOK"], original["USD_NOK"])
        self.assertEqual(data["VND_NOK"], original["VND_NOK"])
        self.assertEqual(data["THB_NOK"]["rate"], 0.31)
        self.assertEqual(data["THB_NOK"]["date"], self._today())

    def test_stale_fallback_is_memoized_and_reused(self):
        stale_date = "2020-01-01"
        self._write_currency(
            {
                "THB_NOK": {"rate": 0.3, "date": stale_date},
                "USD_NOK": {"rate": 9.2, "date": stale_date},
            }
        )
        with self.subTest(kind="memoized"):
            results = [{"price": 100, "currency": "THB"} for _ in range(5)]
            results += [{"price": 10, "currency": "USD"} for _ in range(5)]
            with patch.object(
                flights.requests, "get", side_effect=Exception("network down")
            ) as get:
                with patch.object(flights.log, "print") as log_print:
                    flights.convert_prices(results)
            self.assertEqual(get.call_count, 2)
            self.assertTrue(all(row["currency"] == "NOK" for row in results))
            messages = [str(call) for call in log_print.call_args_list]
            thb_warnings = [
                message
                for message in messages
                if "FX rate fetch failed" in message and "THB" in message
            ]
            usd_warnings = [
                message
                for message in messages
                if "FX rate fetch failed" in message and "USD" in message
            ]
            self.assertEqual(len(thb_warnings), 1)
            self.assertEqual(len(usd_warnings), 1)
            data = self._read_currency()
            self.assertEqual(data["THB_NOK"]["date"], stale_date)
            self.assertEqual(data["USD_NOK"]["date"], stale_date)
            self.assertTrue(
                flights._currency_rates["THB_NOK"][flights._FX_FALLBACK_FAILED]
            )
        flights._currency_rates.clear()
        with self.subTest(kind="warning_includes_date"):
            with patch.object(
                flights.requests, "get", side_effect=Exception("network down")
            ):
                with patch.object(flights.log, "print") as log_print:
                    rate = flights.get_conversion_rate("THB")
            self.assertEqual(rate, 0.3)
            messages = " ".join(str(c) for c in log_print.call_args_list)
            self.assertIn(stale_date, messages)
            self.assertEqual(
                self._read_currency()["THB_NOK"]["date"], stale_date
            )

    def test_archive_prices_reuse_stale_fx_without_network_storm(self):
        stale_date = "2020-01-01"
        self._write_currency(
            {"THB_NOK": {"rate": 0.3, "date": stale_date}}
        )
        archive_path = os.path.join(self.tmp.name, "cache_archive.json")
        old_archive = flights.CACHE_ARCHIVE_FILE
        flights.CACHE_ARCHIVE_FILE = archive_path
        self.addCleanup(setattr, flights, "CACHE_ARCHIVE_FILE", old_archive)

        def offer(date_str):
            return {
                "duration": 36000,
                "numberOfStops": 0,
                "origin": {"iataCode": "OSL"},
                "destination": {"iataCode": "BKK"},
                "segments": [
                    {
                        "flightNumber": "QR1",
                        "vehicle": {"code": "788", "name": "Boeing 787-8"},
                        "departure": {"dateTime": f"{date_str}T10:00:00"},
                        "arrival": {
                            "dateTime": f"{date_str}T20:00:00",
                            "destination": {"iataCode": "BKK"},
                        },
                        "duration": 36000,
                        "qSuiteEquipped": False,
                    }
                ],
                "fareOffers": [
                    {
                        "cabinType": "ECONOMY",
                        "fareFamilyCode": "ECONOMY_CLASSIC",
                        "price": {"total": 1000, "currencyCode": "THB"},
                    }
                ],
            }

        archive = [
            {
                "key": f"OSL_BKK_2026-12-{day}",
                "entry": {
                    "date": f"2026-12-{day}",
                    "data": {
                        "flightOffers": [offer(f"2026-12-{day}")]
                    },
                },
            }
            for day in (29, 30, 31)
        ]
        with open(archive_path, "w") as f:
            json.dump(archive, f)
        with patch.object(
            flights.requests, "get", side_effect=Exception("network down")
        ) as get:
            with patch.object(flights.log, "print") as log_print:
                prices = flights._load_archive_prices()
        self.assertEqual(get.call_count, 1)
        self.assertEqual(
            set(prices),
            {"2026-12-29", "2026-12-30", "2026-12-31"},
        )
        messages = [str(call) for call in log_print.call_args_list]
        self.assertEqual(
            sum("FX rate fetch failed" in message for message in messages), 1
        )
        self.assertEqual(
            self._read_currency()["THB_NOK"]["date"], stale_date
        )


if __name__ == "__main__":
    unittest.main()
