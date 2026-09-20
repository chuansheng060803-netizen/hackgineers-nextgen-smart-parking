"""Tests for run_all.py: the car logic and the new database ingest in one process.

A whole car life is posted to the real Flask webhook. The simulator is faked, so
the test can check both halves: the commands the car logic sent, and what the
new tables contain.
"""
import json
import os
import sqlite3
import tempfile
import time
import unittest

import run_all
import webhook


class FakeSimulator:
    """Answers the REST calls like the real one and records every command it is sent."""

    def __init__(self):
        self.commands = []
        self.answers = {
            "list-parking-spots": [
                {"name": name, "purpose": "Park", "parkingForCarType": "Any", "zoneParent": "ZONE1",
                 "detectedCars": 0, "broken": False, "isUnderMaintenance": False}
                for name in ("S1", "S2")],
            "list-barriers": [
                {"name": g, "zoneParent": "ZONE1", "broken": False, "isUnderMaintenance": False,
                 "state": "Closed"} for g in ("gateA", "gateB")],
            "list-zones": [{"name": "ZONE1", "gasCarbonMonoxideLevel": 0, "risk": "Safe"}],
            "list-lights": [], "list-alarms": [], "status": {"isActive": True, "cars": 1},
        }

    # what the poller uses
    def call(self, method, path, **kwargs):
        return self.answers[path.rsplit("/", 1)[1]]

    # what the car logic uses
    def list_parking_spots(self):
        return self.answers["list-parking-spots"]

    def list_barriers(self):
        return self.answers["list-barriers"]

    def _set_gate(self, name, state):
        for barrier in self.answers["list-barriers"]:
            if barrier["name"] == name:
                barrier["state"] = state       # like the real gate: the list shows it

    def open_gate(self, name):
        self.commands.append(("open_gate", name))
        self._set_gate(name, "Open")

    def close_gate(self, name):
        self.commands.append(("close_gate", name))
        self._set_gate(name, "Closed")

    def move_car(self, name, destination):
        self.commands.append(("move_car", name, destination))

    def charge_car(self, name, parking_cost, charging_cost):
        self.commands.append(("charge_car", name, parking_cost, charging_cost))


PLATE = "TLT 388"
_seq = [100]


def event(cls, **fields):
    _seq[0] += 1
    base = {"EventClass": cls, "EventId": f"run-all-{_seq[0]}", "SequenceId": _seq[0],
            "Signature": None, "ServerDateTime": "2026-09-20 10:00:00"}
    base.update(fields)
    return base


def spot_event(spot_name, spot_type, direction, when="2026-09-20 10:00:00"):
    return event("car_spot_action", CarPlateNumber=PLATE, CarType="Normal", SpotName=spot_name,
                 SpotType=spot_type, Direction=direction, PlannedParkingDurationInMinutes="2",
                 ServerDateTime=when)


class RunAllTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "all.db")
        self.saved = (list(webhook._handlers), list(webhook._background))
        webhook._handlers.clear()
        webhook._background.clear()
        self.sim = FakeSimulator()
        self.addCleanup(self.dir.cleanup)
        self.addCleanup(self.restore)

    def restore(self):
        webhook._handlers[:] = self.saved[0]
        webhook._background[:] = self.saved[1]

    def start(self, **kwargs):
        kwargs.setdefault("control_client", self.sim)
        store, poller, flow = run_all.build(db_path=self.path, flow_client=self.sim,
                                            poll_client=self.sim, **kwargs)
        store.start()
        self.addCleanup(store.stop)
        self.http = webhook.app.test_client()
        return store, poller, flow

    def post(self, ev):
        self.assertEqual(self.http.post("/webhook", json=ev).status_code, 200)

    def settle(self, store):
        webhook._queue.join()                 # the car logic has handled everything posted
        self.assertTrue(store.flush())        # and the database has everything queued

    def q(self, sql):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(sql).fetchall()]
        finally:
            conn.close()

    def wait_for(self, condition, what, timeout=6.0):
        """The car logic charges and releases on short timers (after the webhook was answered)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.02)
        self.fail(f"timed out waiting for {what}; commands so far: {self.sim.commands}")

    def charged(self):
        return any(c[0] == "charge_car" for c in self.sim.commands)

    def released(self):
        return ("move_car", PLATE, "leavepark") in self.sim.commands

    def car_up_to_the_exit(self):
        self.post(spot_event("ENTRY1", "EntrySpot", "CarIn"))
        self.post(spot_event("ENTRY1", "EntrySpot", "CarOut"))
        self.post(spot_event("S1", "Park", "CarIn", "2026-09-20 10:00:10"))
        self.post(spot_event("S1", "Park", "CarOut", "2026-09-20 10:02:10"))
        self.post(spot_event("EXIT_EXIT", "ExitSpot", "CarIn", "2026-09-20 10:02:10"))
        self.wait_for(self.charged, "the bill (charge_car)")      # like the simulator: payment comes after the bill

    def whole_car_life(self):
        self.car_up_to_the_exit()
        self.post(event("payment_made", CarPlateNumber=PLATE, Amount="2.00", Reason="Car Payment"))
        self.wait_for(self.released, "the release (leavepark)")
        self.post(spot_event("EXIT_EXIT", "ExitSpot", "CarOut", "2026-09-20 10:02:15"))


class TestLive(RunAllTestCase):
    def test_a_whole_car_life_drives_the_car_logic_and_fills_the_new_tables(self):
        store, poller, flow = self.start(live=True)
        poller.run_due()
        self.whole_car_life()
        self.settle(store)

        # the car logic did its job. Order that matters: the entry gate opens BEFORE the
        # car is sent, gateA closes again once the car passed ENTRY1, then bill, release,
        # exit gate. (Other commands in between may differ between car_flow versions.)
        cmds = self.sim.commands
        first_move = cmds.index(("move_car", PLATE, "S1"))
        self.assertIn(("open_gate", "gateA"), cmds[:first_move])
        self.assertEqual(cmds.count(("move_car", PLATE, "S1")), 1)
        self.assertIn(("close_gate", "gateA"), cmds[first_move:])
        charge = cmds.index(("charge_car", PLATE, 2.0, 0))
        release = cmds.index(("move_car", PLATE, "leavepark"))
        self.assertLess(first_move, charge)
        self.assertLess(charge, release)
        self.assertLess(cmds.index(("open_gate", "gateB")), release)    # the exit gate opens, then the car is let out
        self.assertEqual(flow.cars, {})                        # the car has left

        # and every event is in the new tables, with the simulator's own names
        rows = self.q('SELECT "SpotName","SpotType","Direction" FROM car_spot_action '
                      'ORDER BY "SequenceId"')
        self.assertEqual([(r["SpotName"], r["Direction"]) for r in rows],
                         [("ENTRY1", "CarIn"), ("ENTRY1", "CarOut"), ("S1", "CarIn"),
                          ("S1", "CarOut"), ("EXIT_EXIT", "CarIn"), ("EXIT_EXIT", "CarOut")])
        pay = self.q("SELECT * FROM payment_made")[0]
        self.assertEqual((pay["CarPlateNumber"], pay["Amount"]), (PLATE, "2.00"))
        self.assertEqual(self.q("SELECT COUNT(*) n FROM webhook_events")[0]["n"], 7)

        # and the REST state from the poller is there too
        self.assertEqual(len(self.q("SELECT * FROM list_parking_spots")), 2)
        self.assertEqual(len(self.q("SELECT * FROM list_barriers")), 2)

    def test_a_duplicate_event_is_stored_once_and_does_not_confuse_the_car_logic(self):
        store, poller, flow = self.start(live=True)
        first = spot_event("ENTRY1", "EntrySpot", "CarIn")
        self.post(first)
        self.post(dict(first))
        self.settle(store)
        self.assertEqual(len(self.q("SELECT * FROM car_spot_action")), 1)
        self.assertEqual([c for c in self.sim.commands if c[0] == "move_car"],
                         [("move_car", PLATE, "S1")])          # moved once, not twice

    def test_events_are_saved_even_if_the_simulator_refuses_a_command(self):
        def refuse(*args, **kwargs):
            from simulator_api import SimulatorError
            raise SimulatorError("simulator says no")

        self.sim.move_car = refuse
        store, poller, flow = self.start(live=True)
        self.post(spot_event("ENTRY1", "EntrySpot", "CarIn"))
        self.settle(store)
        self.assertEqual(len(self.q("SELECT * FROM car_spot_action")), 1)

    def test_health_routes(self):
        store, poller, flow = self.start(live=True)
        self.assertEqual(self.http.get("/api/flow/health").get_json()["live"], True)
        self.assertTrue(self.http.get("/api/ingest/health").get_json()["writer_alive"])


class TestStartGates(RunAllTestCase):
    def test_both_gates_are_closed_at_startup_like_run_flow(self):
        store, poller, flow = self.start(live=True)
        run_all.start_gates(flow)
        self.assertIn(("close_gate", "gateB"), self.sim.commands)
        if hasattr(flow, "initialize_entry_gate"):
            self.assertIn(("close_gate", "gateA"), self.sim.commands)


class TestDryRun(RunAllTestCase):
    def test_dry_run_sends_nothing_but_still_saves_everything(self):
        store, poller, flow = self.start(live=False)
        for spot, kind, direction, when in [("ENTRY1", "EntrySpot", "CarIn", "10:00:00"), ("ENTRY1", "EntrySpot", "CarOut", "10:00:04"),
                                            ("S1", "Park", "CarIn", "10:00:10"), ("S1", "Park", "CarOut", "10:02:10"),
                                            ("EXIT_EXIT", "ExitSpot", "CarIn", "10:02:10")]:
            self.post(spot_event(spot, kind, direction, f"2026-09-20 {when}"))
        self.post(event("payment_made", CarPlateNumber=PLATE, Amount="2.00", Reason="Car Payment"))
        self.post(spot_event("EXIT_EXIT", "ExitSpot", "CarOut", "2026-09-20 10:02:15"))
        time.sleep(0.6)                                        # the car logic's timers have had time to fire
        self.settle(store)
        self.assertEqual(self.sim.commands, [])                # nothing moved, nothing opened
        self.assertEqual(self.q("SELECT COUNT(*) n FROM webhook_events")[0]["n"], 7)


class TestCharges(RunAllTestCase):
    """The bill and the checked payment reach the charges table (this is what income is read from)."""

    def test_an_accepted_payment_becomes_a_paid_charge(self):
        store, poller, flow = self.start(live=True)
        self.whole_car_life()
        self.settle(store)
        rows = self.q("SELECT * FROM charges")
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["CarPlateNumber"], rows[0]["billed_total"], rows[0]["status"]),
                         (PLATE, 2.0, "paid"))
        self.assertIsNotNone(rows[0]["paid_at"])

    def test_a_fake_payment_is_stored_as_an_event_but_never_counts_as_income(self):
        store, poller, flow = self.start(live=True)
        self.car_up_to_the_exit()
        self.post(event("payment_made", CarPlateNumber=PLATE, Amount="9.99", Reason="Car Payment"))
        self.settle(store)
        time.sleep(0.8)                                                     # give a wrong release every chance to happen
        self.assertEqual(len(self.q("SELECT * FROM payment_made")), 1)      # the event is kept as sent
        charge = self.q("SELECT * FROM charges")[0]
        self.assertEqual((charge["status"], charge["paid_at"]), ("billed", None))
        self.assertNotIn(("move_car", PLATE, "leavepark"), self.sim.commands)   # and the car is held


class TestManualControl(RunAllTestCase):
    """An operator's order is final: the car logic cannot open a gate that is held closed."""

    def test_a_gate_held_closed_keeps_the_next_car_out(self):
        store, poller, flow = self.start(live=True)
        controller = run_all._active["controller"]
        controller.set_mode("gateA", "forced_closed", "ks", "operator")
        self.post(spot_event("ENTRY1", "EntrySpot", "CarIn"))
        self.settle(store)
        self.assertNotIn(("open_gate", "gateA"), self.sim.commands)
        self.assertFalse([c for c in self.sim.commands if c[0] == "move_car"])   # the car is not sent in

    def test_a_gate_held_open_is_not_closed_behind_the_car(self):
        store, poller, flow = self.start(live=True)
        controller = run_all._active["controller"]
        controller.set_mode("gateA", "forced_open", "ks", "operator")
        self.whole_car_life()
        self.settle(store)
        self.assertNotIn(("close_gate", "gateA"), self.sim.commands)
        self.assertIn(("move_car", PLATE, "S1"), self.sim.commands)              # the car still parks normally
        self.assertEqual(flow.cars, {})                                          # and leaves

    def test_a_paid_car_waits_while_the_exit_gate_is_held_closed_and_leaves_when_released(self):
        store, poller, flow = self.start(live=True)
        controller = run_all._active["controller"]
        controller.set_mode("gateB", "forced_closed", "ks", "operator")
        self.car_up_to_the_exit()
        self.post(event("payment_made", CarPlateNumber=PLATE, Amount="2.00", Reason="Car Payment"))
        time.sleep(1.5)                                                       # the release check has tried several times
        self.assertFalse(self.released())
        self.assertNotIn(("open_gate", "gateB"), self.sim.commands)
        controller.set_mode("gateB", "auto", "ks", "operator")
        self.wait_for(self.released, "the release once the gate is back on automatic")

    def test_the_operator_command_is_in_the_log_table(self):
        store, poller, flow = self.start(live=True)
        run_all._active["controller"].set_mode("gateB", "forced_open", "ks", "operator")
        self.settle(store)
        self.assertEqual(self.q("SELECT username, gate, mode, result FROM control_log"),
                         [{"username": "ks", "gate": "gateB", "mode": "forced_open", "result": "done"}])
        self.assertEqual(self.q("SELECT gate, mode FROM gate_control"), [{"gate": "gateB", "mode": "forced_open"}])

    def test_a_dry_run_records_the_order_but_moves_nothing(self):
        store, poller, flow = self.start(live=False)
        run_all._active["controller"].set_mode("gateA", "forced_open", "ks", "operator")
        self.assertEqual(self.sim.commands, [])


if __name__ == "__main__":
    unittest.main()
