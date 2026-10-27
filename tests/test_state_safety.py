import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import flights


class LayoutSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "layouts.json")
        self.old_path = flights.LAYOUTS_FILE
        self.old_layouts = flights.layouts
        flights.LAYOUTS_FILE = self.path
        self.addCleanup(setattr, flights, "LAYOUTS_FILE", self.old_path)
        self.addCleanup(setattr, flights, "layouts", self.old_layouts)

    def _write(self, data, raw=False):
        with open(self.path, "w") as f:
            if raw:
                f.write(data)
            else:
                json.dump(data, f)

    def test_invalid_json_and_wrong_container_shapes_fall_back(self):
        with self.subTest(case="invalid_json"):
            self._write("{not json", raw=True)
            with patch.object(flights.log, "print") as log_print:
                result = flights.load_layouts()
            self.assertEqual(result, {"aircraft": {}, "by_flight": {}})
            log_print.assert_called_once()
        for data in ([], {"aircraft": []}, {"by_flight": "bad"}):
            with self.subTest(data=data):
                self._write(data)
                with patch.object(flights.log, "print") as log_print:
                    result = flights.load_layouts()
                self.assertEqual(result, {"aircraft": {}, "by_flight": {}})
                log_print.assert_called_once()

    def test_valid_container_shapes_preserve_existing_values(self):
        self._write({"aircraft": {"788": 5}, "by_flight": {"QR1": "Pod"}})
        with patch.object(flights.log, "print") as log_print:
            result = flights.load_layouts()
        self.assertEqual(result["aircraft"]["788"], 5)
        self.assertEqual(result["by_flight"]["QR1"], "Pod")
        log_print.assert_not_called()

    def test_classify_layout_tolerates_invalid_global_shapes(self):
        flights.layouts = {"aircraft": "bad", "by_flight": []}
        self.assertEqual(flights.classify_layout("788", "QR1", False), "Unknown")
        flights.layouts = {"aircraft": {"788": 5}, "by_flight": {}}
        self.assertEqual(flights.classify_layout("788", "QR1", False), 5)
        self.assertEqual(flights.classify_layout("788", "QR1", True), "QSuite")


class ConfigSafetyTests(unittest.TestCase):
    def test_validate_config_rejects_non_dict_headers_and_cookies(self):
        cases = (
            {"headers": [], "cookies": {}},
            {"headers": {}, "cookies": []},
            {"headers": None, "cookies": []},
            [],
        )
        for cfg in cases:
            with self.subTest(cfg=cfg):
                with patch.object(flights.log, "print") as log_print:
                    self.assertFalse(flights.validate_config(cfg))
                log_print.assert_called_once()

    def test_abck_state_tolerates_invalid_values(self):
        cfg = {
            "headers": {
                "session-id": "s",
                "X-AssignedDeviceID": "d",
                "nbx_fs_api_key": "k",
                "User-Agent": "Firefox",
            },
            "cookies": {"_abck": 123},
        }
        with patch.object(flights.log, "print") as log_print:
            self.assertTrue(flights.validate_config(cfg))
        self.assertEqual(cfg["last_abck_state"], "unknown")
        log_print.assert_called()
        for cookies in (None, []):
            with self.subTest(cookies=cookies):
                self.assertEqual(flights._abck_state(cookies), "unknown")


if __name__ == "__main__":
    unittest.main()
