"""Tests for poller.py and the ingest wiring (webhook -> store, poller -> store)."""
import os
import sqlite3
import tempfile
import time
import unittest

import ingest
import webhook
from poller import DEFAULT_INTERVALS, Poller, intervals_from_env
from store import Store


class FakeClock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class FakeClient:
    """Answers like the real simulator; can be told to fail."""

    def __init__(self):
        self.calls = []
        self.fail = set()
        self.answers = {
            "list-parking-spots": [{"name": "S1", "purpose": "Park", "parkingForCarType": "Any",
                                    "zoneParent": "ZONE1", "detectedCars": 0, "broken": False,
                                    "isUnderMaintenance": False}],
            "list-barriers": [{"name": "gateA", "zoneParent": "ZONE1", "broken": False,
                               "isUnderMaintenance": False, "state": "Closed"}],
            "list-zones": [{"name": "ZONE1", "gasCarbonMonoxideLevel": 0, "risk": "Safe"}],
            "list-lights": [{"name": "t_0", "group": "G1", "zoneParent": "ZONE1", "isOn": True}],
            "list-alarms": [],
            "status": {"isActive": False, "cars": 5},
        }

    def call(self, method, path, **kwargs):
        endpoint = path.rsplit("/", 1)[1]
        self.calls.append((method, endpoint))
        if endpoint in self.fail:
            raise ConnectionError("simulator down")
        return self.answers[endpoint]


class PollerTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "t.db")
        self.store = Store(self.path).start()
        self.client = FakeClient()
        self.clock = FakeClock()
        self.poller = Poller(self.client, self.store, clock=self.clock)
        self.addCleanup(self.dir.cleanup)
        self.addCleanup(self.store.stop)

    def q(self, sql, *args):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]
        finally:
            conn.close()


class TestPoller(PollerTestCase):
    def test_first_round_reads_everything_once(self):
        done = self.poller.run_due()
        self.assertEqual(set(done), set(DEFAULT_INTERVALS))
        self.assertTrue(all(m == "GET" for m, _ in self.client.calls))
        self.assertTrue(self.store.flush())
        self.assertEqual(self.q("SELECT name FROM list_parking_spots")[0]["name"], "S1")
        self.assertEqual(self.q("SELECT state FROM list_barriers")[0]["state"], "Closed")
        self.assertEqual(self.q("SELECT cars FROM status")[0]["cars"], 5)

    def test_each_list_waits_its_own_interval(self):
        self.poller.run_due()
        self.client.calls.clear()
        self.clock.t += 0.5
        self.assertEqual(self.poller.run_due(), [])                 # nothing due yet
        self.clock.t += 0.6                                         # 1.1 s: spots + barriers due
        self.assertEqual(set(self.poller.run_due()), {"list-parking-spots", "list-barriers"})
        self.clock.t += 1.0                                         # 2.1 s: zones due as well
        self.assertIn("list-zones", self.poller.run_due())
        self.clock.t += 2.0                                         # 4.1 s: slow lists still waiting
        self.assertNotIn("status", self.poller.run_due())
        self.clock.t += 1.0                                         # 5.1 s: now they are due
        self.assertIn("status", self.poller.run_due())

    def test_wait_starts_after_the_read_finishes(self):
        class SlowClient(FakeClient):
            def call(inner, method, path, **kw):
                self.clock.t += 0.8                                   # each read takes 0.8 s
                return FakeClient.call(inner, method, path, **kw)

        poller = Poller(SlowClient(), self.store, {"list-parking-spots": 1.0}, clock=self.clock)
        poller.intervals = {"list-parking-spots": 1.0}
        poller._next_due = {"list-parking-spots": 0.0}
        poller._failures = {"list-parking-spots": 0}
        poller.run_due()
        finished = self.clock.t
        self.assertAlmostEqual(poller._next_due["list-parking-spots"], finished + 1.0)

    def test_a_failure_is_recorded_and_backs_off(self):
        self.client.fail.add("list-zones")
        self.poller.run_due()
        self.assertTrue(self.store.flush())
        row = self.q("SELECT * FROM poll_status WHERE endpoint='list-zones'")[0]
        self.assertEqual(row["error_count"], 1)
        self.assertIn("simulator down", row["last_error"])
        first = self.poller._next_due["list-zones"] - self.clock.t
        self.clock.t = self.poller._next_due["list-zones"]
        self.poller.run_due()
        second = self.poller._next_due["list-zones"] - self.clock.t
        self.assertGreater(second, first)                            # backing off
        for _ in range(10):
            self.clock.t = self.poller._next_due["list-zones"]
            self.poller.run_due()
        self.assertLessEqual(self.poller._next_due["list-zones"] - self.clock.t, 5.0)

    def test_backoff_never_overflows_however_long_the_simulator_is_down(self):
        self.poller._failures["list-zones"] = 5000
        self.assertEqual(self.poller._delay_after("list-zones", ok=False), 5.0)

    def test_an_endpoint_always_gets_a_next_turn_even_if_something_raises(self):
        self.poller.poll_once = lambda endpoint: (_ for _ in ()).throw(RuntimeError("boom"))
        with self.assertRaises(RuntimeError):
            self.poller.run_due()
        self.assertGreater(self.poller._next_due["list-parking-spots"], self.clock.t)

    def test_the_other_lists_keep_working_when_one_fails(self):
        self.client.fail.add("list-parking-spots")
        self.poller.run_due()
        self.assertTrue(self.store.flush())
        self.assertEqual(self.q("SELECT state FROM list_barriers")[0]["state"], "Closed")

    def test_recovery_clears_the_error_and_data_flows_again(self):
        self.client.fail.add("list-zones")
        self.poller.run_due()
        self.client.fail.clear()
        self.clock.t += 10
        self.poller.run_due()
        self.assertTrue(self.store.flush())
        row = self.q("SELECT * FROM poll_status WHERE endpoint='list-zones'")[0]
        self.assertIsNone(row["last_error"])
        self.assertEqual(self.q("SELECT risk FROM list_zones")[0]["risk"], "Safe")

    def test_a_change_in_the_simulator_reaches_the_database(self):
        self.poller.run_due()
        self.client.answers["list-parking-spots"][0]["detectedCars"] = 1
        self.clock.t += 1.5
        self.poller.run_due()
        self.assertTrue(self.store.flush())
        self.assertEqual(self.q("SELECT detectedCars FROM list_parking_spots")[0]["detectedCars"], 1)

    def test_thread_start_and_stop(self):
        self.poller = Poller(self.client, self.store, {"list-parking-spots": 0.05})
        self.poller.start()
        deadline = time.time() + 3
        while self.poller.reads < 3 and time.time() < deadline:
            time.sleep(0.02)
        self.poller.stop()
        self.assertGreaterEqual(self.poller.reads, 3)
        self.assertFalse(self.poller._thread.is_alive())

    def test_only_reads_never_sends_commands(self):
        self.poller.run_due()
        self.assertEqual({m for m, _ in self.client.calls}, {"GET"})


class TestIntervals(unittest.TestCase):
    def test_env_parsing(self):
        self.assertEqual(intervals_from_env("list-zones=1, status=10"),
                         {"list-zones": 1.0, "status": 10.0})
        self.assertEqual(intervals_from_env("nonsense,list-zones=abc,unknown=3,status=0"), {})
        self.assertEqual(intervals_from_env(""), {})


class TestWebhookIntoStore(PollerTestCase):
    """A real POST to the Flask webhook ends up in the database."""

    def setUp(self):
        super().setUp()
        self._handlers = list(webhook._handlers)
        webhook._handlers.clear()
        self.store2, _ = ingest.build(self.path, client=self.client, poll=False)
        self.store2.start()
        self.addCleanup(self.store2.stop)
        self.addCleanup(lambda: (webhook._handlers.clear(), webhook._handlers.extend(self._handlers)))
        self.http = webhook.app.test_client()

    def test_post_answers_at_once_and_the_event_is_stored(self):
        event = {"EventClass": "payment_made", "CarPlateNumber": "TLT 388", "Amount": "1.03",
                 "Reason": "Car Payment", "EventId": "e-1", "SequenceId": 10225, "Signature": None,
                 "ServerDateTime": "2026-09-20 00:01:06"}
        reply = self.http.post("/webhook", json=event)
        self.assertEqual(reply.status_code, 200)
        self.assertTrue(self.store2.flush())
        row = self.q("SELECT * FROM payment_made")[0]
        self.assertEqual((row["CarPlateNumber"], row["Amount"]), ("TLT 388", "1.03"))

    def test_duplicate_post_is_stored_once(self):
        event = {"EventClass": "gate_action", "Name": "gateA", "Action": "Open", "EventId": "g-1",
                 "SequenceId": 1, "Signature": None, "ServerDateTime": "2026-09-20 00:01:06"}
        self.http.post("/webhook", json=event)
        self.http.post("/webhook", json=event)
        self.assertTrue(self.store2.flush())
        self.assertEqual(len(self.q("SELECT * FROM gate_action")), 1)

    def test_invalid_json_is_refused_and_writes_nothing(self):
        reply = self.http.post("/webhook", data="not json", content_type="application/json")
        self.assertEqual(reply.status_code, 400)
        self.assertTrue(self.store2.flush())
        self.assertEqual(self.q("SELECT COUNT(*) n FROM webhook_events")[0]["n"], 0)

    def test_health_route(self):
        reply = self.http.get("/api/ingest/health")
        self.assertEqual(reply.status_code, 200)
        self.assertIn("backlog", reply.get_json())


if __name__ == "__main__":
    unittest.main()
