import json
import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import flights


class FakeClock:
    def __init__(self, start=None):
        self.now = start or datetime(2026, 10, 26, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now = self.now + timedelta(seconds=seconds)


class ListSleep:
    def __init__(self, clock=None):
        self.calls = []
        self.clock = clock

    def __call__(self, seconds):
        self.calls.append(seconds)
        if self.clock is not None:
            self.clock.advance(seconds)


class SequenceRng:
    def __init__(self, values):
        self.values = list(values)

    def randint(self, low, high):
        if self.values:
            value = self.values.pop(0)
            if value < low:
                return low
            if value > high:
                return high
            return value
        return low


class RateLimiterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_file = os.path.join(self.tmp.name, ".rate_state.json")
        self.lock_file = os.path.join(self.tmp.name, ".rate_state.lock")
        self.clock = FakeClock()
        self.sleep = ListSleep(self.clock)
        self.rng = SequenceRng([45, 45])
        self.limiter = flights.RateLimiter(
            state_file=self.state_file,
            lock_file=self.lock_file,
            base_delay=45,
            budget=None,
            force=False,
            clock=self.clock,
            sleep=self.sleep,
            rng=self.rng,
        )

    def test_jittered_and_cross_run_spacing(self):
        with self.subTest(kind="jitter"):
            limiter = flights.RateLimiter(
                state_file=os.path.join(self.tmp.name, "jitter.json"),
                lock_file=os.path.join(self.tmp.name, "jitter.lock"),
                base_delay=45,
                budget=None,
                force=False,
                clock=self.clock,
                sleep=self.sleep,
                rng=SequenceRng([45, 45]),
            )
            limiter.wait()
            limiter.record_call()
            self.sleep.calls = []
            self.clock.advance(10)
            limiter.wait()
            limiter.record_call()
            self.assertEqual(self.sleep.calls, [35.0])
            self.sleep.calls = []
            self.clock.advance(45)
            limiter.wait()
            limiter.record_call()
            self.assertEqual(self.sleep.calls, [])
        with self.subTest(kind="cross-run"):
            limiter_a = flights.RateLimiter(
                state_file=os.path.join(self.tmp.name, "cross.json"),
                lock_file=os.path.join(self.tmp.name, "cross.lock"),
                base_delay=45,
                budget=None,
                force=False,
                clock=self.clock,
                sleep=self.sleep,
                rng=SequenceRng([45, 45]),
            )
            limiter_a.wait()
            limiter_a.record_call()
            self.clock.advance(10)
            limiter_b = flights.RateLimiter(
                state_file=os.path.join(self.tmp.name, "cross.json"),
                lock_file=os.path.join(self.tmp.name, "cross.lock"),
                base_delay=45,
                budget=None,
                force=False,
                clock=self.clock,
                sleep=self.sleep,
                rng=SequenceRng([45]),
            )
            self.sleep.calls = []
            limiter_b.wait()
            self.assertEqual(self.sleep.calls, [35.0])
            limiter_b.record_call()

    def test_prunes_entries_older_than_24h(self):
        old = self.clock() - timedelta(hours=25)
        recent = self.clock() - timedelta(hours=1)
        with open(self.state_file, "w") as f:
            json.dump(
                {"live_calls": [old.isoformat(), recent.isoformat()], "blocked_until": None},
                f,
            )
        self.limiter.record_call()
        with open(self.state_file) as f:
            state = json.load(f)
        self.assertEqual(len(state["live_calls"]), 2)
        self.assertIn(recent.isoformat(), state["live_calls"][0])

    def test_block_force_and_clear(self):
        with self.subTest(kind="blocked-refusal"):
            state_file = os.path.join(self.tmp.name, "blocked.json")
            blocked_until = self.clock() + timedelta(minutes=20)
            with open(state_file, "w") as f:
                json.dump(
                    {"live_calls": [], "blocked_until": blocked_until.isoformat()},
                    f,
                )
            sleep = ListSleep(self.clock)
            limiter = flights.RateLimiter(
                state_file=state_file,
                lock_file=os.path.join(self.tmp.name, "blocked.lock"),
                base_delay=45,
                budget=None,
                force=False,
                clock=self.clock,
                sleep=sleep,
                rng=SequenceRng([45, 45]),
            )
            with self.assertRaises(flights.RateLimited) as ctx:
                limiter.wait()
            self.assertGreater(ctx.exception.remaining_seconds, 0)
            self.assertEqual(sleep.calls, [])
        with self.subTest(kind="force-bypass"):
            state_file = os.path.join(self.tmp.name, "force.json")
            blocked_until = self.clock() + timedelta(minutes=20)
            with open(state_file, "w") as f:
                json.dump(
                    {"live_calls": [], "blocked_until": blocked_until.isoformat()},
                    f,
                )
            limiter_force = flights.RateLimiter(
                state_file=state_file,
                lock_file=os.path.join(self.tmp.name, "force.lock"),
                base_delay=45,
                budget=None,
                force=True,
                clock=self.clock,
                sleep=ListSleep(self.clock),
                rng=SequenceRng([45, 45]),
            )
            limiter_force.wait()
            with open(state_file) as f:
                state = json.load(f)
            self.assertEqual(state["blocked_until"], blocked_until.isoformat())
        with self.subTest(kind="record-and-clear"):
            state_file = os.path.join(self.tmp.name, "record.json")
            limiter = flights.RateLimiter(
                state_file=state_file,
                lock_file=os.path.join(self.tmp.name, "record.lock"),
                base_delay=45,
                budget=None,
                force=False,
                clock=self.clock,
                sleep=ListSleep(self.clock),
                rng=SequenceRng([45, 45]),
            )
            limiter.record_block(minutes=20)
            with open(state_file) as f:
                state = json.load(f)
            blocked = datetime.fromisoformat(state["blocked_until"])
            self.assertAlmostEqual(
                blocked.timestamp(),
                (self.clock() + timedelta(minutes=20)).timestamp(),
                delta=1,
            )
            limiter.clear_block()
            with open(state_file) as f:
                state = json.load(f)
            self.assertIsNone(state["blocked_until"])

    def test_budget_exhaustion(self):
        limiter_budget = flights.RateLimiter(
            state_file=self.state_file,
            lock_file=self.lock_file,
            base_delay=45,
            budget=1,
            force=False,
            clock=self.clock,
            sleep=self.sleep,
            rng=SequenceRng([45, 45]),
        )
        limiter_budget.wait()
        limiter_budget.record_call()
        with self.assertRaises(flights.BudgetExceeded):
            limiter_budget.wait()

    def test_lock_serialized_concurrent_writers(self):
        errors = []
        limiters = []

        def writer(idx):
            try:
                limiter = flights.RateLimiter(
                    state_file=self.state_file,
                    lock_file=self.lock_file,
                    base_delay=45,
                    budget=None,
                    force=False,
                    clock=lambda: self.clock() + timedelta(seconds=idx),
                    sleep=self.sleep,
                    rng=SequenceRng([45]),
                )
                limiters.append(limiter)
                for _ in range(5):
                    limiter.record_call()
                    time.sleep(0.01)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        with open(self.state_file) as f:
            state = json.load(f)
        self.assertIsInstance(state, dict)
        self.assertIn("live_calls", state)
        self.assertEqual(len(state["live_calls"]), 20)


class RateLimiterHardeningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_file = os.path.join(self.tmp.name, ".rate_state.json")
        self.lock_file = os.path.join(self.tmp.name, ".rate_state.lock")
        self.clock = FakeClock()
        self.sleep = ListSleep(self.clock)
        self.rng = SequenceRng([45, 45])
        self.limiter = flights.RateLimiter(
            state_file=self.state_file,
            lock_file=self.lock_file,
            base_delay=45,
            budget=None,
            force=False,
            clock=self.clock,
            sleep=self.sleep,
            rng=self.rng,
        )

    def test_sleep_rechecks_block_and_concurrent_calls(self):
        with self.subTest(kind="block-honored-after-sleep"):
            clock = FakeClock()
            state_file = os.path.join(self.tmp.name, "sleepblock.json")
            sleep = ListSleep(clock)
            limiter = flights.RateLimiter(
                state_file=state_file,
                lock_file=os.path.join(self.tmp.name, "sleepblock.lock"),
                base_delay=45,
                budget=None,
                force=False,
                clock=clock,
                sleep=sleep,
                rng=SequenceRng([45, 45]),
            )
            limiter.wait()
            limiter.record_call()
            calls = []
            clock.advance(10)

            def sleep_and_block(seconds):
                calls.append(seconds)
                blocked_until = clock() + timedelta(minutes=20)
                with open(state_file, "w") as f:
                    json.dump(
                        {
                            "live_calls": [],
                            "blocked_until": blocked_until.isoformat(),
                        },
                        f,
                    )
                clock.advance(seconds)

            limiter.sleep = sleep_and_block
            with self.assertRaises(flights.RateLimited):
                limiter.wait()
            self.assertTrue(calls)
        with self.subTest(kind="concurrent-call-recompute"):
            clock = FakeClock()
            state_file = os.path.join(self.tmp.name, "sleepcall.json")
            sleep = ListSleep(clock)
            limiter = flights.RateLimiter(
                state_file=state_file,
                lock_file=os.path.join(self.tmp.name, "sleepcall.lock"),
                base_delay=45,
                budget=None,
                force=False,
                clock=clock,
                sleep=sleep,
                rng=SequenceRng([45, 45]),
            )
            limiter.wait()
            limiter.record_call()
            calls = []
            clock.advance(10)
            other = flights.RateLimiter(
                state_file=state_file,
                lock_file=os.path.join(self.tmp.name, "sleepcall.lock"),
                base_delay=45,
                budget=None,
                force=False,
                clock=clock,
                sleep=ListSleep(clock),
                rng=SequenceRng([45]),
            )

            def sleep_and_call(seconds):
                calls.append(seconds)
                if len(calls) == 1:
                    other.record_call()
                clock.advance(seconds)

            limiter.sleep = sleep_and_call
            limiter.wait()
            limiter.record_call()
            self.assertEqual(calls, [35.0, 10.0])
        with self.subTest(kind="force-ignores-block-after-sleep"):
            clock = FakeClock()
            state_file = os.path.join(self.tmp.name, "sleepforce.json")
            sleep = ListSleep(clock)
            limiter = flights.RateLimiter(
                state_file=state_file,
                lock_file=os.path.join(self.tmp.name, "sleepforce.lock"),
                base_delay=45,
                budget=None,
                force=True,
                clock=clock,
                sleep=sleep,
                rng=SequenceRng([45]),
            )
            limiter.wait()
            limiter.record_call()
            calls = []
            clock.advance(10)

            def sleep_and_block(seconds):
                calls.append(seconds)
                blocked_until = clock() + timedelta(minutes=20)
                with open(state_file, "w") as f:
                    json.dump(
                        {
                            "live_calls": [],
                            "blocked_until": blocked_until.isoformat(),
                        },
                        f,
                    )
                clock.advance(seconds)

            limiter.sleep = sleep_and_block
            limiter.wait()
            self.assertEqual(calls, [35.0])

    def test_missing_empty_and_corrupt_state_fail_closed(self):
        for name in ("missing", "empty"):
            with self.subTest(state=name):
                self.sleep.calls = []
                state_file = os.path.join(self.tmp.name, f"{name}.json")
                lock_file = os.path.join(self.tmp.name, f"{name}.lock")
                if name == "empty":
                    with open(state_file, "w") as f:
                        f.write("   ")
                limiter = flights.RateLimiter(
                    state_file=state_file,
                    lock_file=lock_file,
                    base_delay=45,
                    clock=self.clock,
                    sleep=self.sleep,
                    rng=SequenceRng([45]),
                )
                limiter.wait()
                limiter.record_call()
                with open(state_file) as f:
                    state = json.load(f)
                self.assertIn("live_calls", state)
                self.assertIn("blocked_until", state)
                self.assertEqual(len(state["live_calls"]), 1)
        bad_states = ["{bad json", json.dumps([]), json.dumps("x")]
        for idx, bad in enumerate(bad_states):
            with self.subTest(state=f"corrupt-{idx}"):
                self.sleep.calls = []
                state_file = os.path.join(self.tmp.name, f"corrupt{idx}.json")
                lock_file = os.path.join(self.tmp.name, f"corrupt{idx}.lock")
                limiter = flights.RateLimiter(
                    state_file=state_file,
                    lock_file=lock_file,
                    base_delay=45,
                    force=False,
                    clock=self.clock,
                    sleep=self.sleep,
                    rng=self.rng,
                )
                with open(state_file, "w") as f:
                    f.write(bad)
                with self.assertRaises(flights.RateLimited) as ctx:
                    limiter.wait()
                self.assertTrue(getattr(ctx.exception, "state_error", False))
                self.assertEqual(self.sleep.calls, [])
        with self.subTest(state="malformed-blocked-until"):
            self.sleep.calls = []
            with open(self.state_file, "w") as f:
                json.dump({"live_calls": [], "blocked_until": "not-a-date"}, f)
            with self.assertRaises(flights.RateLimited) as ctx:
                self.limiter.wait()
            self.assertTrue(getattr(ctx.exception, "state_error", False))

    def test_unreadable_state_raises_rate_state_error(self):
        with patch("builtins.open", side_effect=PermissionError("denied")):
            with self.assertRaises(flights.RateStateError) as ctx:
                self.limiter._read_state()
        self.assertIn("--clear-block", str(ctx.exception))

    def test_force_does_not_bypass_corrupt_state(self):
        with open(self.state_file, "w") as f:
            f.write("{bad")
        limiter_force = flights.RateLimiter(
            state_file=self.state_file,
            lock_file=self.lock_file,
            base_delay=45,
            force=True,
            clock=self.clock,
            sleep=self.sleep,
            rng=self.rng,
        )
        with self.assertRaises(flights.RateLimited) as ctx:
            limiter_force.wait()
        self.assertTrue(getattr(ctx.exception, "state_error", False))

    def test_is_blocked_returns_true_on_corrupt_state_without_raising(self):
        with open(self.state_file, "w") as f:
            f.write("{bad")
        self.assertTrue(self.limiter.is_blocked())
        self.assertGreater(self.limiter.blocked_remaining(), 0)

    def test_record_call_on_corrupt_state_warns_and_does_not_raise(self):
        with open(self.state_file, "w") as f:
            f.write("{bad")
        with patch.object(flights.log, "print") as log_print:
            self.limiter.record_call()
        self.assertEqual(self.limiter.calls_this_run, 1)
        output = " ".join(
            str(arg) for call in log_print.call_args_list for arg in call.args
        )
        self.assertIn("corrupt", output)

    def test_clear_block_recovers_corrupt_and_preserves_live_calls_when_valid(self):
        live = self.clock().isoformat()
        with open(self.state_file, "w") as f:
            json.dump(
                {
                    "live_calls": [live],
                    "blocked_until": (
                        self.clock() + timedelta(minutes=10)
                    ).isoformat(),
                },
                f,
            )
        self.limiter.clear_block()
        with open(self.state_file) as f:
            state = json.load(f)
        self.assertIsNone(state["blocked_until"])
        self.assertEqual(state["live_calls"], [live])

        with open(self.state_file, "w") as f:
            f.write("{bad")
        self.limiter.clear_block()
        with open(self.state_file) as f:
            state = json.load(f)
        self.assertIsNone(state["blocked_until"])
        self.assertEqual(state["live_calls"], [])

    def test_clear_block_write_failure_raises_local_state_error(self):
        with patch.object(
            self.limiter, "_write_state", side_effect=OSError("disk full")
        ):
            with self.assertRaises(flights.LocalStateError):
                self.limiter.clear_block()

    def test_record_block_merge_and_failures(self):
        with self.subTest(kind="merge-longest"):
            self.limiter.record_block(minutes=5)
            with open(self.state_file) as f:
                s1 = json.load(f)
            b1 = datetime.fromisoformat(s1["blocked_until"])
            self.limiter.record_block(minutes=20)
            with open(self.state_file) as f:
                s2 = json.load(f)
            b2 = datetime.fromisoformat(s2["blocked_until"])
            self.assertGreaterEqual(b2, b1)
            self.limiter.record_block(minutes=10)
            with open(self.state_file) as f:
                s3 = json.load(f)
            b3 = datetime.fromisoformat(s3["blocked_until"])
            self.assertGreaterEqual(b3, b2)
        with self.subTest(kind="write-failure"):
            with open(self.state_file, "w") as f:
                json.dump({"live_calls": [], "blocked_until": None}, f)
            with patch.object(
                self.limiter, "_write_state", side_effect=OSError("disk full")
            ):
                with patch.object(flights.log, "print") as log_print:
                    self.limiter.record_block(minutes=20)
            output = " ".join(
                str(arg) for call in log_print.call_args_list for arg in call.args
            )
            self.assertIn("disk full", output)
        with self.subTest(kind="read-failure"):
            with open(self.state_file, "w") as f:
                f.write("{bad")
            with patch.object(flights.log, "print") as log_print:
                self.limiter.record_block(minutes=20)
            output = " ".join(
                str(arg) for call in log_print.call_args_list for arg in call.args
            )
            self.assertIn("Could not record", output)

    def test_main_clear_block_failure_exits_without_success(self):
        args = flights.parse_args([])
        args.clear_block = True
        limiter = MagicMock()
        limiter.clear_block.side_effect = flights.LocalStateError("cannot write")
        with patch.object(flights, "parse_args", return_value=args):
            with patch.object(
                flights, "configure_rate_limiter", return_value=limiter
            ):
                with patch.object(flights, "get_rate_limiter", return_value=limiter):
                    with patch.object(flights.log, "print") as log_print:
                        with self.assertRaises(SystemExit) as ctx:
                            flights.main()
        self.assertEqual(ctx.exception.code, 1)
        output = " ".join(
            str(arg) for call in log_print.call_args_list for arg in call.args
        )
        self.assertIn("cannot write", output)
        self.assertNotIn("cleared", output.lower())


if __name__ == "__main__":
    unittest.main()
