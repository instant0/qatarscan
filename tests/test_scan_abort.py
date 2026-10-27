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


class FakeSequenceSession:
    def __init__(self, responses):
        self.cookies = FakeCookieJar()
        self.responses = list(responses)
        self.post_calls = 0

    def post(self, url, headers=None, json=None, timeout=None):
        self.post_calls += 1
        if not self.responses:
            raise AssertionError("unexpected extra POST")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


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

    def test_scans_finalize_once_when_edge_on_third_call(self):
        edge = flights.EdgeBlock("block", status=403, reference="18.abc.123")
        cases = [
            (
                "scan_dates",
                lambda: flights._scan_dates(
                    {},
                    {},
                    make_args(ob=2, rb=None, min_trip=0, nofilter=True),
                    {},
                    "price",
                    25,
                    5,
                    True,
                    "OB",
                ),
            ),
            (
                "drift_scan",
                lambda: flights.drift_scan(
                    {}, {}, make_args(drift=1, nofilter=True), {}, 25
                ),
            ),
            (
                "drift_scan_return",
                lambda: flights.drift_scan_return(
                    {},
                    {},
                    make_args(drift_ret=1, min_trip=0, nofilter=True),
                    {},
                    25,
                ),
            ),
        ]
        for name, run_scan in cases:
            with self.subTest(scan=name):
                fake_fetch, state = make_abort_scan(2, edge)
                with patch.object(
                    flights, "_fetch_and_parse", side_effect=fake_fetch
                ):
                    with patch.object(flights, "update_price_map") as update:
                        with self.assertRaises(flights.ScanAborted) as ctx:
                            run_scan()
                aborted = ctx.exception
                self.assertEqual(state["count"], 3)
                self.assertEqual(aborted.reason, "edge")
                self.assertEqual(aborted.results, [{"marker": 1}, {"marker": 2}])
                update.assert_called_once_with([{"marker": 1}, {"marker": 2}])

    def test_abort_reason_and_cooldown_recording(self):
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

        block_cases = [
            (flights.EdgeBlock("block"), True),
            (flights.AppAuthError("auth"), False),
            (flights.RateLimited(60, "2026-10-26T12:00:00+00:00"), False),
            (flights.BudgetExceeded(1), False),
        ]
        for exc, expected in block_cases:
            with self.subTest(block=type(exc).__name__):
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
    def test_edge_message_variants(self):
        for reference, expected, absent in (
            ("18.abc.123", ("Akamai edge", "18.abc.123", "--paste"), ()),
            (None, ("Akamai edge", "--paste"), ("None",)),
        ):
            with self.subTest(reference=reference):
                cause = flights.EdgeBlock("block", status=403, reference=reference)
                message = flights._abort_message(flights.ScanAborted([], "edge", cause))
                for text in expected:
                    self.assertIn(text, message)
                for text in absent:
                    self.assertNotIn(text, message)

    def test_non_edge_messages(self):
        cases = [
            (
                "auth",
                flights.ScanAborted([], "auth", flights.AppAuthError("bad")),
                ("Session rejected", "--paste"),
                (),
            ),
            (
                "rate_remaining",
                flights.ScanAborted(
                    [],
                    "rate",
                    flights.RateLimited(125, "2026-10-26T12:10:00+00:00"),
                ),
                ("about 3 min remaining", "--force", "--clear-block"),
                (),
            ),
            (
                "rate_state_error",
                flights.ScanAborted(
                    [],
                    "rate",
                    flights.RateLimited(
                        1200, "2026-10-26T12:20:00+00:00", state_error=True
                    ),
                ),
                ("corrupt", "--clear-block"),
                ("--force",),
            ),
            (
                "budget",
                flights.ScanAborted([], "budget", flights.BudgetExceeded(7)),
                ("(7 calls)", "--budget"),
                (),
            ),
        ]
        for name, aborted, expected, absent in cases:
            with self.subTest(case=name):
                message = flights._abort_message(aborted)
                for text in expected:
                    self.assertIn(text, message)
                for text in absent:
                    self.assertNotIn(text, message)

        with self.subTest(case="fallback"):
            message = flights._abort_message(flights.ScanAborted([], "unknown"))
            self.assertEqual(message, "Scan aborted.")


class RetryTests(unittest.TestCase):
    def setUp(self):
        self.old_max_retries = flights.SCAN["max_retries"]

    def tearDown(self):
        flights.SCAN["max_retries"] = self.old_max_retries

    def test_retry_policy_for_transient_and_non_retryable_errors(self):
        cases = [
            (
                "transient_retried",
                1,
                [flights.RequestFailed(500, "boom"), ("data", 200, None)],
                ("data", 200, None),
                2,
            ),
            (
                "default_no_retry",
                0,
                flights.RequestFailed(500, "boom"),
                flights.RequestFailed,
                1,
            ),
            (
                "non_retryable_4xx",
                1,
                flights.RequestFailed(404, "nope"),
                flights.RequestFailed,
                1,
            ),
        ]
        for name, max_retries, side_effect, expected, calls in cases:
            with self.subTest(case=name):
                flights.SCAN["max_retries"] = max_retries
                with patch.object(
                    flights, "fetch_flights", side_effect=side_effect
                ) as fetch:
                    if isinstance(expected, type) and issubclass(expected, Exception):
                        with self.assertRaises(expected):
                            flights._fetch_with_retries(
                                {}, {}, "OSL", "BKK", "2026-12-29"
                            )
                    else:
                        result = flights._fetch_with_retries(
                            {}, {}, "OSL", "BKK", "2026-12-29"
                        )
                        self.assertEqual(result, expected)
                self.assertEqual(fetch.call_count, calls)

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

    def test_fetch_and_parse_error_handling(self):
        with self.subTest(case="transient_failure_keeps_scan_going"):
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

        with self.subTest(case="edge_propagates"):
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

    def test_html_error_summaries_are_sanitized(self):
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


class RetryLimiterAccountingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_cache_file = flights.OFFERS_CACHE_FILE
        self.old_lock_file = flights.DATA_LOCK_FILE
        self.old_max_retries = flights.SCAN["max_retries"]
        flights.OFFERS_CACHE_FILE = os.path.join(self.tmp.name, "offers_cache.json")
        flights.DATA_LOCK_FILE = os.path.join(self.tmp.name, ".data_state.lock")
        self.addCleanup(setattr, flights, "OFFERS_CACHE_FILE", self.old_cache_file)
        self.addCleanup(setattr, flights, "DATA_LOCK_FILE", self.old_lock_file)
        self.addCleanup(
            lambda: flights.SCAN.__setitem__("max_retries", self.old_max_retries)
        )
        flights.set_nocache(False)
        self.addCleanup(flights.set_nocache, False)
        persist_patcher = patch.object(flights, "_persist_cookies")
        persist_patcher.start()
        self.addCleanup(persist_patcher.stop)

    def _run(self, session, limiter):
        with patch.object(flights.requests, "Session", return_value=session):
            with patch.object(flights, "get_rate_limiter", return_value=limiter):
                return flights._fetch_with_retries(
                    {}, {}, "OSL", "BKK", "2026-12-29"
                )

    def test_retry_limiter_accounting(self):
        with self.subTest(case="transient_retry"):
            flights.SCAN["max_retries"] = 1
            limiter = FakeLimiter()
            session = FakeSequenceSession(
                [
                    FakeResponse(500, "boom"),
                    FakeResponse(200, '{"flightOffers": []}'),
                ]
            )
            result = self._run(session, limiter)
            self.assertEqual(result, ({"flightOffers": []}, 200, None))
            self.assertEqual(session.post_calls, 2)
            self.assertEqual(limiter.wait.call_count, 2)
            self.assertEqual(limiter.record_call.call_count, 2)
            limiter.record_block.assert_not_called()

        response_cases = [
            (
                "edge",
                FakeResponse(
                    403,
                    "<html>Access Denied Reference #18.abc.123 "
                    "errors.edgesuite.net</html>",
                ),
                flights.EdgeBlock,
            ),
            (
                "auth",
                FakeResponse(403, '{"error":"session"}'),
                flights.AppAuthError,
            ),
        ]
        for name, response, error in response_cases:
            with self.subTest(case=name):
                if os.path.exists(flights.OFFERS_CACHE_FILE):
                    os.remove(flights.OFFERS_CACHE_FILE)
                flights.SCAN["max_retries"] = 1
                limiter = FakeLimiter()
                session = FakeSequenceSession([response])
                with self.assertRaises(error):
                    self._run(session, limiter)
                self.assertEqual(session.post_calls, 1)
                self.assertEqual(limiter.wait.call_count, 1)
                self.assertEqual(limiter.record_call.call_count, 1)
                limiter.record_block.assert_not_called()

        limiter_cases = [
            ("rate", flights.RateLimited(60, "2026-10-26T12:00:00+00:00")),
            ("budget", flights.BudgetExceeded(1)),
        ]
        for name, error in limiter_cases:
            with self.subTest(case=name):
                if os.path.exists(flights.OFFERS_CACHE_FILE):
                    os.remove(flights.OFFERS_CACHE_FILE)
                flights.SCAN["max_retries"] = 1
                limiter = FakeLimiter()
                limiter.wait.side_effect = error
                session = FakeSequenceSession([])
                with self.assertRaises(type(error)):
                    self._run(session, limiter)
                self.assertEqual(session.post_calls, 0)
                self.assertEqual(limiter.wait.call_count, 1)
                limiter.record_call.assert_not_called()
                limiter.record_block.assert_not_called()


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

    def test_non_scan_return_failures_render_outbound_partial(self):
        cases = [
            ("edge", flights.EdgeBlock("block", 403, "18.abc.123"), True),
            ("request_failed", flights.RequestFailed(0, "timed out"), False),
        ]
        for name, failure, records_block in cases:
            with self.subTest(case=name):
                limiter = FakeLimiter()
                args = make_args()
                ob_result = {"marker": "outbound"}
                with patch.object(flights, "parse_args", return_value=args):
                    with patch.object(
                        flights, "configure_rate_limiter", return_value=limiter
                    ):
                        with patch.object(
                            flights, "get_rate_limiter", return_value=limiter
                        ):
                            with patch.object(
                                flights,
                                "load_config",
                                return_value={"headers": {}, "cookies": {}},
                            ):
                                with patch.object(
                                    flights, "validate_config", return_value=True
                                ):
                                    with patch.object(
                                        flights, "archive_stale_cache", return_value=0
                                    ):
                                        with patch.object(
                                            flights,
                                            "_fetch_with_retries",
                                            side_effect=[
                                                ("ob", 200, None),
                                                failure,
                                            ],
                                        ):
                                            with patch.object(
                                                flights,
                                                "parse_results",
                                                return_value=[ob_result],
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
                                                                        "update_price_map",
                                                                    ) as update:
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
                if records_block:
                    limiter.record_block.assert_called_once_with(
                        flights.SCAN["block_minutes"]
                    )
                else:
                    limiter.record_block.assert_not_called()


class SourceInvariantTests(unittest.TestCase):
    def test_scan_functions_have_no_live_sleep_and_cache_helper_removed(self):
        for func in (
            flights._scan_dates,
            flights.drift_scan,
            flights.drift_scan_return,
        ):
            with self.subTest(func=func.__name__):
                source = inspect.getsource(func)
                self.assertNotIn("time.sleep(", source)
                self.assertNotIn("_is_cache_hit", source)
        self.assertFalse(hasattr(flights, "_is_cache_hit"))


if __name__ == "__main__":
    unittest.main()
