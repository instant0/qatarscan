import contextlib
import io
import json
import os
import re
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rich.console import Console

import flights


class FakeLimiter:
    def __init__(self):
        self.wait = MagicMock()
        self.record_call = MagicMock()
        self.record_block = MagicMock()
        self.is_blocked = MagicMock(return_value=False)
        self.clear_block = MagicMock()


def make_args(**overrides):
    args = flights.parse_args([])
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def make_result(
    origin="OSL",
    destination="BKK",
    cabin="ECONOMY",
    fare="ECONOMY_CLASSIC",
    price=5000,
    flight="QR1",
    dep="2026-08-17T10:00:00",
    arr="2026-08-17T20:00:00",
    direction="OB",
    dep_date="2026-08-17",
    ret_date="2026-09-04",
):
    return {
        "origin": origin,
        "destination": destination,
        "departure_date": dep_date[:10],
        "duration": "10h 0m",
        "duration_seconds": 36000,
        "stops": 0,
        "segments": [
            {
                "flight": flight,
                "aircraft": "A350",
                "aircraft_code": "359",
                "layout": "QSuite",
                "departure": dep,
                "arrival": arr,
                "qSuite": True,
                "duration": "10h 0m",
            }
        ],
        "fare": fare,
        "cabin": cabin,
        "price": price,
        "currency": "NOK",
        "avios": None,
        "available_seats": 5,
        "_dep_date": dep_date,
        "_ret_date": ret_date,
        "_direction": direction,
    }


def capture_console(width=2000):
    stream = io.StringIO()
    return stream, Console(
        file=stream, width=width, color_system=None, force_terminal=False
    )


def run_main(args, config=None, patches=None, width=2000):
    stdout, out_console = capture_console(width)
    stderr, err_console = capture_console(width)
    limiter = FakeLimiter()
    code = None
    patch_map = patches or {}
    with patch.object(flights, "parse_args", return_value=args):
        with patch.object(
            flights, "configure_rate_limiter", return_value=limiter
        ):
            with patch.object(flights, "get_rate_limiter", return_value=limiter):
                with patch.object(flights, "load_config", return_value=config):
                    with patch.object(flights, "validate_config", return_value=True):
                        with patch.object(
                            flights, "archive_stale_cache", return_value=0
                        ):
                            with patch.object(
                                flights, "console", out_console
                            ):
                                with patch.object(flights, "log", err_console):
                                    with contextlib.ExitStack() as stack:
                                        for name, value in patch_map.items():
                                            stack.enter_context(
                                                patch.object(flights, name, value)
                                            )
                                        try:
                                            flights.main()
                                        except SystemExit as exc:
                                            code = exc.code
    return code, stdout.getvalue(), stderr.getvalue()


class RichMarkupSafetyTests(unittest.TestCase):
    def setUp(self):
        self.bad = "OSL[bold]X[/][red]Y[/][unknown]Z["

    def test_json_renderers_keep_markup_literal_and_valid(self):
        result = make_result()
        result["origin"] = self.bad
        result["destination"] = "BKK[red]"
        stream, renderer = capture_console()
        with patch.object(flights, "console", renderer):
            flights.print_json([result])
        text = stream.getvalue()
        self.assertIn("[bold]", text)
        self.assertIn("[unknown]", text)
        self.assertEqual(json.loads(text)[0]["origin"], self.bad)

        stream, renderer = capture_console()
        with patch.object(flights, "console", renderer):
            flights.print_json_combined({"outbound": [result]})
        text = stream.getvalue()
        self.assertIn("[red]", text)
        self.assertEqual(
            json.loads(text)["outbound"][0]["destination"], "BKK[red]"
        )

        long_result = make_result()
        long_result["origin"] = "x " * 80
        stream, renderer = capture_console(40)
        with patch.object(flights, "console", renderer):
            flights.print_json([long_result])
        self.assertEqual(
            json.loads(stream.getvalue())[0]["origin"], "x " * 80
        )

    def test_tables_and_summary_render_dynamic_text_literally(self):
        with self.subTest(renderer="compare"):
            result = make_result(
                origin=self.bad,
                destination="BKK[red]",
                fare="ECONOMY_CLASSIC[bold]",
                flight="QR[unknown]1[/]",
                dep="2026-08-17T10:00:00[",
                arr="2026-08-17T20:00:00[/]",
            )
            merged = flights.merge_cabins([result])
            stream, renderer = capture_console(400)
            with patch.object(flights, "console", renderer):
                flights.print_compare_table(
                    merged, title=self.bad, filter_note=self.bad
                )
            text = stream.getvalue()
            for token in ("[bold]", "[/]", "[red]", "[unknown]", "["):
                self.assertIn(token, text)

        with self.subTest(renderer="compare_fares"):
            result = make_result(
                fare="ECONOMY_CLASSIC[bold]",
                price=5000,
            )
            result["currency"] = "NOK[red]"
            merged = flights.merge_cabins([result])
            stream, renderer = capture_console(400)
            with patch.object(flights, "console", renderer):
                flights.print_compare_table(merged)
            text = stream.getvalue()
            self.assertIn("ECONOMY_CLASSIC[bold]", text)
            self.assertIn("NOK[red]", text)
            self.assertIn("5000", text)
            self.assertNotIn("\\[", text)
            self.assertNotIn("\\\\", text)

        with self.subTest(renderer="drift_pair"):
            result = make_result(
                origin=self.bad,
                destination="BKK[red]",
                fare="ECONOMY_CLASSIC[bold]",
                dep_date="2026-08-17[unknown]",
                ret_date="2026-09-04[/]",
            )
            stream, renderer = capture_console(400)
            with patch.object(flights, "console", renderer):
                flights.print_drift_pair_table(
                    [result], title=self.bad, filter_note=self.bad
                )
            text = stream.getvalue()
            for token in ("[bold]", "[/]", "[red]", "[unknown]", "["):
                self.assertIn(token, text)

        with self.subTest(renderer="mixed"):
            ob = make_result(direction="OB", fare="ECONOMY_CLASSIC[bold]")
            rb = make_result(direction="RB", fare="BUSINESS_COMFORT[red]")
            combos = [
                {
                    "label": self.bad,
                    "pair": "2026-08-17[unknown] \u2192 2026-09-04[/]",
                    "leg1": ob,
                    "leg2": rb,
                    "total": 9000,
                    "biz_layout": "QSuite[red]",
                }
            ]
            stream, renderer = capture_console(400)
            with patch.object(flights, "console", renderer):
                flights.print_mixed_table(combos)
            text = stream.getvalue()
            for token in ("[bold]", "[/]", "[red]", "[unknown]", "["):
                self.assertIn(token, text)

        with self.subTest(renderer="drift_summary"):
            result = make_result(
                origin=self.bad,
                destination="BKK[red]",
                fare="ECONOMY_CLASSIC[bold]",
                dep_date="2026-08-17[unknown]",
                ret_date="2026-09-04[/]",
            )
            merged = flights.merge_cabins([result])
            stream, renderer = capture_console(400)
            with patch.object(flights, "console", renderer):
                flights.print_drift_summary(merged)
            text = stream.getvalue()
            for token in ("[bold]", "[/]", "[red]", "[unknown]", "["):
                self.assertIn(token, text)

    def test_booking_links_render_dynamic_text_literally(self):
        with self.subTest(output="stdout"):
            result = make_result(
                origin=self.bad,
                destination="BKK[red]",
                dep_date="[bold]2026-08-17[unknown]",
                ret_date="2026-09-04[/][red]",
            )
            stream, renderer = capture_console()
            with patch.object(flights, "console", renderer):
                flights.print_booking_links([result])
            text = stream.getvalue()
            for token in ("[bold]", "[/]", "[red]", "[unknown]", "["):
                self.assertIn(token, text)

        with self.subTest(output="selector"):
            result = make_result(
                origin="[bold]OSL[/]",
                destination="BKK[red]",
                dep_date="[bold]2026-08-17[/]",
            )
            out_stream, out_console = capture_console()
            err_stream, err_console = capture_console()
            with patch.object(flights, "console", out_console):
                with patch.object(flights, "log", err_console):
                    flights.print_booking_links([result])
                    flights.print_booking_links([result], to_stderr=True)
            self.assertIn("Booking links", out_stream.getvalue())
            self.assertIn("[bold]", out_stream.getvalue())
            self.assertIn("Booking links", err_stream.getvalue())
            self.assertIn(
                "https://www.qatarairways.com", err_stream.getvalue()
            )

    def test_price_views_render_stored_text_literally(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        old = {
            "PRICE_MAP_FILE": flights.PRICE_MAP_FILE,
            "CACHE_ARCHIVE_FILE": flights.CACHE_ARCHIVE_FILE,
            "DATA_LOCK_FILE": flights.DATA_LOCK_FILE,
        }
        flights.PRICE_MAP_FILE = os.path.join(tmp.name, "price_map.json")
        flights.CACHE_ARCHIVE_FILE = os.path.join(tmp.name, "cache_archive.json")
        flights.DATA_LOCK_FILE = os.path.join(tmp.name, ".data_state.lock")
        self.addCleanup(
            lambda: [
                setattr(flights, key, value) for key, value in old.items()
            ]
        )
        origin = "[bold]OSL[/]"
        destination = "BKK[red]"
        direction = f"{origin}_{destination}"
        data = {
            direction: {
                "Mon": {
                    "QR[unknown]1[/]|10:00|0|0": {
                        "flight": "QR[unknown]1[/]",
                        "departure": "10:[red]00[/]",
                        "arrival": "20:00[",
                        "transfer_minutes": 0,
                        "stops": 0,
                        "fares": {
                            "ECONOMY_CLASSIC[bold][unknown][": {
                                "min": 5000,
                                "max": 5000,
                                "total": 5000,
                                "samples": 1,
                            }
                        },
                    }
                }
            }
        }
        with open(flights.PRICE_MAP_FILE, "w") as f:
            json.dump(data, f)

        stream, renderer = capture_console(400)
        with patch.object(flights, "console", renderer):
            flights.print_price_map()
        text = stream.getvalue()
        for token in ("[bold]", "[/]", "[red]", "[unknown]", "["):
            self.assertIn(token, text)

        stream, renderer = capture_console(400)
        with patch.object(flights, "console", renderer):
            flights.print_price_calendar(
                2026, 8, origin, destination, archive_prices={}
            )
        text = stream.getvalue()
        for token in ("[bold]", "[/]", "[red]"):
            self.assertIn(token, text)

        stream, renderer = capture_console(400)
        with patch.object(flights, "console", renderer):
            flights.print_price_trends(origin, destination, archive_prices={})
        text = stream.getvalue()
        for token in ("[bold]", "[/]", "[red]", "[unknown]", "["):
            self.assertIn(token, text)


class JsonUrlSeparationTests(unittest.TestCase):
    def test_json_urls_success_and_partial_keep_stdout_parseable(self):
        with self.subTest(case="success"):
            ob = make_result(direction="OB")
            rb = make_result(
                direction="RB",
                origin="BKK",
                destination="OSL",
                dep="2026-09-04T10:00:00",
                arr="2026-09-04T20:00:00",
                dep_date="2026-09-04",
            )
            args = make_args(json=True, urls=True, nofilter=True)
            patches = {
                "_fetch_with_retries": MagicMock(
                    side_effect=[("ob", 200, None), ("rb", 200, None)]
                ),
                "parse_results": MagicMock(side_effect=[[ob], [rb]]),
                "update_price_map": MagicMock(),
            }
            code, out, err = run_main(
                args, {"headers": {}, "cookies": {}}, patches
            )
            self.assertIsNone(code)
            parsed = json.loads(out)
            self.assertIn("outbound", parsed)
            self.assertIn("return", parsed)
            self.assertNotIn("Booking links", out)
            self.assertIn("Booking links", err)
            self.assertIn("https://www.qatarairways.com", err)

        with self.subTest(case="partial"):
            ob = make_result(direction="OB")
            args = make_args(json=True, urls=True, nofilter=True)
            patches = {
                "_fetch_with_retries": MagicMock(
                    side_effect=[
                        ("ob", 200, None),
                        flights.RequestFailed(0, "timed out"),
                    ]
                ),
                "parse_results": MagicMock(return_value=[ob]),
                "update_price_map": MagicMock(),
                "merge_cabins": lambda r: r,
                "print_compare_table": MagicMock(),
            }
            code, out, err = run_main(
                args, {"headers": {}, "cookies": {}}, patches
            )
            self.assertEqual(code, 1)
            parsed = json.loads(out)
            self.assertEqual(parsed["return"], [])
            self.assertNotIn("Booking links", out)
            self.assertIn("Booking links", err)

    def test_scan_json_urls_success_and_partial_keep_stdout_parseable(self):
        result = make_result()

        with self.subTest(case="success"):
            args = make_args(scan=True, json=True, urls=True, nofilter=True)
            patches = {"scan_outbound": MagicMock(return_value=[result])}
            code, out, err = run_main(
                args, {"headers": {}, "cookies": {}}, patches
            )
            self.assertIsNone(code)
            self.assertIn("cabins", json.loads(out)[0])
            self.assertIn("Booking links", err)
            self.assertNotIn("Booking links", out)

        with self.subTest(case="partial"):
            args = make_args(scan=True, json=True, urls=True, nofilter=True)
            patches = {
                "scan_outbound": MagicMock(
                    side_effect=flights.ScanAborted(
                        [result], "auth", flights.AppAuthError("bad")
                    )
                )
            }
            code, out, err = run_main(
                args, {"headers": {}, "cookies": {}}, patches
            )
            self.assertEqual(code, 1)
            self.assertIn("cabins", json.loads(out)[0])
            self.assertIn("Booking links", err)
            self.assertNotIn("Booking links", out)


class BookingLinkDirectionTests(unittest.TestCase):
    def setUp(self):
        self.old_search = dict(flights.SEARCH)
        flights.SEARCH.update(
            {
                "origin": "OSL",
                "destination": "BKK",
                "departure_date": "2026-08-17",
                "return_date": "2026-09-04",
            }
        )
        self.addCleanup(lambda: flights.SEARCH.update(self.old_search))

    def _urls(self, callback):
        stream, renderer = capture_console()
        with patch.object(flights, "console", renderer):
            callback()
        return re.findall(r"https://\S+", stream.getvalue())

    def _assert_link(self, url, dep, ret):
        query = parse_qs(urlparse(url).query)
        self.assertEqual(query["fromStation"], ["OSL"])
        self.assertEqual(query["toStation"], ["BKK"])
        self.assertEqual(query["departing"], [dep])
        self.assertEqual(query["returning"], [ret])

    def test_links_use_expected_direction_and_dates(self):
        with self.subTest(case="ob_and_rb"):
            ob = make_result(direction="OB")
            rb = make_result(
                direction="RB",
                origin="BKK",
                destination="OSL",
                dep="2026-09-04T10:00:00",
                arr="2026-09-04T20:00:00",
                dep_date="2026-08-17",
                ret_date="2026-09-04",
            )
            urls = self._urls(
                lambda: flights.print_booking_links([ob, rb])
            )
            self.assertEqual(len(urls), 1)
            self._assert_link(urls[0], "2026-08-17", "2026-09-04")

            rb_only = self._urls(lambda: flights.print_booking_links([rb]))
            self.assertEqual(len(rb_only), 1)
            self._assert_link(rb_only[0], "2026-08-17", "2026-09-04")
            query = parse_qs(urlparse(rb_only[0]).query)
            self.assertNotEqual(query["fromStation"], ["BKK"])
            self.assertNotEqual(query["toStation"], ["OSL"])

        with self.subTest(case="scan"):
            first = make_result(dep_date="2026-08-17", ret_date="2026-09-04")
            second = make_result(
                dep_date="2026-08-18", ret_date="2026-09-05"
            )
            urls = self._urls(
                lambda: flights.print_booking_links([first, second])
            )
            self.assertEqual(len(urls), 2)
            self._assert_link(urls[0], "2026-08-17", "2026-09-04")
            self._assert_link(urls[1], "2026-08-18", "2026-09-05")

        with self.subTest(case="drift_summary"):
            ob = make_result(
                direction="OB", flight="QR1", price=5000
            )
            rb = make_result(
                direction="RB",
                origin="BKK",
                destination="OSL",
                flight="QR2",
                price=3000,
                dep="2026-09-04T10:00:00",
                arr="2026-09-04T20:00:00",
                dep_date="2026-08-17",
                ret_date="2026-09-04",
            )
            merged = flights.merge_cabins([ob, rb])
            urls = self._urls(
                lambda: flights.print_drift_summary(merged)
            )
            self.assertEqual(len(urls), 1)
            self._assert_link(urls[0], "2026-08-17", "2026-09-04")

        with self.subTest(case="non_scan_main"):
            ob = make_result(direction="OB")
            rb = make_result(
                direction="RB",
                origin="BKK",
                destination="OSL",
                dep="2026-09-04T10:00:00",
                arr="2026-09-04T20:00:00",
            )
            args = make_args(
                urls=True,
                nofilter=True,
                depart="2026-08-17",
                ret="2026-09-04",
            )
            patches = {
                "_fetch_with_retries": MagicMock(
                    side_effect=[("ob", 200, None), ("rb", 200, None)]
                ),
                "parse_results": MagicMock(side_effect=[[ob], [rb]]),
                "update_price_map": MagicMock(),
            }
            code, out, err = run_main(
                args, {"headers": {}, "cookies": {}}, patches
            )
            self.assertIsNone(code)
            urls = re.findall(r"https://\S+", out + err)
            self.assertEqual(len(urls), 1)
            self._assert_link(urls[0], "2026-08-17", "2026-09-04")


class MalformedPayloadMainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old = {
            "OFFERS_CACHE_FILE": flights.OFFERS_CACHE_FILE,
            "PRICE_MAP_FILE": flights.PRICE_MAP_FILE,
            "DATA_LOCK_FILE": flights.DATA_LOCK_FILE,
        }
        flights.OFFERS_CACHE_FILE = os.path.join(
            self.tmp.name, "offers_cache.json"
        )
        flights.PRICE_MAP_FILE = os.path.join(
            self.tmp.name, "price_map.json"
        )
        flights.DATA_LOCK_FILE = os.path.join(
            self.tmp.name, ".data_state.lock"
        )
        self.addCleanup(
            lambda: [
                setattr(flights, key, value)
                for key, value in self.old.items()
            ]
        )
        flights.set_nocache(False)
        self.addCleanup(flights.set_nocache, False)

    def test_malformed_and_nonjson_200_exit_without_state_writes(self):
        with self.subTest(case="malformed_reused"):
            response = MagicMock()
            response.status_code = 200
            response.text = '{"flightOffers": null}'
            response.json.return_value = {"flightOffers": None}
            args = make_args(
                nofilter=True,
                depart="2026-08-17",
                ret="2026-09-04",
            )
            update_price_map = MagicMock()
            record_block = MagicMock()
            patches = {
                "update_price_map": update_price_map,
                "_record_block_for_abort": record_block,
            }
            post = MagicMock(return_value=response)
            with patch.object(flights.HttpClient, "post_offers", post):
                with patch.object(flights, "_persist_cookies", MagicMock()):
                    code, out, err = run_main(
                        args, {"headers": {}, "cookies": {}}, patches
                    )
            self.assertEqual(code, 1)
            self.assertIn("Malformed flight-offers payload", err)
            self.assertNotIn("Traceback", err)
            self.assertNotIn("Traceback", out)
            self.assertFalse(os.path.exists(flights.OFFERS_CACHE_FILE))
            self.assertFalse(os.path.exists(flights.PRICE_MAP_FILE))
            update_price_map.assert_not_called()
            record_block.assert_not_called()
            self.assertEqual(post.call_count, 1)

            with patch.object(flights.HttpClient, "post_offers", post):
                with patch.object(flights, "_persist_cookies", MagicMock()):
                    code, out, err = run_main(
                        args, {"headers": {}, "cookies": {}}, patches
                    )
            self.assertEqual(code, 1)
            self.assertEqual(post.call_count, 2)
            self.assertFalse(os.path.exists(flights.OFFERS_CACHE_FILE))
            self.assertFalse(os.path.exists(flights.PRICE_MAP_FILE))

        with self.subTest(case="nonjson"):
            response = MagicMock()
            response.status_code = 200
            response.text = "<html>not json</html>"
            response.json.side_effect = ValueError("not json")
            args = make_args(
                nofilter=True,
                depart="2026-08-17",
                ret="2026-09-04",
            )
            update_price_map = MagicMock()
            record_block = MagicMock()
            patches = {
                "update_price_map": update_price_map,
                "_record_block_for_abort": record_block,
            }
            post = MagicMock(return_value=response)
            with patch.object(flights.HttpClient, "post_offers", post):
                with patch.object(flights, "_persist_cookies", MagicMock()):
                    code, out, err = run_main(
                        args, {"headers": {}, "cookies": {}}, patches
                    )
            self.assertEqual(code, 1)
            self.assertIn("Non-JSON response", err)
            self.assertNotIn("Traceback", err)
            self.assertNotIn("Traceback", out)
            self.assertFalse(os.path.exists(flights.OFFERS_CACHE_FILE))
            self.assertFalse(os.path.exists(flights.PRICE_MAP_FILE))
            update_price_map.assert_not_called()
            record_block.assert_not_called()
            self.assertEqual(post.call_count, 1)


class CliValidationTests(unittest.TestCase):
    def parse_error(self, argv):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                flights.parse_args(argv)
        self.assertEqual(ctx.exception.code, 2)

    def test_max_price_validates_and_last_duplicate_wins(self):
        args = flights.parse_args(
            ["--max-price", "economy=8000,BUSINESS=14000,ECONOMY=9000"]
        )
        self.assertEqual(args.max_price, {"ECONOMY": 9000, "BUSINESS": 14000})
        for value in (
            "",
            "ECONOMY",
            "=5",
            "ECONOMY=",
            "ECONOMY=abc",
            "ECONOMY=0",
            "ECONOMY=-1",
            "FIRST=100",
        ):
            with self.subTest(value=value):
                self.parse_error(["--max-price", value])

    def test_budget_delay_and_json_drift_validation(self):
        with self.subTest(case="budget_delay"):
            self.assertEqual(flights.parse_args(["--budget", "0"]).budget, 0)
            self.assertEqual(flights.parse_args(["--budget", "3"]).budget, 3)
            self.assertEqual(flights.parse_args(["--delay", "1"]).delay, 1)
            for argv in (
                ["--budget", "-1"],
                ["--budget", "abc"],
                ["--delay", "0"],
                ["--delay", "-1"],
                ["--delay", "abc"],
            ):
                with self.subTest(argv=argv):
                    self.parse_error(argv)

        with self.subTest(case="json_drift"):
            self.parse_error(["--json", "--drift", "1"])
            self.parse_error(["--json", "--drift-ret", "1"])

    def test_numeric_flags_validate_ranges_and_preserve_zero(self):
        for flag, attr in (
            ("--ob", "ob"),
            ("--rb", "rb"),
            ("--drift", "drift"),
            ("--drift-ret", "drift_ret"),
            ("--min-trip", "min_trip"),
        ):
            with self.subTest(flag=flag):
                self.assertEqual(
                    getattr(flights.parse_args([flag, "0"]), attr), 0
                )
                self.parse_error([flag, "-1"])
                self.parse_error([flag, "abc"])

        for flag, attr in (
            ("--top", "top"),
            ("--max-duration", "max_duration"),
            ("--recommend", "recommend"),
        ):
            with self.subTest(flag=flag):
                self.assertEqual(
                    getattr(flights.parse_args([flag, "1"]), attr), 1
                )
                self.parse_error([flag, "0"])
                self.parse_error([flag, "-1"])
                self.parse_error([flag, "abc"])

        self.assertEqual(
            flights.parse_args(["--recommend"]).recommend, 10
        )

    def test_rolling_defaults_explicit_overrides_and_help(self):
        class FixedDateTime(flights.datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2030, 5, 1, 12, 0, 0, tzinfo=tz)

        old_search = dict(flights.SEARCH)
        self.addCleanup(lambda: flights.SEARCH.update(old_search))

        with self.subTest(case="rolling_defaults"):
            args = make_args(nofilter=True)
            patches = {
                "datetime": FixedDateTime,
                "_fetch_with_retries": MagicMock(
                    side_effect=[("ob", 200, None), ("rb", 200, None)]
                ),
                "parse_results": MagicMock(side_effect=[[], []]),
                "update_price_map": MagicMock(),
            }
            code, out, err = run_main(
                args, {"headers": {}, "cookies": {}}, patches
            )
            self.assertIsNone(code)
            self.assertEqual(flights.SEARCH["departure_date"], "2030-05-31")
            self.assertEqual(flights.SEARCH["return_date"], "2030-06-18")

        with self.subTest(case="explicit_dates"):
            args = make_args(
                nofilter=True,
                depart="2030-07-01",
                ret="2030-08-01",
            )
            patches = {
                "datetime": FixedDateTime,
                "_fetch_with_retries": MagicMock(
                    side_effect=[("ob", 200, None), ("rb", 200, None)]
                ),
                "parse_results": MagicMock(side_effect=[[], []]),
                "update_price_map": MagicMock(),
            }
            code, out, err = run_main(
                args, {"headers": {}, "cookies": {}}, patches
            )
            self.assertIsNone(code)
            self.assertEqual(flights.SEARCH["departure_date"], "2030-07-01")
            self.assertEqual(flights.SEARCH["return_date"], "2030-08-01")

        with self.subTest(case="help_text"):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                with self.assertRaises(SystemExit) as ctx:
                    flights.parse_args(["--help"])
            self.assertEqual(ctx.exception.code, 0)
            text = output.getvalue()
            self.assertIn("today + 30 days", text)
            self.assertIn("departure + 18 days", text)
            self.assertNotIn("2026-08-17", text)
            self.assertNotIn("2026-09-04", text)

    def test_budget_zero_configured_as_unlimited(self):
        args = make_args(budget=0, price_map=True)
        with patch.object(flights, "parse_args", return_value=args):
            with patch.object(flights, "configure_rate_limiter") as configure:
                with patch.object(flights, "print_price_map"):
                    flights._main()
        self.assertIsNone(configure.call_args.kwargs["budget"])
        self.assertIsNone(flights.RateLimiter(budget=0).budget)


class SortingTests(unittest.TestCase):
    def test_date_is_primary_over_price_and_duration(self):
        outbound = make_result(
            flight="QR1", price=9000, dep_date="2026-08-17", direction="OB"
        )
        outbound["duration_seconds"] = 72000
        returning = make_result(
            flight="QR2",
            price=1000,
            dep_date="2026-09-04",
            direction="RB",
            origin="BKK",
            destination="OSL",
        )
        returning["duration_seconds"] = 18000
        for order in ("duration", "price"):
            with self.subTest(order=order):
                ordered = flights.sort_results([returning, outbound], order)
                self.assertEqual([r["segments"][0]["flight"] for r in ordered], ["QR1", "QR2"])

    def test_default_order_is_duration_then_price_within_date(self):
        slow = make_result(flight="QR1", price=9000, dep_date="2026-08-17")
        slow["duration_seconds"] = 72000
        fast_expensive = make_result(
            flight="QR2", price=9000, dep_date="2026-08-17"
        )
        fast_expensive["duration_seconds"] = 36000
        fast_cheap = make_result(flight="QR3", price=5000, dep_date="2026-08-17")
        fast_cheap["duration_seconds"] = 36000
        ordered = flights.sort_results(
            [slow, fast_expensive, fast_cheap], "duration"
        )
        self.assertEqual(
            [r["segments"][0]["flight"] for r in ordered], ["QR3", "QR2", "QR1"]
        )

    def test_price_override_keeps_date_primary(self):
        cheap_slow = make_result(flight="QR1", price=4000, dep_date="2026-08-17")
        cheap_slow["duration_seconds"] = 72000
        expensive_fast = make_result(
            flight="QR2", price=8000, dep_date="2026-08-17"
        )
        expensive_fast["duration_seconds"] = 36000
        later_cheap = make_result(
            flight="QR3", price=1000, dep_date="2026-08-18"
        )
        later_cheap["duration_seconds"] = 18000
        ordered = flights.sort_results(
            [expensive_fast, later_cheap, cheap_slow], "price"
        )
        self.assertEqual(
            [r["segments"][0]["flight"] for r in ordered], ["QR1", "QR2", "QR3"]
        )

    def test_same_date_outbound_sorts_before_return(self):
        outbound = make_result(
            flight="QR1", price=9000, dep_date="2026-08-17", direction="OB"
        )
        returning = make_result(
            flight="QR2",
            price=1000,
            dep_date="2026-08-17",
            direction="RB",
            origin="BKK",
            destination="OSL",
        )
        ordered = flights.sort_results([returning, outbound], "price")
        self.assertEqual([r["segments"][0]["flight"] for r in ordered], ["QR1", "QR2"])

    def test_limit_scan_results_ranks_then_displays_in_date_order(self):
        durations = [72000, 36000, 18000, 108000]
        results = []
        for i, duration in enumerate(durations):
            outbound = make_result(
                flight=f"OB{i}",
                dep_date=f"2026-08-{10 + i}",
                direction="OB",
            )
            outbound["duration_seconds"] = duration
            results.append(outbound)
            returning = make_result(
                flight=f"RB{i}",
                dep_date=f"2026-09-{10 + i}",
                direction="RB",
                origin="BKK",
                destination="OSL",
            )
            returning["duration_seconds"] = duration
            results.append(returning)
        limited = flights.limit_scan_results(results, "duration", 2)
        self.assertEqual(
            [r["segments"][0]["flight"] for r in limited],
            ["OB1", "OB2", "RB1", "RB2"],
        )

    def test_sort_flag_is_alias_for_weight_and_defaults_to_duration(self):
        self.assertEqual(flights.SCAN["weight"], "duration")
        self.assertEqual(
            flights.parse_args(["--sort", "price"]).weight, "price"
        )
        self.assertEqual(
            flights.parse_args(["--weight", "duration"]).weight, "duration"
        )
        self.assertIsNone(flights.parse_args([]).weight)


class LocalModeTests(unittest.TestCase):
    def test_local_modes_run_without_config_but_live_requires_it(self):
        with self.subTest(mode="price_map"):
            args = make_args(price_map=True)
            printer = MagicMock()
            code, out, err = run_main(
                args, config=None, patches={"print_price_map": printer}
            )
            self.assertIsNone(code)
            printer.assert_called_once_with()
            self.assertNotIn("No saved config", err)

        with self.subTest(mode="calendar_trends"):
            args = make_args(calendar="Aug 2026", trends=True)
            calendar = MagicMock()
            trends = MagicMock()
            archive = MagicMock(return_value={})
            code, out, err = run_main(
                args,
                config=None,
                patches={
                    "print_price_calendar": calendar,
                    "print_price_trends": trends,
                    "_load_archive_prices": archive,
                },
            )
            self.assertIsNone(code)
            calendar.assert_called_once()
            trends.assert_called_once()
            self.assertNotIn("No saved config", err)

        with self.subTest(mode="live_requires_config"):
            args = make_args()
            code, out, err = run_main(args, config=None)
            self.assertEqual(code, 1)
            self.assertIn("No saved config", err)

    def test_prune_cache_without_config_archives_entries(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        old = {
            "OFFERS_CACHE_FILE": flights.OFFERS_CACHE_FILE,
            "CACHE_ARCHIVE_FILE": flights.CACHE_ARCHIVE_FILE,
            "DATA_LOCK_FILE": flights.DATA_LOCK_FILE,
        }
        flights.OFFERS_CACHE_FILE = os.path.join(tmp.name, "offers_cache.json")
        flights.CACHE_ARCHIVE_FILE = os.path.join(tmp.name, "cache_archive.json")
        flights.DATA_LOCK_FILE = os.path.join(tmp.name, ".data_state.lock")
        self.addCleanup(
            lambda: [
                setattr(flights, key, value) for key, value in old.items()
            ]
        )
        stale_date = (
            flights.datetime.now() - flights.timedelta(days=5)
        ).strftime("%Y-%m-%d")
        stale_key = f"OSL_BKK_{stale_date}"
        fresh_key = "OSL_BKK_2099-01-01"
        with open(flights.OFFERS_CACHE_FILE, "w") as f:
            json.dump(
                {
                    stale_key: {"date": stale_date, "data": {"old": True}},
                    fresh_key: {"date": "2099-01-01", "data": {"new": True}},
                },
                f,
            )
        args = make_args(prune_cache=True)
        code, out, err = run_main(
            args,
            config=None,
            patches={"archive_stale_cache": flights.archive_stale_cache},
        )
        self.assertIsNone(code)
        with open(flights.CACHE_ARCHIVE_FILE) as f:
            archive = json.load(f)
        self.assertEqual([a["key"] for a in archive], [stale_key])
        with open(flights.OFFERS_CACHE_FILE) as f:
            cache = json.load(f)
        self.assertEqual(list(cache), [fresh_key])
        self.assertIn("Pruned", err)
        self.assertNotIn("No saved config", err)


class PasteOnlyTests(unittest.TestCase):
    def test_paste_only_saves_and_exits_without_live_call(self):
        args = make_args(paste=True)
        with patch.object(flights, "parse_args", return_value=args):
            with patch.object(
                flights,
                "prompt_paste_headers",
                return_value={"headers": {}, "cookies": {}},
            ) as prompt:
                with patch.object(flights, "configure_rate_limiter"):
                    with patch.object(flights, "load_config") as load:
                        with patch.object(flights, "_fetch_with_retries") as fetch:
                            with patch.object(flights.log, "print"):
                                flights._main(["--paste"])
        prompt.assert_called_once_with()
        load.assert_not_called()
        fetch.assert_not_called()

    def test_paste_with_other_args_continues(self):
        args = make_args(paste=True, price_map=True)
        with patch.object(flights, "parse_args", return_value=args):
            with patch.object(
                flights,
                "prompt_paste_headers",
                return_value={"headers": {}, "cookies": {}},
            ):
                with patch.object(flights, "configure_rate_limiter"):
                    with patch.object(flights, "print_price_map") as printer:
                        flights._main(["--paste", "--price-map"])
        printer.assert_called_once_with()


class TransientStatsTests(unittest.TestCase):
    def setUp(self):
        self.stats = {"calls": 0, "failures": 0}

    def call(self, fetch_value, parse_value=None):
        fetch = MagicMock(
            side_effect=fetch_value
            if isinstance(fetch_value, BaseException)
            else [fetch_value]
        )
        patches = {"_fetch_with_retries": fetch}
        if parse_value is not None:
            patches["parse_results"] = parse_value
        with contextlib.ExitStack() as stack:
            for name, value in patches.items():
                stack.enter_context(patch.object(flights, name, value))
            stack.enter_context(patch.object(flights, "log"))
            try:
                result = flights._fetch_and_parse(
                    {},
                    {},
                    "OSL",
                    "BKK",
                    "2026-12-29",
                    "2026-12-29",
                    "2027-01-15",
                    "OB",
                    25,
                    stats=self.stats,
                )
                return result, None
            except BaseException as exc:
                return None, exc

    def _reset_stats(self):
        self.stats["calls"] = 0
        self.stats["failures"] = 0

    def test_failures_are_counted(self):
        with self.subTest(case="request_failed"):
            self._reset_stats()
            result, exc = self.call(flights.RequestFailed(500, "boom"))
            self.assertEqual(result, [])
            self.assertIsNone(exc)
            self.assertEqual(self.stats, {"calls": 1, "failures": 1})

        with self.subTest(case="data_none"):
            self._reset_stats()
            result, exc = self.call((None, 500, "err"))
            self.assertEqual(result, [])
            self.assertEqual(self.stats, {"calls": 1, "failures": 1})

        with self.subTest(case="parse_request_failed"):
            self._reset_stats()
            result, exc = self.call(
                ({"flightOffers": []}, 200, None),
                parse_value=MagicMock(
                    side_effect=flights.RequestFailed(200, "malformed")
                ),
            )
            self.assertEqual(result, [])
            self.assertEqual(self.stats, {"calls": 1, "failures": 1})

    def test_success_abort_and_cache_hit_are_counted(self):
        with self.subTest(case="abort_not_counted"):
            self._reset_stats()
            result, exc = self.call(flights.EdgeBlock("block", status=403))
            self.assertIsNone(result)
            self.assertIsInstance(exc, flights.EdgeBlock)
            self.assertEqual(self.stats, {"calls": 1, "failures": 0})

        with self.subTest(case="success"):
            self._reset_stats()
            result, exc = self.call(({"flightOffers": []}, 200, None))
            self.assertEqual(result, [])
            self.assertEqual(self.stats, {"calls": 1, "failures": 0})

        with self.subTest(case="cache_hit"):
            self._reset_stats()
            tmp = tempfile.TemporaryDirectory()
            self.addCleanup(tmp.cleanup)
            old_cache = flights.OFFERS_CACHE_FILE
            flights.OFFERS_CACHE_FILE = os.path.join(
                tmp.name, "offers_cache.json"
            )
            self.addCleanup(setattr, flights, "OFFERS_CACHE_FILE", old_cache)
            today = flights.datetime.now().strftime("%Y-%m-%d")
            with open(flights.OFFERS_CACHE_FILE, "w") as f:
                json.dump(
                    {
                        "OSL_BKK_2026-12-29": {
                            "date": today,
                            "data": {"flightOffers": []},
                        }
                    },
                    f,
                )
            limiter = FakeLimiter()
            with patch.object(flights, "get_rate_limiter", return_value=limiter):
                with patch.object(flights, "log"):
                    result = flights._fetch_and_parse(
                        {},
                        {},
                        "OSL",
                        "BKK",
                        "2026-12-29",
                        "2026-12-29",
                        "2027-01-15",
                        "OB",
                        25,
                        stats=self.stats,
                    )
            self.assertEqual(result, [])
            self.assertEqual(self.stats, {"calls": 1, "failures": 0})
            limiter.wait.assert_not_called()


class ScanFailureExitTests(unittest.TestCase):
    def test_scan_all_failed_exits_one(self):
        with self.subTest(case="synthetic"):
            args = make_args(scan=True, nofilter=True)

            def fake_scan(*_args, stats=None, **_kwargs):
                stats["calls"] = 3
                stats["failures"] = 3
                return []

            code, out, err = run_main(
                args,
                {"headers": {}, "cookies": {}},
                {"scan_outbound": fake_scan},
            )
            self.assertEqual(code, 1)
            self.assertIn("failed", err)
            self.assertNotIn("No results after scan + filtering", err)

        with self.subTest(case="real_transient"):
            args = make_args(scan=True, nofilter=True)
            patches = {
                "_fetch_with_retries": MagicMock(
                    side_effect=flights.RequestFailed(500, "boom")
                ),
                "update_price_map": MagicMock(),
            }
            code, out, err = run_main(
                args, {"headers": {}, "cookies": {}}, patches
            )
            self.assertEqual(code, 1)
            self.assertIn("All 9 request(s) failed", err)
            self.assertNotIn("No results after scan + filtering", err)

    def test_return_and_drift_all_failed_exit_one(self):
        cases = (
            (
                "return_scan",
                make_args(scan=True, rb=1, nofilter=True),
                "scan_return",
            ),
            ("drift", make_args(drift=1, nofilter=True), "drift_scan"),
            (
                "drift_ret",
                make_args(drift_ret=1, nofilter=True),
                "drift_scan_return",
            ),
        )
        for name, args, patch_name in cases:
            with self.subTest(case=name):

                def fake_scan(*_args, stats=None, **_kwargs):
                    stats["calls"] = 2
                    stats["failures"] = 2
                    return []

                code, out, err = run_main(
                    args,
                    {"headers": {}, "cookies": {}},
                    {patch_name: fake_scan},
                )
                self.assertEqual(code, 1)
                self.assertIn("failed", err)

    def test_mixed_and_skipped_failures_keep_exit_zero(self):
        result = make_result()

        with self.subTest(case="scan_mixed"):
            args = make_args(scan=True, nofilter=True)

            def fake_scan(*_args, stats=None, **_kwargs):
                stats["calls"] = 3
                stats["failures"] = 1
                return [result]

            code, out, err = run_main(
                args,
                {"headers": {}, "cookies": {}},
                {"scan_outbound": fake_scan},
            )
            self.assertIsNone(code)
            self.assertIn("1/3 requests failed", err)

        with self.subTest(case="scan_cache_hits"):
            args = make_args(scan=True, nofilter=True)

            def cache_scan(*_args, stats=None, **_kwargs):
                stats["calls"] = 3
                stats["failures"] = 0
                return [result]

            code, out, err = run_main(
                args,
                {"headers": {}, "cookies": {}},
                {"scan_outbound": cache_scan},
            )
            self.assertIsNone(code)
            self.assertNotIn("requests failed", err)

        with self.subTest(case="scan_skipped"):
            args = make_args(scan=True, nofilter=True)

            def skipped_scan(*_args, stats=None, **_kwargs):
                return []

            code, out, err = run_main(
                args,
                {"headers": {}, "cookies": {}},
                {"scan_outbound": skipped_scan},
            )
            self.assertIsNone(code)
            self.assertIn("No results after scan + filtering", err)
            self.assertNotIn("requests failed", err)

        with self.subTest(case="drift_mixed"):
            args = make_args(drift=1, nofilter=True)

            def mixed_drift(*_args, stats=None, **_kwargs):
                stats["calls"] = 2
                stats["failures"] = 1
                return [result]

            code, out, err = run_main(
                args,
                {"headers": {}, "cookies": {}},
                {"drift_scan": mixed_drift},
            )
            self.assertIsNone(code)
            self.assertIn("1/2 requests failed", err)

        with self.subTest(case="drift_skipped"):
            args = make_args(drift=1, nofilter=True)

            def skipped_drift(*_args, stats=None, **_kwargs):
                return []

            code, out, err = run_main(
                args,
                {"headers": {}, "cookies": {}},
                {"drift_scan": skipped_drift},
            )
            self.assertIsNone(code)
            self.assertNotIn("requests failed", err)


class NonScanReturnFailureTests(unittest.TestCase):
    def run_failure(self, return_effect, parse_effect=None):
        ob = {"marker": "outbound"}
        args = make_args(nofilter=True)
        patches = {
            "_fetch_with_retries": MagicMock(
                side_effect=[("ob", 200, None), return_effect]
            ),
            "parse_results": parse_effect
            if parse_effect is not None
            else MagicMock(return_value=[ob]),
            "update_price_map": MagicMock(),
            "filter_dirt_cheap": lambda r: r,
            "merge_cabins": lambda r: r,
            "print_compare_table": MagicMock(),
            "_log_error": MagicMock(),
        }
        code, out, err = run_main(
            args, {"headers": {}, "cookies": {}}, patches
        )
        return code, patches["update_price_map"], ob

    def test_transient_return_failures_update_price_map(self):
        for name, effect in (
            ("request_failed", flights.RequestFailed(0, "timed out")),
            ("data_none", (None, 500, "err")),
        ):
            with self.subTest(case=name):
                code, update, ob = self.run_failure(effect)
                self.assertEqual(code, 1)
                update.assert_called_once_with([ob])

    def test_parse_and_other_return_failures_update_price_map(self):
        with self.subTest(case="parse_failed"):
            ob = {"marker": "outbound"}
            parse = MagicMock(
                side_effect=[[ob], flights.RequestFailed(200, "malformed")]
            )
            code, update, result = self.run_failure(
                ("rb", 200, None), parse_effect=parse
            )
            self.assertEqual(code, 1)
            update.assert_called_once()
            updated = update.call_args[0][0]
            self.assertEqual(updated[0]["marker"], "outbound")
            self.assertIn("_dep_date", updated[0])

        for exc in (
            flights.EdgeBlock("block", status=403, reference="18.abc.123"),
            flights.AppAuthError("auth"),
            flights.RateLimited(60, "2026-10-26T12:00:00+00:00"),
            flights.BudgetExceeded(1),
        ):
            with self.subTest(case=type(exc).__name__):
                code, update, ob = self.run_failure(exc)
                self.assertEqual(code, 1)
                update.assert_called_once_with([ob])


if __name__ == "__main__":
    unittest.main()
