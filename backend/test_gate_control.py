"""Tests for gate_control.py (the operator's order is final) and control_api.py (who may give it)."""
import os
import sqlite3
import tempfile
import unittest

import control_api
import gate_control
import webhook
from gate_control import GateController, GateRefused
from simulator_api import SimulatorError
from store import Store


class FakeGates:
    """Two gates that behave like the simulator: a command changes the state the list shows."""

    def __init__(self):
        self.commands = []
        self.barriers = [{"name": g, "state": "Closed", "broken": False, "isUnderMaintenance": False}
                         for g in ("gateA", "gateB")]
        self.fail = False

    def _gate(self, name):
        return next(b for b in self.barriers if b["name"] == name)

    def list_barriers(self):
        if self.fail:
            raise SimulatorError("simulator is down")
        return [dict(b) for b in self.barriers]

    def open_gate(self, name):
        if self.fail:
            raise SimulatorError("simulator is down")
        self.commands.append(("open", name))
        self._gate(name)["state"] = "Open"

    def close_gate(self, name):
        if self.fail:
            raise SimulatorError("simulator is down")
        self.commands.append(("close", name))
        self._gate(name)["state"] = "Closed"

    def repair_gate(self, name):
        if self.fail:
            raise SimulatorError("simulator is down")
        self.commands.append(("repair", name))
        self._gate(name)["isUnderMaintenance"] = True


class ControllerCase(unittest.TestCase):
    def setUp(self):
        self.sim = FakeGates()                 # what the operator's commands go to
        self.flow_sim = FakeGates()            # what the car logic's commands go to
        self.c = GateController(self.sim, gates=("gateA", "gateB"))
        self.car_logic = self.c.wrap(self.flow_sim)


class TestAutomation(ControllerCase):
    def test_in_auto_the_car_logic_moves_the_gates_as_before(self):
        self.car_logic.open_gate("gateA")
        self.car_logic.close_gate("gateA")
        self.assertEqual(self.flow_sim.commands, [("open", "gateA"), ("close", "gateA")])

    def test_other_calls_pass_straight_through(self):
        self.assertEqual(len(self.car_logic.list_barriers()), 2)

    def test_a_gate_held_closed_ignores_the_car_logic_and_the_car_logic_is_told(self):
        self.c.set_mode("gateA", "forced_closed", "ks", "operator")
        with self.assertRaises(SimulatorError):
            self.car_logic.open_gate("gateA")
        self.car_logic.close_gate("gateA")                    # already what the operator wants
        self.assertEqual(self.flow_sim.commands, [])
        self.assertEqual(self.c.status()["gates"]["gateA"]["held_back"], 1)

    def test_a_gate_held_open_ignores_a_close_from_the_car_logic(self):
        self.c.set_mode("gateA", "forced_open", "ks", "operator")
        self.car_logic.close_gate("gateA")
        self.car_logic.open_gate("gateA")                     # already open: nothing to send
        self.assertEqual(self.flow_sim.commands, [])
        self.assertEqual(self.sim.commands, [("open", "gateA")])

    def test_holding_one_gate_does_not_touch_the_other(self):
        self.c.set_mode("gateA", "forced_closed", "ks", "operator")
        self.car_logic.open_gate("gateB")
        self.assertEqual(self.flow_sim.commands, [("open", "gateB")])

    def test_back_to_auto_gives_the_gate_back_to_the_car_logic(self):
        self.c.set_mode("gateA", "forced_closed", "ks", "operator")
        self.c.set_mode("gateA", "auto", "ks", "operator")
        self.car_logic.open_gate("gateA")
        self.assertEqual(self.flow_sim.commands, [("open", "gateA")])


class TestOperatorOrders(ControllerCase):
    def test_hold_open_and_closed_send_the_command_at_once(self):
        self.c.set_mode("gateA", "forced_open", "ks", "operator")
        self.c.set_mode("gateB", "forced_closed", "ks", "operator")
        self.assertEqual(self.sim.commands, [("open", "gateA")])   # gateB was already closed
        self.assertEqual(self.c.mode_of("gateA"), "forced_open")

    def test_an_admin_cannot_control_anything(self):
        with self.assertRaises(GateRefused) as caught:
            self.c.set_mode("gateA", "forced_open", "boss", "admin")
        self.assertEqual(caught.exception.status, 403)
        self.assertEqual((self.sim.commands, self.c.mode_of("gateA")), ([], "auto"))

    def test_unknown_mode_and_unknown_gate_are_refused(self):
        with self.assertRaises(GateRefused) as caught:
            self.c.set_mode("gateA", "sideways", "ks", "operator")
        self.assertEqual(caught.exception.status, 400)
        with self.assertRaises(GateRefused) as caught:
            self.c.set_mode("gateZ", "forced_open", "ks", "operator")
        self.assertEqual(caught.exception.status, 404)

    def test_a_broken_or_maintained_gate_is_never_operated(self):
        self.sim._gate("gateA")["broken"] = True
        self.sim._gate("gateB")["isUnderMaintenance"] = True
        for gate in ("gateA", "gateB"):
            with self.assertRaises(GateRefused):
                self.c.set_mode(gate, "forced_open", "ks", "operator")
        self.assertEqual(self.sim.commands, [])
        self.c.set_mode("gateA", "auto", "ks", "operator")     # letting go is always fine

    def test_when_the_simulator_is_unreachable_nothing_changes(self):
        self.sim.fail = True
        with self.assertRaises(GateRefused):
            self.c.set_mode("gateA", "forced_open", "ks", "operator")
        self.assertEqual(self.c.mode_of("gateA"), "auto")

    def test_when_the_simulator_rejects_the_command_the_mode_is_not_saved(self):
        self.sim.open_gate = lambda name: (_ for _ in ()).throw(SimulatorError("no"))
        with self.assertRaises(GateRefused) as caught:
            self.c.set_mode("gateA", "forced_open", "ks", "operator")
        self.assertEqual(caught.exception.status, 502)
        self.assertEqual(self.c.mode_of("gateA"), "auto")


class TestEnforcement(ControllerCase):
    def test_a_forced_gate_that_someone_changed_is_put_back(self):
        self.c.set_mode("gateA", "forced_closed", "ks", "operator")
        self.sim._gate("gateA")["state"] = "Open"              # something else opened it
        self.assertEqual(self.c.enforce_once(), ["gateA"])
        self.assertEqual(self.sim._gate("gateA")["state"], "Closed")
        self.assertEqual(self.c.enforce_once(), [])            # nothing more to do

    def test_a_moving_gate_is_left_alone(self):
        self.c.set_mode("gateA", "forced_open", "ks", "operator")
        self.sim._gate("gateA")["state"] = "Opening"
        self.assertEqual(self.c.enforce_once(), [])

    def test_a_gate_that_breaks_while_forced_is_left_alone_until_it_is_healthy(self):
        self.c.set_mode("gateA", "forced_open", "ks", "operator")
        gate = self.sim._gate("gateA")
        gate.update(state="Closed", broken=True)
        before = list(self.sim.commands)
        self.assertEqual(self.c.enforce_once(), [])
        self.assertEqual(self.sim.commands, before)            # never operated while broken
        gate["broken"] = False                                 # repaired
        self.assertEqual(self.c.enforce_once(), ["gateA"])     # the order is applied again
        self.assertEqual(gate["state"], "Open")

    def test_auto_gates_are_never_touched_by_the_enforcer(self):
        self.sim._gate("gateA")["state"] = "Open"
        self.assertEqual(self.c.enforce_once(), [])


class TestRepair(ControllerCase):
    def test_a_broken_gate_can_be_sent_for_repair(self):
        self.sim._gate("gateA")["broken"] = True
        self.c.request_repair("gateA", "ks", "operator")
        self.assertEqual(self.sim.commands, [("repair", "gateA")])

    def test_a_working_gate_needs_a_confirmation(self):
        with self.assertRaises(GateRefused):
            self.c.request_repair("gateA", "ks", "operator")
        self.assertEqual(self.sim.commands, [])
        self.c.request_repair("gateA", "ks", "operator", confirm_healthy=True)
        self.assertEqual(self.sim.commands, [("repair", "gateA")])

    def test_a_gate_already_under_maintenance_is_not_touched_again(self):
        self.sim._gate("gateA")["isUnderMaintenance"] = True
        with self.assertRaises(GateRefused):
            self.c.request_repair("gateA", "ks", "operator", confirm_healthy=True)
        self.assertEqual(self.sim.commands, [])

    def test_admin_cannot_request_repairs(self):
        with self.assertRaises(GateRefused) as caught:
            self.c.request_repair("gateA", "boss", "admin", confirm_healthy=True)
        self.assertEqual(caught.exception.status, 403)


class TestSaved(unittest.TestCase):
    """Orders and the log go to the database through the single writer, and survive a restart."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "c.db")
        self.store = Store(self.path)
        self.store.start()
        self.addCleanup(self.store.stop)
        self.sim = FakeGates()
        self.c = GateController(self.sim, store=self.store, gates=("gateA", "gateB"))

    def rows(self, sql):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(sql).fetchall()]
        finally:
            conn.close()

    def test_an_order_and_a_refusal_are_both_logged(self):
        self.c.set_mode("gateA", "forced_closed", "ks", "operator")
        with self.assertRaises(GateRefused):
            self.c.set_mode("gateA", "forced_open", "boss", "admin")
        self.assertTrue(self.store.flush())
        log = self.rows("SELECT username, gate, mode, result FROM control_log ORDER BY log_id")
        self.assertEqual([(r["username"], r["mode"], r["result"]) for r in log],
                         [("ks", "forced_closed", "done"), ("boss", "forced_open", "refused")])
        self.assertEqual(self.rows("SELECT gate, mode, set_by FROM gate_control"),
                         [{"gate": "gateA", "mode": "forced_closed", "set_by": "ks"}])   # the refusal changed nothing

    def test_a_restart_keeps_the_order(self):
        self.c.set_mode("gateA", "forced_open", "ks", "operator")
        self.assertTrue(self.store.flush())
        after_restart = GateController(FakeGates(), gates=("gateA", "gateB"))
        after_restart.load(self.path)
        self.assertEqual(after_restart.mode_of("gateA"), "forced_open")
        self.assertEqual(after_restart.mode_of("gateB"), "auto")


class TestEndpoint(unittest.TestCase):
    """The HTTP side: the key, this computer only, and the role taken from the database."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "c.db")
        self.store = Store(self.path)
        self.store.start()
        self.addCleanup(self.store.stop)
        conn = sqlite3.connect(self.path)
        conn.executemany("INSERT INTO app_users VALUES (?, 'x', ?, '2026-09-20 10:00:00')",
                         [("ks", "operator"), ("boss", "admin")])
        conn.commit()
        conn.close()
        self.sim = FakeGates()
        self.controller = GateController(self.sim, store=self.store, gates=("gateA", "gateB"))
        self.token = control_api.load_token(self.path, create=True)
        control_api.configure(self.controller, self.path, self.token)
        self.http = webhook.app.test_client()
        self.headers = {control_api.TOKEN_HEADER: self.token}

    def post(self, gate, mode="forced_open", user="ks", headers=None, **extra):
        return self.http.post(f"/api/control/gates/{gate}/mode", json={"mode": mode, "username": user, **extra},
                              headers=self.headers if headers is None else headers)

    def test_an_operator_can_hold_a_gate(self):
        response = self.post("gateA")
        self.assertEqual((response.status_code, response.get_json()["ok"]), (200, True))
        self.assertEqual(self.controller.mode_of("gateA"), "forced_open")

    def test_without_the_key_nothing_happens(self):
        for headers in ({}, {control_api.TOKEN_HEADER: "guess"}):
            self.assertEqual(self.post("gateA", headers=headers).status_code, 401)
        self.assertEqual(self.controller.mode_of("gateA"), "auto")

    def test_the_role_comes_from_the_database_not_from_the_request(self):
        response = self.post("gateA", user="boss", role="operator")          # an admin claiming to be operator
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.controller.mode_of("gateA"), "auto")
        self.assertEqual(self.post("gateA", user="nobody").status_code, 403)

    def test_a_refusal_carries_a_readable_message(self):
        self.sim._gate("gateA")["broken"] = True
        response = self.post("gateA")
        self.assertEqual(response.status_code, 409)
        self.assertIn("broken", response.get_json()["message"])

    def test_repair_needs_the_same_checks(self):
        url = "/api/control/gates/gateA/repair"
        self.assertEqual(self.http.post(url, json={"username": "ks"}, headers={}).status_code, 401)
        self.assertEqual(self.http.post(url, json={"username": "boss", "confirm": True},
                                        headers=self.headers).status_code, 403)
        self.assertEqual(self.http.post(url, json={"username": "ks"}, headers=self.headers).status_code, 409)
        self.assertEqual(self.http.post(url, json={"username": "ks", "confirm": True},
                                        headers=self.headers).status_code, 200)
        self.assertEqual(self.sim.commands, [("repair", "gateA")])

    def test_only_this_computer_may_call(self):
        response = self.http.post("/api/control/gates/gateA/mode", json={"mode": "forced_open", "username": "ks"},
                                  headers=self.headers, environ_overrides={"REMOTE_ADDR": "192.168.1.20"})
        self.assertEqual(response.status_code, 403)

    def test_the_key_is_made_once_and_reused(self):
        self.assertEqual(control_api.load_token(self.path), self.token)
        self.assertEqual(len(self.token), 48)


if __name__ == "__main__":
    unittest.main()
