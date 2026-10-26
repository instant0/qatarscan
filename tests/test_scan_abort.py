import inspect
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

import flights


class FakeResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text

    def json(self):
        return json.loads(self.text)


class FakeCookieJar(dict):
    def get_dict(self):
        return dict(self)


class FakeSession:
    def __init__(self, response=None, error=None):
        self.cookies = FakeCookieJar()
        self.response = response or FakeResponse(200, '{"flightOffers": []}')
        self.error = error

    def post(self, url, headers=None, json=None, timeout=None):
        if self.error is not None:
            raise self.error
        return self.response


class FakeLimiter:
    def __init__(self):
        self.wait = MagicMock()
        self.record_call = MagicMock()
        self.record_block = MagicMock()
        self.is_blocked = MagicMock(return_value=False)


def make_args(**overrides):
    args = flights.parse_args([])
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def make_abort_scan(calls=2, exc=None):
    state = {"count": 0}

    def fake_fetch(*args, **kwargs):
        state["count"] += 1
        if state["count"] > calls:
            raise exc
        return [{"marker": state["count"]}]

    return fake_fetch, state


class ScanFunctionAbortTests(unittest.TestCase):
    def setUp(self):
        self.old_max_retries = flights.SCAN["max_retries"]
        flights.SCAN["max_retries"] = 0

    def tearDown(self):
        flights.SCAN["max_retries"] = self.old_max_retries

    def test_scan_dates_edge_on_third_call_finalizes_once(self):
        edge = flights.EdgeBlock("block", status=403, reference="18.abc.123")
        fake_fetch, state = make_abort_scan(2, edge)
        args = make_args(ob=2, rb=None, min_trip=0, nofilter=True)
        with patch.object(flights, "_fetch_and_parse", side_effect=fake_fetch):
            with patch.object(flights, "update_price_map") as update:
                with self.assertRaises(flights.ScanAborted) as ctx:
                    flights._scan_dates(
                        {}, {}, args, {}, "price", 25, 5, True, "OB"
                    )
        aborted = ctx.exception
        self.assertEqual(state["count"], 3)
        self.assertEqual(aborted.reason, "edge")
        self.assertEqual(aborted.results, [{"marker": 1}, {"marker": 2}])
        update.assert_called_once_with([{"marker": 1}, {"marker": 2}])

    def test_drift_scan_edge_on_third_call_finalizes_once(self):
        edge = flights.EdgeBlock("block", status=403, reference="18.abc.123")
        fake_fetch, state = make_abort_scan(2, edge)
        args = make_args(drift=1, nofilter=True)
        with patch.object(flights, "_fetch_and_parse", side_effect=fake_fetch):
            with patch.object(flights, "update_price_map") as update:
                with self.assertRaises(flights.ScanAborted) as ctx:
                    flights.drift_scan({}, {}, args, {}, 25)
        aborted = ctx.exception
        self.assertEqual(state["count"], 3)
        self.assertEqual(aborted.reason, "edge")
        self.assertEqual(aborted.results, [{"marker": 1}, {"marker": 2}])
        update.assert_called_once_with([{"marker": 1}, {"marker": 2}])

    def test_drift_scan_return_edge_on_third_call_finalizes_once(self):
        edge = flights.EdgeBlock("block", status=403, reference="18.abc.123")
        fake_fetch, state = make_abort_scan(2, edge)
        args = make_args(drift_ret=1, min_trip=0, nofilter=True)
        with patch.object(flights, "_fetch_and_parse", side_effect=fake_fetch):
            with patch.object(flights, "update_price_map") as update:
                with self.assertRaises(flights.ScanAborted) as ctx:
                    flights.drift_scan_return({}, {}, args, {}, 25)
        aborted = ctx.exception
        self.assertEqual(state["count"], 3)
        self.assertEqual(aborted.reason, "edge")
        self.assertEqual(aborted.results, [{"marker": 1}, {"marker": 2}])
        update.assert_called_once_with([{"marker": 1}, {"marker": 2}])

    def test_scan_dates_reason_for_each_typed_error(self):
        cases = [
            (flights.AppAuthError("auth"), "auth"),
            (flights.RateLimited(120, "2026-10-26T12:00:00+00:00"), "rate"),
            (flights.BudgetExceeded(1), "budget"),
        ]
        for exc, expected in cases:
            with self.subTest(reason=expected):
                fake_fetch, state = make_abort_scan(2, exc)
                args = make_args(ob=2, rb=None, min_trip=0, nofilter=True)
                with patch.object(flights, "_fetch_and_parse", side_effect=fake_fetch):
                    with patch.object(flights, "update_price_map"):
                        with self.assertRaises(flights.ScanAborted) as ctx:
                            flights._scan_dates(
                                {}, {}, args, {}, "price", 25, 5, True, "OB"
                            )
                self.assertEqual(state["count"], 3)
                self.assertEqual(ctx.exception.reason, expected)
                self.assertEqual(len(ctx.exception.results), 2)

    def test_record_block_only_for_edge(self):
        cases = [
            (flights.EdgeBlock("block"), "edge", True),
            (flights.AppAuthError("auth"), "auth", False),
            (flights.RateLimited(60, "2026-10-26T12:00:00+00:00"), "rate", False),
            (flights.BudgetExceeded(1), "budget", False),
        ]
        for exc, reason, expected in cases:
            with self.subTest(reason=reason):
                limiter = FakeLimiter()
                aborted = flights.ScanAborted([], flights._abort_reason(exc), exc)
                with patch.object(flights, "get_rate_limiter", return_value=limiter):
                    flights._record_block_for_abort(aborted)
                if expected:
                    limiter.record_block.assert_called_once_with(
                        flights.SCAN["block_minutes"]
                    )
                else:
                    limiter.record_block.assert_not_called()


class AbortMessageTests(unittest.TestCase):
    def test_edge_with_reference(self):
        cause = flights.EdgeBlock("block", status=403, reference="18.abc.123")
        message = flights._abort_message(flights.ScanAborted([], "edge", cause))
        self.assertIn("Akamai edge", message)
        self.assertIn("18.abc.123", message)
        self.assertIn("--paste", message)

    def test_edge_without_reference(self):
        cause = flights.EdgeBlock("block", status=403)
        message = flights._abort_message(flights.ScanAborted([], "edge", cause))
        self.assertNotIn("None", message)

    def test_auth(self):
        message = flights._abort_message(
            flights.ScanAborted([], "auth", flights.AppAuthError("bad"))
        )
        self.assertIn("Session rejected", message)
        self.assertIn("--paste", message)

    def test_rate_remaining_minutes(self):
        cause = flights.RateLimited(125, "2026-10-26T12:10:00+00:00")
        message = flights._abort_message(flights.ScanAborted([], "rate", cause))
        self.assertIn("about 3 min remaining", message)
        self.assertIn("--force", message)
        self.assertIn("--clear-block", message)

    def test_budget_count(self):
        cause = flights.BudgetExceeded(7)
        message = flights._abort_message(flights.ScanAborted([], "budget", cause))
        self.assertIn("(7 calls)", message)
        self.assertIn("--budget", message)

    def test_fallback(self):
        message = flights._abort_message(flights.ScanAborted([], "unknown"))
        self.assertEqual(message, "Scan aborted.")


class RetryTests(unittest.TestCase):
    def setUp(self):
        self.old_max_retries = flights.SCAN["max_retries"]

    def tearDown(self):
        flights.SCAN["max_retries"] = self.old_max_retries

    def test_transient_500_retried_then_succeeds(self):
        flights.SCAN["max_retries"] = 1
        with patch.object(
            flights,
            "fetch_flights",
            side_effect=[flights.RequestFailed(500, "boom"), ("data", 200, None)],
        ) as fetch:
            result = flights._fetch_with_retries({}, {}, "OSL", "BKK", "2026-12-29")
        self.assertEqual(result, ("data", 200, None))
        self.assertEqual(fetch.call_count, 2)

    def test_transient_500_not_retried_by_default(self):
        flights.SCAN["max_retries"] = 0
        with patch.object(
            flights, "fetch_flights", side_effect=flights.RequestFailed(500, "boom")
        ) as fetch:
            with self.assertRaises(flights.RequestFailed):
                flights._fetch_with_retries({}, {}, "OSL", "BKK", "2026-12-29")
        self.assertEqual(fetch.call_count, 1)

    def test_hard_errors_never_retried(self):
        cases = [
            flights.EdgeBlock("block", status=403),
            flights.AppAuthError("auth"),
            flights.RateLimited(60, "2026-10-26T12:00:00+00:00"),
            flights.BudgetExceeded(1),
        ]
        for exc in cases:
            with self.subTest(exc=type(exc).__name__):
                flights.SCAN["max_retries"] = 1
                with patch.object(flights, "fetch_flights", side_effect=exc) as fetch:
                    with self.assertRaises(type(exc)):
                        flights._fetch_with_retries({}, {}, "OSL", "BKK", "2026-12-29")
                self.assertEqual(fetch.call_count, 1)

    def test_non_retryable_4xx_not_retried(self):
        flights.SCAN["max_retries"] = 1
        with patch.object(
            flights, "fetch_flights", side_effect=flights.RequestFailed(404, "nope")
        ) as fetch:
            with self.assertRaises(flights.RequestFailed):
                flights._fetch_with_retries({}, {}, "OSL", "BKK", "2026-12-29")
        self.assertEqual(fetch.call_count, 1)

    def test_transient_failure_keeps_scan_going(self):
        flights.SCAN["max_retries"] = 0
        with patch.object(
            flights,
            "_fetch_with_retries",
            side_effect=flights.RequestFailed(500, "boom"),
        ):
            with patch.object(flights, "log") as log:
                results = flights._fetch_and_parse(
                    {}, {}, "OSL", "BKK", "2026-12-29",
                    "2026-12-29", "2027-01-15", "OB", 25,
                )
        self.assertEqual(results, [])
        log.print.assert_called()

    def test_edge_propagates_from_fetch_and_parse(self):
        flights.SCAN["max_retries"] = 1
        edge = flights.EdgeBlock("block", status=403)
        with patch.object(flights, "_fetch_with_retries", side_effect=edge):
            with self.assertRaises(flights.EdgeBlock):
                flights._fetch_and_parse(
                    {}, {}, "OSL", "BKK", "2026-12-29",
                    "2026-12-29", "2027-01-15", "OB", 25,
                )


class FetchFlightsHardeningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_cache_file = flights.OFFERS_CACHE_FILE
        flights.OFFERS_CACHE_FILE = os.path.join(self.tmp.name, "offers_cache.json")
        self.addCleanup(setattr, flights, "OFFERS_CACHE_FILE", self.old_cache_file)
        flights.set_nocache(False)
        self.addCleanup(flights.set_nocache, False)
        self.today = flights.datetime.now().strftime("%Y-%m-%d")
        self.limiter = FakeLimiter()
        self.limiter_patcher = patch.object(
            flights, "get_rate_limiter", return_value=self.limiter
        )
        self.limiter_patcher.start()
        self.addCleanup(self.limiter_patcher.stop)
        self.persist_patcher = patch.object(flights, "_persist_cookies")
        self.persist = self.persist_patcher.start()
        self.addCleanup(self.persist_patcher.stop)

    def test_timeout_wrapped_and_record_call_runs(self):
        session = FakeSession(error=requests.Timeout("timed out"))
        with patch.object(flights.requests, "Session", return_value=session):
            with self.assertRaises(flights.RequestFailed) as ctx:
                flights.fetch_flights({}, {}, "OSL", "BKK", "2026-12-29")
        self.assertEqual(ctx.exception.status, 0)
        self.assertIn("Network error", str(ctx.exception))
        self.limiter.record_call.assert_called_once()
        self.persist.assert_not_called()

    def test_edge_html_extracts_reference_without_body(self):
        body = (
            "<html><title>Access Denied</title>"
            "Reference #18.abc.123 errors.edgesuite.net</html>"
        )
        session = FakeSession(response=FakeResponse(403, body))
        with patch.object(flights.requests, "Session", return_value=session):
            with self.assertRaises(flights.EdgeBlock) as ctx:
                flights.fetch_flights({}, {}, "OSL", "BKK", "2026-12-29")
        edge = ctx.exception
        self.assertEqual(edge.reference, "18.abc.123")
        self.assertEqual(edge.status, 403)
        self.assertNotIn("<html", str(edge))
        self.persist.assert_called_once()
        self.limiter.record_call.assert_called_once()

        aborted = flights.ScanAborted([], "edge", edge)
        with patch.object(flights.log, "print") as log_print:
            flights._log_error(flights._abort_message(aborted))
        logged = " ".join(
            str(arg) for call in log_print.call_args_list for arg in call.args
        )
        self.assertNotIn("<html", logged)
        self.assertIn("18.abc.123", logged)

    def test_html_request_failed_summary_is_omitted(self):
        failed = flights.RequestFailed(403, "<html>Access Denied</html>")
        self.assertEqual(failed.body_summary, "HTML response body (omitted)")
        with patch.object(flights.log, "print") as log_print:
            flights._log_error(str(failed))
        logged = " ".join(
            str(arg) for call in log_print.call_args_list for arg in call.args
        )
        self.assertIn("HTML response body (omitted)", logged)
        self.assertNotIn("<html", logged)

    def test_cache_hit_skips_limiter(self):
        with open(flights.OFFERS_CACHE_FILE, "w") as f:
            json.dump(
                {"OSL_BKK_2026-12-29": {"date": self.today, "data": {"cached": True}}},
                f,
            )
        with patch.object(flights.HttpClient, "post_offers") as post:
            data, status, err = flights.fetch_flights(
                {}, {}, "OSL", "BKK", "2026-12-29"
            )
        post.assert_not_called()
        self.limiter.wait.assert_not_called()
        self.limiter.record_call.assert_not_called()
        self.assertEqual(data, {"cached": True})


class MainAbortTests(unittest.TestCase):
    def _run(self, args, limiter, scan_target, scan_value):
        with patch.object(flights, "parse_args", return_value=args):
            with patch.object(
                flights, "configure_rate_limiter", return_value=limiter
            ):
                with patch.object(flights, "get_rate_limiter", return_value=limiter):
                    with patch.object(
                        flights,
                        "load_config",
                        return_value={"headers": {}, "cookies": {}},
                    ):
                        with patch.object(flights, "validate_config", return_value=True):
                            with patch.object(
                                flights, "archive_stale_cache", return_value=0
                            ):
                                with patch.object(
                                    flights, scan_target, side_effect=scan_value
                                ):
                                    with patch.object(flights, "_log_error") as log_error:
                                        with self.assertRaises(SystemExit) as ctx:
                                            flights.main()
        return ctx.exception.code, log_error

    def test_drift_edge_records_block_and_exits_one(self):
        for reason, exc in [
            ("edge", flights.EdgeBlock("block", status=403, reference="18.abc.123")),
            ("auth", flights.AppAuthError("auth")),
            ("rate", flights.RateLimited(60, "2026-10-26T12:00:00+00:00")),
            ("budget", flights.BudgetExceeded(1)),
        ]:
            with self.subTest(reason=reason):
                limiter = FakeLimiter()
                args = make_args(drift=1)
                code, log_error = self._run(
                    args,
                    limiter,
                    "drift_scan",
                    flights.ScanAborted([], reason, exc),
                )
                self.assertEqual(code, 1)
                log_error.assert_called_once()
                if reason == "edge":
                    limiter.record_block.assert_called_once_with(
                        flights.SCAN["block_minutes"]
                    )
                else:
                    limiter.record_block.assert_not_called()

    def test_drift_zero_partials_still_exits_one_and_renders_nothing(self):
        limiter = FakeLimiter()
        args = make_args(drift=1)
        with patch.object(flights, "parse_args", return_value=args):
            with patch.object(
                flights, "configure_rate_limiter", return_value=limiter
            ):
                with patch.object(flights, "get_rate_limiter", return_value=limiter):
                    with patch.object(
                        flights,
                        "load_config",
                        return_value={"headers": {}, "cookies": {}},
                    ):
                        with patch.object(flights, "validate_config", return_value=True):
                            with patch.object(
                                flights, "archive_stale_cache", return_value=0
                            ):
                                with patch.object(
                                    flights,
                                    "drift_scan",
                                    side_effect=flights.ScanAborted(
                                        [], "auth", flights.AppAuthError("auth")
                                    ),
                                ):
                                    with patch.object(
                                        flights, "print_drift_pair_table"
                                    ) as table:
                                        with patch.object(
                                            flights, "_log_error"
                                        ) as log_error:
                                            with self.assertRaises(SystemExit) as ctx:
                                                flights.main()
        self.assertEqual(ctx.exception.code, 1)
        table.assert_not_called()
        log_error.assert_called_once()
        self.assertIn("Session rejected", str(log_error.call_args[0][0]))

    def test_scan_does_not_start_return_after_outbound_abort(self):
        limiter = FakeLimiter()
        args = make_args(scan=True, ob=1, rb=1)
        with patch.object(flights, "parse_args", return_value=args):
            with patch.object(
                flights, "configure_rate_limiter", return_value=limiter
            ):
                with patch.object(flights, "get_rate_limiter", return_value=limiter):
                    with patch.object(
                        flights,
                        "load_config",
                        return_value={"headers": {}, "cookies": {}},
                    ):
                        with patch.object(flights, "validate_config", return_value=True):
                            with patch.object(
                                flights, "archive_stale_cache", return_value=0
                            ):
                                with patch.object(
                                    flights,
                                    "scan_outbound",
                                    side_effect=flights.ScanAborted(
                                        [],
                                        "edge",
                                        flights.EdgeBlock("block", 403, "18.abc.123"),
                                    ),
                                ):
                                    with patch.object(
                                        flights, "scan_return"
                                    ) as scan_return:
                                        with patch.object(flights, "_log_error"):
                                            with self.assertRaises(SystemExit) as ctx:
                                                flights.main()
        self.assertEqual(ctx.exception.code, 1)
        scan_return.assert_not_called()
        limiter.record_block.assert_called_once_with(flights.SCAN["block_minutes"])

    def test_non_scan_return_edge_renders_outbound_partial(self):
        limiter = FakeLimiter()
        args = make_args()
        ob_result = {"marker": "outbound"}
        with patch.object(flights, "parse_args", return_value=args):
            with patch.object(
                flights, "configure_rate_limiter", return_value=limiter
            ):
                with patch.object(flights, "get_rate_limiter", return_value=limiter):
                    with patch.object(
                        flights,
                        "load_config",
                        return_value={"headers": {}, "cookies": {}},
                    ):
                        with patch.object(flights, "validate_config", return_value=True):
                            with patch.object(
                                flights, "archive_stale_cache", return_value=0
                            ):
                                with patch.object(
                                    flights,
                                    "_fetch_with_retries",
                                    side_effect=[
                                        ("ob", 200, None),
                                        flights.EdgeBlock("block", 403, "18.abc.123"),
                                    ],
                                ):
                                    with patch.object(
                                        flights, "parse_results", return_value=[ob_result]
                                    ):
                                        with patch.object(
                                            flights,
                                            "filter_dirt_cheap",
                                            side_effect=lambda r: r,
                                        ):
                                            with patch.object(
                                                flights,
                                                "filter_results",
                                                side_effect=lambda r, *a: r,
                                            ):
                                                with patch.object(
                                                    flights, "update_price_map"
                                                ) as update:
                                                    with patch.object(
                                                        flights,
                                                        "merge_cabins",
                                                        side_effect=lambda r: r,
                                                    ):
                                                        with patch.object(
                                                            flights,
                                                            "print_compare_table",
                                                        ) as table:
                                                            with patch.object(
                                                                flights,
                                                                "print_booking_links",
                                                            ):
                                                                with patch.object(
                                                                    flights,
                                                                    "_log_error",
                                                                ):
                                                                    with self.assertRaises(
                                                                        SystemExit
                                                                    ) as ctx:
                                                                        flights.main()
        self.assertEqual(ctx.exception.code, 1)
        update.assert_called_once_with([ob_result])
        table.assert_called_once()
        limiter.record_block.assert_called_once_with(flights.SCAN["block_minutes"])

    def test_non_scan_outbound_request_failed_exits_without_cooldown(self):
        limiter = FakeLimiter()
        args = make_args()
        with patch.object(flights, "parse_args", return_value=args):
            with patch.object(
                flights, "configure_rate_limiter", return_value=limiter
            ):
                with patch.object(flights, "get_rate_limiter", return_value=limiter):
                    with patch.object(
                        flights,
                        "load_config",
                        return_value={"headers": {}, "cookies": {}},
                    ):
                        with patch.object(flights, "validate_config", return_value=True):
                            with patch.object(
                                flights, "archive_stale_cache", return_value=0
                            ):
                                with patch.object(
                                    flights,
                                    "_fetch_with_retries",
                                    side_effect=flights.RequestFailed(500, "boom"),
                                ):
                                    with patch.object(
                                        flights, "parse_results"
                                    ) as parse_results:
                                        with patch.object(
                                            flights, "update_price_map"
                                        ) as update:
                                            with patch.object(
                                                flights, "_log_error"
                                            ) as log_error:
                                                with self.assertRaises(
                                                    SystemExit
                                                ) as ctx:
                                                    flights.main()
        self.assertEqual(ctx.exception.code, 1)
        limiter.record_block.assert_not_called()
        parse_results.assert_not_called()
        update.assert_not_called()
        self.assertIn("boom", str(log_error.call_args[0][0]))

    def test_non_scan_return_request_failed_renders_outbound_partial(self):
        limiter = FakeLimiter()
        args = make_args()
        ob_result = {"marker": "outbound"}
        with patch.object(flights, "parse_args", return_value=args):
            with patch.object(
                flights, "configure_rate_limiter", return_value=limiter
            ):
                with patch.object(flights, "get_rate_limiter", return_value=limiter):
                    with patch.object(
                        flights,
                        "load_config",
                        return_value={"headers": {}, "cookies": {}},
                    ):
                        with patch.object(flights, "validate_config", return_value=True):
                            with patch.object(
                                flights, "archive_stale_cache", return_value=0
                            ):
                                with patch.object(
                                    flights,
                                    "_fetch_with_retries",
                                    side_effect=[
                                        ("ob", 200, None),
                                        flights.RequestFailed(0, "timed out"),
                                    ],
                                ):
                                    with patch.object(
                                        flights, "parse_results", return_value=[ob_result]
                                    ):
                                        with patch.object(
                                            flights,
                                            "filter_dirt_cheap",
                                            side_effect=lambda r: r,
                                        ):
                                            with patch.object(
                                                flights,
                                                "filter_results",
                                                side_effect=lambda r, *a: r,
                                            ):
                                                with patch.object(
                                                    flights, "merge_cabins",
                                                    side_effect=lambda r: r,
                                                ):
                                                    with patch.object(
                                                        flights,
                                                        "print_compare_table",
                                                    ) as table:
                                                        with patch.object(
                                                            flights,
                                                            "_log_error",
                                                        ):
                                                            with self.assertRaises(
                                                                SystemExit
                                                            ) as ctx:
                                                                flights.main()
        self.assertEqual(ctx.exception.code, 1)
        table.assert_called_once()
        limiter.record_block.assert_not_called()


class SourceInvariantTests(unittest.TestCase):
    def test_scan_functions_have_no_live_sleep(self):
        for func in (
            flights._scan_dates,
            flights.drift_scan,
            flights.drift_scan_return,
        ):
            source = inspect.getsource(func)
            self.assertNotIn("time.sleep(", source)
            self.assertNotIn("_is_cache_hit", source)

    def test_cache_hit_helper_removed(self):
        self.assertFalse(hasattr(flights, "_is_cache_hit"))


if __name__ == "__main__":
    unittest.main()
