import json
import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone

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

    def test_jittered_spacing(self):
        self.limiter.wait()
        self.limiter.record_call()
        self.sleep.calls = []
        self.clock.advance(10)
        self.limiter.wait()
        self.limiter.record_call()
        self.assertEqual(self.sleep.calls, [35.0])
        self.sleep.calls = []
        self.clock.advance(45)
        self.limiter.wait()
        self.limiter.record_call()
        self.assertEqual(self.sleep.calls, [])

    def test_cross_run_persisted_spacing(self):
        self.limiter.wait()
        self.limiter.record_call()
        self.clock.advance(10)
        limiter_b = flights.RateLimiter(
            state_file=self.state_file,
            lock_file=self.lock_file,
            base_delay=45,
            budget=None,
            force=False,
            clock=self.clock,
            sleep=self.sleep,
            rng=SequenceRng([45]),
        )
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

    def test_blocked_refusal_and_remaining_time(self):
        blocked_until = self.clock() + timedelta(minutes=20)
        with open(self.state_file, "w") as f:
            json.dump(
                {"live_calls": [], "blocked_until": blocked_until.isoformat()}, f
            )
        with self.assertRaises(flights.RateLimited) as ctx:
            self.limiter.wait()
        self.assertGreater(ctx.exception.remaining_seconds, 0)
        self.assertEqual(self.sleep.calls, [])

    def test_force_bypasses_without_clearing(self):
        blocked_until = self.clock() + timedelta(minutes=20)
        with open(self.state_file, "w") as f:
            json.dump(
                {"live_calls": [], "blocked_until": blocked_until.isoformat()}, f
            )
        limiter_force = flights.RateLimiter(
            state_file=self.state_file,
            lock_file=self.lock_file,
            base_delay=45,
            budget=None,
            force=True,
            clock=self.clock,
            sleep=self.sleep,
            rng=self.rng,
        )
        limiter_force.wait()
        with open(self.state_file) as f:
            state = json.load(f)
        self.assertEqual(state["blocked_until"], blocked_until.isoformat())

    def test_record_block_and_clear_block(self):
        self.limiter.record_block(minutes=20)
        with open(self.state_file) as f:
            state = json.load(f)
        blocked = datetime.fromisoformat(state["blocked_until"])
        self.assertAlmostEqual(
            blocked.timestamp(),
            (self.clock() + timedelta(minutes=20)).timestamp(),
            delta=1,
        )
        self.limiter.clear_block()
        with open(self.state_file) as f:
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

    def test_corrupt_and_missing_state(self):
        missing = flights.RateLimiter(
            state_file=os.path.join(self.tmp.name, "missing.json"),
            lock_file=os.path.join(self.tmp.name, "missing.lock"),
            base_delay=45,
            clock=self.clock,
            sleep=self.sleep,
            rng=SequenceRng([45]),
        )
        missing.wait()
        missing.record_call()
        with open(os.path.join(self.tmp.name, "missing.json")) as f:
            state = json.load(f)
        self.assertIn("live_calls", state)
        self.assertIn("blocked_until", state)

        with open(self.state_file, "w") as f:
            f.write("{bad json")
        with self.assertRaises(flights.RateLimited) as ctx:
            self.limiter.wait()
        self.assertTrue(getattr(ctx.exception, "state_error", False))
        self.assertEqual(self.sleep.calls, [])

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


if __name__ == "__main__":
    unittest.main()


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

    def test_block_written_during_sleep_honored_after_wake(self):
        self.limiter.wait()
        self.limiter.record_call()
        calls = []
        self.clock.advance(10)

        original_sleep = self.sleep

        def sleep_and_block(seconds):
            calls.append(seconds)
            blocked_until = self.clock() + timedelta(minutes=20)
            with open(self.state_file, "w") as f:
                json.dump({"live_calls": [], "blocked_until": blocked_until.isoformat()}, f)
            self.clock.advance(seconds)

        self.limiter.sleep = sleep_and_block
        with self.assertRaises(flights.RateLimited):
            self.limiter.wait()
        self.assertTrue(calls)

    def test_concurrent_call_during_sleep_causes_recompute(self):
        self.limiter.wait()
        self.limiter.record_call()
        calls = []
        self.clock.advance(10)

        other = flights.RateLimiter(
            state_file=self.state_file,
            lock_file=self.lock_file,
            base_delay=45,
            budget=None,
            force=False,
            clock=self.clock,
            sleep=ListSleep(self.clock),
            rng=SequenceRng([45]),
        )

        def sleep_and_call(seconds):
            calls.append(seconds)
            if len(calls) == 1:
                other.record_call()
            self.clock.advance(seconds)

        self.limiter.sleep = sleep_and_call
        self.limiter.wait()
        self.limiter.record_call()
        self.assertEqual(calls, [35.0, 10.0])

    def test_corrupt_cases_fail_closed(self):
        for bad in ["{bad json", json.dumps([]), json.dumps("x")]:
            state_file = os.path.join(self.tmp.name, f"s{hash(bad)}.json")
            lock_file = os.path.join(self.tmp.name, f"s{hash(bad)}.lock")
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

    def test_malformed_blocked_until_fail_closed(self):
        with open(self.state_file, "w") as f:
            json.dump({"live_calls": [], "blocked_until": "not-a-date"}, f)
        with self.assertRaises(flights.RateLimited) as ctx:
            self.limiter.wait()
        self.assertTrue(getattr(ctx.exception, "state_error", False))

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
        self.limiter.record_call()
        self.assertEqual(self.limiter.calls_this_run, 1)

    def test_clear_block_recovers_corrupt_and_preserves_live_calls_when_valid(self):
        with open(self.state_file, "w") as f:
            json.dump({"live_calls": [self.clock().isoformat()], "blocked_until": (self.clock()+timedelta(minutes=10)).isoformat()}, f)
        self.limiter.clear_block()
        with open(self.state_file) as f:
            state = json.load(f)
        self.assertIsNone(state["blocked_until"])
        self.assertTrue(len(state["live_calls"]) >= 0)

        with open(self.state_file, "w") as f:
            f.write("{bad")
        self.limiter.clear_block()
        with open(self.state_file) as f:
            state = json.load(f)
        self.assertIsNone(state["blocked_until"])
        self.assertEqual(state["live_calls"], [])

    def test_clear_block_write_failure_raises_local_state_error(self):
        limiter = flights.RateLimiter(
            state_file=os.path.join(self.tmp.name, "subdir", "state.json"),
            lock_file=self.lock_file,
            base_delay=45,
            clock=self.clock,
            sleep=self.sleep,
            rng=self.rng,
        )
        os.makedirs(os.path.join(self.tmp.name, "subdir"), exist_ok=True)
        with open(os.path.join(self.tmp.name, "subdir", "state.json"), "w") as f:
            f.write("{bad")
        os.chmod(os.path.join(self.tmp.name, "subdir"), 0o555)
        try:
            with self.assertRaises(flights.LocalStateError):
                limiter.clear_block()
        finally:
            os.chmod(os.path.join(self.tmp.name, "subdir"), 0o755)

    def test_record_block_preserves_longer_and_extends_shorter(self):
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

    def test_record_block_read_write_failure_does_not_raise(self):
        limiter = flights.RateLimiter(
            state_file=os.path.join(self.tmp.name, "x", "state.json"),
            lock_file=self.lock_file,
            base_delay=45,
            clock=self.clock,
            sleep=self.sleep,
            rng=self.rng,
        )
        os.makedirs(os.path.join(self.tmp.name, "x"), exist_ok=True)
        with open(os.path.join(self.tmp.name, "x", "state.json"), "w") as f:
            f.write("{bad")
        os.chmod(os.path.join(self.tmp.name, "x"), 0o555)
        try:
            limiter.record_block(minutes=20)  # should not raise
        finally:
            os.chmod(os.path.join(self.tmp.name, "x"), 0o755)

    def test_missing_and_empty_behave_as_first_run(self):
        missing = flights.RateLimiter(
            state_file=os.path.join(self.tmp.name, "missing2.json"),
            lock_file=os.path.join(self.tmp.name, "missing2.lock"),
            base_delay=45,
            clock=self.clock,
            sleep=self.sleep,
            rng=self.rng,
        )
        missing.wait()
        missing.record_call()

        empty = flights.RateLimiter(
            state_file=os.path.join(self.tmp.name, "empty.json"),
            lock_file=os.path.join(self.tmp.name, "empty.lock"),
            base_delay=45,
            clock=self.clock,
            sleep=self.sleep,
            rng=self.rng,
        )
        with open(os.path.join(self.tmp.name, "empty.json"), "w") as f:
            f.write("   ")
        empty.wait()
        empty.record_call()
