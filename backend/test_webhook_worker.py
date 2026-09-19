"""The webhook must answer at once and hand events to the car logic in order."""
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import webhook
from timing import CallStats, Timed


class WebhookWorkerTests(unittest.TestCase):
    def setUp(self):
        webhook._handlers.clear()
        webhook._background.clear()
        self.client = webhook.app.test_client()

    def test_slow_background_handler_does_not_slow_the_reply(self):
        gate = threading.Event()
        seen = []
        webhook.register_handler(lambda e: (gate.wait(3), seen.append(e["n"])), background=True)
        start = time.monotonic()
        response = self.client.post("/webhook", json={"n": 1})
        self.assertEqual(response.status_code, 200)
        self.assertLess(time.monotonic() - start, 1.0)
        gate.set()
        webhook._queue.join()
        self.assertEqual(seen, [1])

    def test_background_events_are_handled_in_arrival_order(self):
        seen = []
        webhook.register_handler(lambda e: (time.sleep(0.01), seen.append(e["n"])),
                                 background=True)
        for n in range(20):
            self.client.post("/webhook", json={"n": n})
        webhook._queue.join()
        self.assertEqual(seen, list(range(20)))

    def test_inline_handlers_still_run_immediately(self):
        seen = []
        webhook.register_handler(lambda e: seen.append(e["n"]))
        self.client.post("/webhook", json={"n": 7})
        self.assertEqual(seen, [7])

    def test_a_failing_handler_does_not_stop_the_others(self):
        seen = []
        def boom(e):
            raise RuntimeError("bad")
        webhook.register_handler(boom, background=True)
        webhook.register_handler(lambda e: seen.append(e["n"]), background=True)
        self.client.post("/webhook", json={"n": 3})
        webhook._queue.join()
        self.assertEqual(seen, [3])

    def test_invalid_json_is_refused(self):
        self.assertEqual(self.client.post("/webhook", data="nope").status_code, 400)

    def test_backlog_counts_waiting_events(self):
        gate = threading.Event()
        webhook.register_handler(lambda e: gate.wait(3), background=True)
        for n in range(4):
            self.client.post("/webhook", json={"n": n})
        time.sleep(0.05)
        self.assertGreaterEqual(webhook.backlog(), 3)
        gate.set()
        webhook._queue.join()
        self.assertEqual(webhook.backlog(), 0)


class LocalhostTests(unittest.TestCase):
    """Windows spends ~2 s per call on "localhost" (IPv6 first); 127.0.0.1 has no delay."""

    def test_simulator_client_uses_ipv4(self):
        from simulator_api import SimulatorClient
        self.assertEqual(SimulatorClient(base_url="http://localhost:9898/").base_url,
                         "http://127.0.0.1:9898")
        self.assertEqual(SimulatorClient(base_url="http://10.0.0.5:9898").base_url,
                         "http://10.0.0.5:9898")


class TimingTests(unittest.TestCase):
    def test_calls_are_timed_and_results_pass_through(self):
        class Thing:
            value = 5
            def slow(self, x):
                time.sleep(0.02)
                return x * 2
        stats = CallStats()
        thing = Timed(Thing(), "sim", stats)
        self.assertEqual(thing.slow(4), 8)
        self.assertEqual(thing.value, 5)
        summary = stats.summary()["sim.slow"]
        self.assertEqual(summary["n"], 1)
        self.assertGreaterEqual(summary["max_s"], 0.02)

    def test_a_failing_call_is_still_timed_and_still_raises(self):
        class Thing:
            def bad(self):
                raise ValueError("x")
        stats = CallStats()
        with self.assertRaises(ValueError):
            Timed(Thing(), "sim", stats).bad()
        self.assertEqual(stats.summary()["sim.bad"]["n"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
