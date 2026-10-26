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

        with open(self.state_file, "w") as f:
            f.write("{bad json")
        self.limiter.wait()
        self.limiter.record_call()

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
