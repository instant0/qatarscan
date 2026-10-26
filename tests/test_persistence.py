import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import flights


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = {
            "DATA_LOCK_FILE": flights.DATA_LOCK_FILE,
            "OFFERS_CACHE_FILE": flights.OFFERS_CACHE_FILE,
            "PRICE_MAP_FILE": flights.PRICE_MAP_FILE,
            "CACHE_ARCHIVE_FILE": flights.CACHE_ARCHIVE_FILE,
            "CURRENCY_FILE": flights.CURRENCY_FILE,
        }
        flights.DATA_LOCK_FILE = os.path.join(self.tmp.name, ".data_state.lock")
        flights.OFFERS_CACHE_FILE = os.path.join(self.tmp.name, "offers_cache.json")
        flights.PRICE_MAP_FILE = os.path.join(self.tmp.name, "price_map.json")
        flights.CACHE_ARCHIVE_FILE = os.path.join(self.tmp.name, "cache_archive.json")
        flights.CURRENCY_FILE = os.path.join(self.tmp.name, "currency.json")

    def tearDown(self):
        for name, value in self.old.items():
            setattr(flights, name, value)
        self.tmp.cleanup()

    def _write(self, path, data):
        with open(path, "w") as f:
            json.dump(data, f)

    def _read(self, path):
        with open(path) as f:
            return json.load(f)

    def test_atomic_write_replaces_target_without_temp_file(self):
        path = os.path.join(self.tmp.name, "data.json")
        flights._atomic_write_json(path, {"a": 1})
        self.assertEqual(self._read(path), {"a": 1})
        self.assertEqual(os.listdir(self.tmp.name), ["data.json"])

    def test_atomic_write_failure_leaves_original_intact(self):
        path = os.path.join(self.tmp.name, "data.json")
        flights._atomic_write_json(path, {"a": 1})
        with patch.object(flights.os, "replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                flights._atomic_write_json(path, {"a": 2})
        self.assertEqual(self._read(path), {"a": 1})
        self.assertEqual(os.listdir(self.tmp.name), ["data.json"])

    def test_corrupt_offers_cache_is_quarantined(self):
        with open(flights.OFFERS_CACHE_FILE, "w") as f:
            f.write("{not json")
        with patch.object(flights.log, "print"):
            self.assertEqual(flights.load_offers_cache(), {})
        corrupt = [n for n in os.listdir(self.tmp.name) if ".corrupt-" in n]
        self.assertEqual(len(corrupt), 1)
        with open(os.path.join(self.tmp.name, corrupt[0])) as f:
            self.assertEqual(f.read(), "{not json")

    def test_quarantine_failure_raises_local_state_error(self):
        with open(flights.OFFERS_CACHE_FILE, "w") as f:
            f.write("{not json")
        with patch.object(
            flights, "_quarantine_corrupt", side_effect=flights.LocalStateError("no")
        ):
            with self.assertRaises(flights.LocalStateError):
                flights.load_offers_cache()

    def test_save_offers_cache_merges_concurrent_additions(self):
        self._write(
            flights.OFFERS_CACHE_FILE,
            {"A": {"date": "2026-01-01", "data": {"old": True}}},
        )
        flights.save_offers_cache({"B": {"date": "2026-01-02", "data": {"new": True}}})
        cache = self._read(flights.OFFERS_CACHE_FILE)
        self.assertIn("A", cache)
        self.assertIn("B", cache)

    def test_save_offers_cache_write_failure_does_not_raise(self):
        self._write(flights.OFFERS_CACHE_FILE, {"A": {"date": "2026-01-01"}})
        with patch.object(
            flights, "_atomic_write_json", side_effect=OSError("disk full")
        ):
            with patch.object(flights.log, "print"):
                flights.save_offers_cache({"B": {"date": "2026-01-02"}})
        self.assertEqual(self._read(flights.OFFERS_CACHE_FILE), {"A": {"date": "2026-01-01"}})

    def _result(self, price=5000):
        return {
            "departure_date": "2026-08-17",
            "fare": "ECONOMY_CLASSIC",
            "segments": [
                {
                    "flight": "QR1",
                    "departure": "2026-08-17T10:00:00",
                    "arrival": "2026-08-17T20:00:00",
                }
            ],
            "stops": 0,
            "origin": "OSL",
            "destination": "BKK",
            "price": price,
        }

    def test_update_price_map_preserves_unrelated_entries(self):
        day = datetime.strptime("2026-08-17", "%Y-%m-%d").strftime("%a")
        existing = {
            "OSL_BKK": {
                day: {
                    "OLD|09:00|0|0": {
                        "flight": "OLD",
                        "departure": "09:00",
                        "arrival": "19:00",
                        "stops": 0,
                        "transfer_minutes": 0,
                        "direct": False,
                        "fares": {
                            "ECONOMY_CLASSIC": {
                                "min": 4000,
                                "max": 4000,
                                "total": 4000,
                                "samples": 1,
                            }
                        },
                    }
                }
            },
            "BKK_OSL": {"Fri": {}},
        }
        self._write(flights.PRICE_MAP_FILE, existing)
        flights.update_price_map([self._result()])
        data = self._read(flights.PRICE_MAP_FILE)
        self.assertIn("OLD|09:00|0|0", data["OSL_BKK"][day])
        self.assertIn("BKK_OSL", data)
        new_key = "QR1|10:00|0|0"
        self.assertIn(new_key, data["OSL_BKK"][day])
        self.assertEqual(data["OSL_BKK"][day][new_key]["fares"]["ECONOMY_CLASSIC"]["samples"], 1)

    def test_save_currency_rates_preserves_other_currencies(self):
        self._write(
            flights.CURRENCY_FILE,
            {
                "THB_NOK": {"rate": 0.3, "date": "2026-10-25"},
                "VND_NOK": {"rate": 0.0004, "date": "2026-10-25"},
                "USD_NOK": {"rate": 9.2, "date": "2026-10-25"},
            },
        )
        flights.save_currency_rates(
            {"THB_NOK": {"rate": 0.31, "date": "2026-10-26"}}
        )
        data = self._read(flights.CURRENCY_FILE)
        self.assertEqual(set(data), {"THB_NOK", "VND_NOK", "USD_NOK"})
        self.assertEqual(data["THB_NOK"]["rate"], 0.31)
        self.assertEqual(data["USD_NOK"]["rate"], 9.2)

    def test_archive_moves_stale_entries_and_prunes(self):
        today = datetime.now().strftime("%Y-%m-%d")
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        old = (datetime.now() - timedelta(days=400)).strftime("%Y-%m-%d")
        self._write(
            flights.OFFERS_CACHE_FILE,
            {
                "YDAY": {"date": yesterday, "data": {"new": True}},
                "DUP": {"date": yesterday, "data": {"replacement": True}},
                "OLD": {"date": old, "data": {"old": True}},
                "TODAY": {"date": today, "data": {"fresh": True}},
            },
        )
        self._write(
            flights.CACHE_ARCHIVE_FILE,
            [
                {"archived_at": "ancient", "key": "DUP", "entry": {"date": yesterday, "data": {"old": True}}},
                {"archived_at": "ancient", "key": "KEEP", "entry": {"date": yesterday, "data": {"keep": True}}},
                {"archived_at": "ancient", "key": "TOOOLD", "entry": {"date": old, "data": {"old": True}}},
            ],
        )
        self.assertEqual(flights.archive_stale_cache(), 3)
        cache = self._read(flights.OFFERS_CACHE_FILE)
        archive = self._read(flights.CACHE_ARCHIVE_FILE)
        self.assertEqual(set(cache), {"TODAY"})
        self.assertEqual(set(a["key"] for a in archive), {"YDAY", "DUP", "KEEP"})
        dup = [a for a in archive if a["key"] == "DUP"]
        self.assertEqual(len(dup), 1)
        self.assertEqual(dup[0]["entry"]["data"], {"replacement": True})

    def test_archive_write_failure_keeps_stale_entries(self):
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        cache = {"YDAY": {"date": yesterday, "data": {"new": True}}}
        self._write(flights.OFFERS_CACHE_FILE, cache)
        with patch.object(
            flights, "_atomic_write_json", side_effect=OSError("disk full")
        ):
            with patch.object(flights.log, "print"):
                self.assertEqual(flights.archive_stale_cache(), 0)
        self.assertEqual(self._read(flights.OFFERS_CACHE_FILE), cache)
        self.assertFalse(os.path.exists(flights.CACHE_ARCHIVE_FILE))

    def test_corrupt_price_map_is_quarantined_on_update(self):
        with open(flights.PRICE_MAP_FILE, "w") as f:
            f.write("{not json")
        with patch.object(flights.log, "print"):
            flights.update_price_map([self._result()])
        self.assertTrue(os.path.exists(flights.PRICE_MAP_FILE))
        self.assertIn("OSL_BKK", self._read(flights.PRICE_MAP_FILE))
        corrupt = [n for n in os.listdir(self.tmp.name) if ".corrupt-" in n]
        self.assertEqual(len(corrupt), 1)

    def test_corrupt_currency_cache_is_quarantined(self):
        with open(flights.CURRENCY_FILE, "w") as f:
            f.write("{not json")
        with patch.object(flights.log, "print"):
            self.assertEqual(flights.load_currency_rates(), {})
        corrupt = [n for n in os.listdir(self.tmp.name) if ".corrupt-" in n]
        self.assertEqual(len(corrupt), 1)

    def test_main_exits_on_local_state_error(self):
        with patch.object(flights, "_main", side_effect=flights.LocalStateError("bad")):
            with patch.object(flights.log, "print"):
                with self.assertRaises(SystemExit) as ctx:
                    flights.main()
        self.assertEqual(ctx.exception.code, 1)

    def test_corrupt_price_map_and_archive_views_do_not_raise(self):
        with open(flights.PRICE_MAP_FILE, "w") as f:
            f.write("{not json")
        with open(flights.CACHE_ARCHIVE_FILE, "w") as f:
            f.write("not json")
        with patch.object(flights.log, "print"):
            with patch.object(flights.console, "print"):
                flights.print_price_map()
                flights.print_price_calendar(2026, 8, "OSL", "BKK", archive_prices={})
                flights.print_price_trends("OSL", "BKK", archive_prices={})
                self.assertEqual(flights._load_archive_prices(), {})


if __name__ == "__main__":
    unittest.main()
