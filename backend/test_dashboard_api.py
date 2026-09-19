"""Tests for the dashboard bridge, using a stub simulator shaped like level 1."""
import json
import re
import sys
import time
import unittest
from unittest import mock
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dashboard"))

import dashboard_api
from dashboard_api import DashboardState, build_blueprint

NOW = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def spot(name, detected=0, broken=False, maintenance=False, purpose="Park"):
    return {"name": name, "zoneParent": "ZONE1", "purpose": purpose,
            "parkingForCarType": "Any", "detectedCars": detected,
            "broken": broken, "isUnderMaintenance": maintenance,
            "lastCarPlate": None}


class StubClient:
    """Shaped like the real simulator: 30 Park spots in ZONE1, 3 gates, no fans."""

    def __init__(self):
        self.spots = [spot(f"S{i}") for i in range(1, 31)]
        self.spots.append(spot("ENTRY1", purpose="EntrySpot"))
        self.spots.append(spot("EXIT_EXIT", purpose="ExitSpot"))
        self.barriers = [
            {"name": "gateA", "zoneParent": "ZONE1", "state": "Closed",
             "broken": False, "isUnderMaintenance": False},
            {"name": "gateB", "zoneParent": "ZONE1", "state": "Open",
             "broken": False, "isUnderMaintenance": False},
            {"name": "gateC", "zoneParent": "", "state": "Open",
             "broken": False, "isUnderMaintenance": False},
        ]
        self.commands = []

    def list_parking_spots(self):
        return self.spots

    def list_barriers(self):
        return self.barriers

    def open_gate(self, name):
        self.commands.append(("open", name))
        self._set(name, "Open")

    def close_gate(self, name):
        self.commands.append(("close", name))
        self._set(name, "Closed")

    def _set(self, name, state):
        for gate in self.barriers:
            if gate["name"] == name:
                gate["state"] = state


class StubFlow:
    def __init__(self):
        self.cars = {}

    def add(self, plate, stage, spot_name, parked=False):
        now = datetime.now()
        self.cars[plate] = {
            "plate": plate, "car_type": "Normal", "stage": stage, "spot": spot_name,
            "entry_in_time": now - timedelta(minutes=5),
            "parked_in_time": now - timedelta(minutes=4) if parked else None,
            "parked_out_time": None, "parking_cost": 4, "charging_cost": 0,
            "paid": False,
        }


def event(event_class, **fields):
    base = {"EventClass": event_class, "ServerDateTime": NOW, "EventId": id(fields)}
    base.update(fields)
    return base


class DashboardApiTests(unittest.TestCase):
    def setUp(self):
        self.client = StubClient()
        self.flow = StubFlow()
        self.state = DashboardState(self.flow, self.client, use_db=False)

    # ---- snapshot shape ---------------------------------------------------

    def test_snapshot_matches_the_dashboards_contract(self):
        snap = self.state.snapshot()
        for key in ("generated_at", "spots", "cars", "events", "sessions", "archive",
                    "gates", "fans", "zones", "history", "stats", "penalties"):
            self.assertIn(key, snap, f"snapshot is missing {key}")
        self.assertEqual(set(snap["history"]), {"occupancy", "co", "arrivals"})
        self.assertEqual(set(snap["stats"]),
                         {"revenue", "cars_served", "penalty_count", "penalty_total",
                          "refused_last_10min"})
        json.dumps(snap)  # must be serialisable

    def test_the_dashboard_accepts_the_snapshot_unchanged(self):
        import data_source
        normalised = data_source.normalise(self.state.snapshot())
        self.assertEqual(len(normalised["spots"]), 30)
        self.assertEqual(normalised["source"], "simulator")

    def test_alerts_can_be_derived_from_it(self):
        import alerts
        self.assertEqual(alerts.derive_alerts(self.state.snapshot()), [])

    # ---- spots ------------------------------------------------------------

    def test_only_park_spots_are_shown_and_entry_exit_are_not(self):
        names = [s["name"] for s in self.state.spots()]
        self.assertEqual(len(names), 30)
        self.assertNotIn("ENTRY1", names)
        self.assertNotIn("EXIT_EXIT", names)
        self.assertEqual(names[:3], ["S1", "S2", "S3"])

    def test_spot_states(self):
        self.client.spots[0]["detectedCars"] = 1      # S1 occupied
        self.client.spots[1]["broken"] = True         # S2 broken
        self.client.spots[2]["isUnderMaintenance"] = True
        self.flow.add("WXY 111", "MOVING", "S4")      # reserved, on its way
        self.flow.add("WXY 222", "PARKED", "S1", parked=True)
        self.state._read_spots()
        by_name = {s["name"]: s for s in self.state.spots()}
        self.assertEqual(by_name["S1"]["state"], "occupied")
        self.assertEqual(by_name["S1"]["car"], "WXY 222")
        self.assertEqual(by_name["S2"]["state"], "broken")
        self.assertEqual(by_name["S3"]["state"], "maintenance")
        self.assertEqual(by_name["S4"]["state"], "reserved")
        self.assertEqual(by_name["S5"]["state"], "available")

    # ---- cars -------------------------------------------------------------

    def test_car_stages_become_readable_statuses(self):
        self.flow.add("WAA 1", "MOVING", "S7")
        self.flow.add("WAA 2", "PARKED", "S8", parked=True)
        self.flow.add("WAA 3", "CHARGING", "S9", parked=True)
        status = {c["plate"]: c["status"] for c in self.state.cars()}
        self.assertEqual(status["WAA 1"], "Heading to spot")
        self.assertEqual(status["WAA 2"], "Parked")
        self.assertEqual(status["WAA 3"], "Heading to exit")

    # ---- events -----------------------------------------------------------

    def test_entry_park_and_exit_events(self):
        self.state.on_event(event("car_spot_action", CarPlateNumber="WBB 1",
                                  SpotType="EntrySpot", SpotName="ENTRY1", Direction="CarIn"))
        self.state.on_event(event("car_spot_action", CarPlateNumber="WBB 1",
                                  SpotType="ParkingSpot", SpotName="S5", Direction="CarIn"))
        kinds = [e["kind"] for e in self.state.events]
        self.assertIn("entry", kinds)
        self.assertIn("park", kinds)
        self.assertEqual(len(self.state.arrivals), 1)

    def test_payment_adds_to_revenue(self):
        self.state.on_event(event("payment_made", CarPlateNumber="WBB 2", Amount=7.5))
        self.assertEqual(self.state.stats()["revenue"], 7.5)

    def test_penalty_is_recorded(self):
        self.state.on_event(event("penalty", CarPlateNumber="WBB 3", Amount=10,
                                  Reason="Gate broke", SpotName="gateA"))
        stats = self.state.stats()
        self.assertEqual(stats["penalty_count"], 1)
        self.assertEqual(stats["penalty_total"], 10)
        self.assertEqual(self.state.snapshot()["penalties"][0]["reason"], "Gate broke")

    def test_carbon_monoxide_event_fills_the_zone_panel(self):
        self.state.on_event(event("carbon_monoxide_event", ZoneName="ZONE1", Value=63.2))
        zones = self.state.zones()
        self.assertEqual(zones, [{"name": "ZONE1", "co_ppm": 63.2, "risk": "Mid"}])

    def test_no_co_events_means_no_zones(self):
        self.assertEqual(self.state.zones(), [])

    def test_exit_completes_a_session(self):
        self.flow.add("WBB 4", "CHARGING", "S6", parked=True)
        self.state.on_event(event("car_spot_action", CarPlateNumber="WBB 4",
                                  SpotType="ExitSpot", SpotName="EXIT_EXIT",
                                  Direction="CarOut"))
        self.assertEqual(self.state.stats()["cars_served"], 1)
        visit = self.state.snapshot()["sessions"][0]
        self.assertEqual(visit["plate"], "WBB 4")
        self.assertEqual(visit["spot"], "S6")
        self.assertEqual(visit["charge"], 4)

    def test_a_broken_event_does_not_crash_the_dashboard(self):
        self.state.on_event({"EventClass": "penalty", "Amount": "not a number"})
        self.state.on_event({})
        self.assertTrue(self.state.snapshot())

    # ---- gates ------------------------------------------------------------

    def test_gates_are_read_from_the_simulator(self):
        gates = self.state.gates()
        self.assertEqual([g["name"] for g in gates], ["gateA", "gateB", "gateC"])
        self.assertEqual(gates[0]["state"], "Closed")
        self.assertEqual(gates[0]["health"], "ok")

    def test_gates_wrapped_in_an_object_are_still_read(self):
        self.client.list_barriers = lambda: {"barriers": self.client.barriers}
        self.state._read_gates()
        self.assertEqual(len(self.state.gates()), 3)

    def test_broken_gate_health(self):
        self.client.barriers[0]["broken"] = True
        self.state._read_gates()
        self.assertEqual(self.state.gates()[0]["health"], "broken")

    # ---- controls ---------------------------------------------------------

    def test_viewer_cannot_control_anything(self):
        ok, message = self.state.control("gate", "gateA", "open", "viewer")
        self.assertFalse(ok)
        self.assertIn("Operator", message)
        self.assertEqual(self.client.commands, [])

    def test_operator_opens_and_closes_a_gate(self):
        ok, _ = self.state.control("gate", "gateA", "open", "operator")
        self.assertTrue(ok)
        self.assertEqual(self.client.commands[-1], ("open", "gateA"))
        self.state._read_gates()
        self.assertEqual(self.state.gates()[0]["state"], "Open")
        self.assertEqual(self.state.gates()[0]["manual"], "open")

        ok, _ = self.state.control("gate", "gateA", "close", "operator")
        self.assertTrue(ok)
        self.assertEqual(self.client.commands[-1], ("close", "gateA"))

    def test_auto_releases_the_manual_hold(self):
        self.state.control("gate", "gateA", "open", "operator")
        ok, message = self.state.control("gate", "gateA", "auto", "operator")
        self.assertTrue(ok)
        self.state._read_gates()
        self.assertIsNone(self.state.gates()[0]["manual"])

    def test_a_broken_gate_is_never_operated(self):
        self.client.barriers[0]["broken"] = True
        self.state._read_gates()
        ok, message = self.state.control("gate", "gateA", "open", "operator")
        self.assertFalse(ok)
        self.assertIn("broken", message)
        self.assertEqual(self.client.commands, [])

    def test_fan_control_says_there_are_no_fans(self):
        ok, message = self.state.control("fan", "fan0", "on", "operator")
        self.assertFalse(ok)
        self.assertIn("no exhaust fans", message)

    def test_unknown_gate_is_refused(self):
        ok, _ = self.state.control("gate", "gateZ", "open", "operator")
        self.assertFalse(ok)

    def test_simulator_failure_is_reported_not_raised(self):
        def boom(name):
            raise RuntimeError("simulator is down")
        self.client.open_gate = boom
        ok, message = self.state.control("gate", "gateA", "open", "operator")
        self.assertFalse(ok)
        self.assertIn("simulator is down", message)

    # ---- the HTTP layer ---------------------------------------------------

    def test_http_endpoints(self):
        from flask import Flask
        app = Flask(__name__)
        app.register_blueprint(build_blueprint(self.state))
        http = app.test_client()

        snap = http.get("/api/snapshot")
        self.assertEqual(snap.status_code, 200)
        self.assertEqual(len(snap.get_json()["spots"]), 30)

        history = http.get("/api/history?date=" + datetime.now().date().isoformat())
        self.assertEqual(history.status_code, 200)
        self.assertIsInstance(history.get_json(), list)

        refused = http.post("/api/control/gate/gateA/open", json={"role": "Admin"})
        self.assertEqual(refused.status_code, 401)
        self.assertFalse(refused.get_json()["ok"])

        with mock.patch("database.database_service.authenticate_user") as authenticate:
            with mock.patch.object(self.state, "control", wraps=self.state.control) as control:
                authenticate.return_value = None
                refused = http.post("/api/control/gate/gateA/open", auth=("alice", "wrong"))
                self.assertEqual(refused.status_code, 401)
                authenticate.assert_called_once_with("alice", "wrong")
                control.assert_not_called()

                authenticate.return_value = {"role": "OPERATOR"}
                allowed = http.post("/api/control/gate/gateA/open", auth=("alice", "correct"),
                                    json={"role": "Admin"})
                self.assertEqual(allowed.status_code, 200)
                self.assertTrue(allowed.get_json()["ok"])
                control.assert_called_once_with("gate", "gateA", "open", "OPERATOR")

                refused = http.post("/api/control/gate/gateZ/open", auth=("alice", "correct"))
                self.assertEqual(refused.status_code, 403)
                self.assertFalse(refused.get_json()["ok"])

        self.assertTrue(http.get("/api/health").get_json()["ok"])

    def test_the_simulator_going_down_keeps_the_last_good_data(self):
        good = self.state.snapshot()
        self.assertEqual(len(good["spots"]), 30)

        def boom():
            raise RuntimeError("connection refused")
        self.client.list_parking_spots = boom
        self.client.list_barriers = boom
        self.state._read_spots(); self.state._read_gates()

        snap = self.state.snapshot()          # must not raise
        self.assertEqual(len(snap["spots"]), 30, "should keep showing the last good spots")
        self.assertEqual(len(snap["gates"]), 3)

    def test_with_no_simulator_at_all_the_snapshot_is_empty_not_broken(self):
        state = DashboardState(flow=None, client=None, use_db=False)
        snap = state.snapshot()
        self.assertEqual(snap["spots"], [])
        self.assertEqual(snap["gates"], [])
        self.assertEqual(snap["cars"], [])
        json.dumps(snap)




class RealSimulatorPayloadTests(unittest.TestCase):
    """Runs the bridge against the JSON the live simulator actually returned
    (captured by probe_simulator.py), not against hand-written stubs."""

    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parent / "real_simulator_sample.json"
        if not path.exists():
            raise unittest.SkipTest("no captured simulator sample")
        cls.real = json.loads(path.read_text(encoding="utf-8"))

    def setUp(self):
        real = self.real

        class RealShapedClient:
            def list_parking_spots(self):
                return real["/api/v1/list-parking-spots"]

            def list_barriers(self):
                return real["/api/v1/list-barriers"]

            def call(self, method, path):
                if path not in real:
                    raise RuntimeError("404")
                return real[path]

            def open_gate(self, name):
                pass

            def close_gate(self, name):
                pass

        self.state = DashboardState(StubFlow(), RealShapedClient(), use_db=False)

    def test_thirty_real_park_spots(self):
        spots = self.state.spots()
        self.assertEqual(len(spots), 30)
        self.assertEqual({s["zone"] for s in spots}, {"ZONE1"})
        self.assertEqual({s["type"] for s in spots}, {"Any"})
        self.assertEqual({s["state"] for s in spots}, {"available"})
        self.assertEqual([s["name"] for s in spots][:3], ["S1", "S2", "S3"])

    def test_real_gates(self):
        gates = self.state.gates()
        self.assertEqual([g["name"] for g in gates], ["gateA", "gateB", "gateC"])
        self.assertEqual([g["state"] for g in gates], ["Closed", "Open", "Open"])
        self.assertTrue(all(g["health"] == "ok" for g in gates))

    def test_real_zone_gives_the_co_panel_its_data(self):
        self.assertEqual(self.state.zones(),
                         [{"name": "ZONE1", "co_ppm": 0.0, "risk": "Safe"}])

    def test_level_one_has_no_fans(self):
        self.assertEqual(self.state.fans(), [])

    def test_the_dashboard_renders_this_snapshot(self):
        import alerts
        import data_source
        snap = data_source.normalise(self.state.snapshot())
        self.assertEqual(len(snap["spots"]), 30)
        self.assertEqual(snap["fans"], [])
        derived = alerts.derive_alerts(snap)
        self.assertEqual([a for a in derived if a["severity"] == "critical"], [])



class SlowSimulatorTests(unittest.TestCase):
    """A busy simulator must never make a dashboard request hang: every web
    request is served from the cache the background thread keeps warm."""

    def test_snapshot_is_fast_even_when_every_simulator_call_is_slow(self):
        class SlowClient(StubClient):
            def list_parking_spots(self):
                time.sleep(3)
                return super().list_parking_spots()

            def list_barriers(self):
                time.sleep(3)
                return super().list_barriers()

            def call(self, method, path):
                time.sleep(3)
                raise RuntimeError("slow")

        state = DashboardState(StubFlow(), SlowClient(), use_db=False)
        started = time.monotonic()
        snap = state.snapshot()
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.0,
                        f"snapshot took {elapsed:.1f}s; it must not wait on the simulator")
        self.assertIn("spots", snap)

    def test_snapshot_never_touches_the_database(self):
        """CarFlow writes to SQLite on every webhook; a reader can block behind a
        writer for seconds. The request path must not read the database."""
        state = DashboardState(StubFlow(), StubClient(), use_db=True)

        def boom(*args, **kwargs):
            raise AssertionError("snapshot must not query the database")

        state._archive_from_db = boom
        state._db_sessions = boom
        started = time.monotonic()
        snap = state.snapshot()
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(len(snap["archive"]), 1)     # today, built from memory


class HeadlineNumbersTests(unittest.TestCase):
    """The headline numbers must agree with the car park the judges can see."""

    def setUp(self):
        import ui
        self.ui = ui
        # the real situation seen on screen: 2 cars parked, 14 spots held for
        # cars queued outside a closed barrier, 14 spots free and unheld
        spots = ([{"state": "occupied"}] * 2 + [{"state": "reserved"}] * 14
                 + [{"state": "available"}] * 14)
        cars = ([{"status": "Heading to spot"}] * 23
                + [{"status": "Heading to exit"}] * 2 + [{"status": "Parked"}] * 2)
        html = ui.kpis(spots, cars, {"revenue": 5.09, "cars_served": 3,
                                     "penalty_count": 0, "penalty_total": 0})
        self.text = " ".join(re.sub("<[^>]+>", " ", html).split())

    def test_occupancy_is_what_is_physically_parked(self):
        # the simulator reports 28 of 30 free, so occupancy is 7%, not 53%
        self.assertIn("Occupancy 7 %", self.text)
        self.assertIn("2 parked · 28 empty", self.text)

    def test_reserved_spots_are_shown_but_not_counted_as_occupied(self):
        self.assertIn("14 held for cars on the way", self.text)

    def test_cars_inside_excludes_cars_still_outside(self):
        # 2 parked + 2 leaving are inside; the 23 queued at the barrier are not
        self.assertIn("Cars inside 4", self.text)
        self.assertIn("23 heading in", self.text)

    def test_cars_inside_can_never_exceed_the_car_park(self):
        spots = [{"state": "available"}] * 30
        cars = [{"status": "Heading to spot"}] * 100
        text = " ".join(re.sub("<[^>]+>", " ", self.ui.kpis(spots, cars, {})).split())
        self.assertIn("Cars inside 0", text)


class RunningChargeTests(unittest.TestCase):
    """A parked car is earning money even though nothing has been billed yet."""

    def setUp(self):
        self.flow = StubFlow()
        self.state = DashboardState(self.flow, StubClient(), use_db=False)

    def _car(self, plate, **over):
        now = datetime.now()
        car = {"plate": plate, "car_type": "Normal", "stage": "PARKED", "spot": "S1",
               "entry_in_time": now - timedelta(minutes=4),
               "parked_in_time": now - timedelta(minutes=3), "parked_out_time": None,
               "parking_cost": None, "charging_cost": None, "paid": False}
        car.update(over)
        self.flow.cars[plate] = car
        return car

    def by_plate(self):
        return {c["plate"]: c for c in self.state.cars()}

    def test_a_parked_car_shows_a_running_charge_not_zero(self):
        self._car("AAA 1")
        self.assertAlmostEqual(self.by_plate()["AAA 1"]["estimated_charge"], 3.0, places=1)

    def test_an_electric_car_also_accrues_the_charging_cost(self):
        self._car("BBB 2", car_type="Electric")
        self.assertAlmostEqual(self.by_plate()["BBB 2"]["estimated_charge"], 6.0, places=1)

    def test_a_billed_car_shows_what_it_was_actually_billed(self):
        self._car("CCC 3", stage="CHARGING", parking_cost=8.05, charging_cost=0)
        self.assertEqual(self.by_plate()["CCC 3"]["estimated_charge"], 8.05)

    def test_a_car_that_has_not_parked_yet_owes_nothing(self):
        self._car("DDD 4", stage="MOVING", parked_in_time=None)
        self.assertEqual(self.by_plate()["DDD 4"]["estimated_charge"], 0.0)


class LeavingCarTests(unittest.TestCase):
    """Telling apart a car that dodged payment from one we simply never saw."""

    def setUp(self):
        self.flow = StubFlow()
        self.state = DashboardState(self.flow, StubClient(), use_db=False)

    def _leaves(self, plate):
        self.state.on_event(event("car_spot_action", CarPlateNumber=plate,
                                  SpotType="ExitSpot", SpotName="EXIT_EXIT",
                                  Direction="CarOut"))
        return self.state.snapshot()["sessions"][0]

    def test_a_car_we_never_saw_arrive_is_not_accused_of_dodging(self):
        # happens after a backend restart: the car was mid-visit already
        visit = self._leaves("ZZZ 999")
        self.assertEqual(visit["status"], "Left (not tracked)")
        self.assertEqual(visit["spot"], "-")
        self.assertEqual(self.state.stats()["cars_served"], 0,
                         "a visit we never handled must not count as served")

    def test_a_tracked_car_that_paid_is_completed(self):
        self.flow.add("AAA 1", "CHARGING", "S6", parked=True)
        self.state.on_event(event("payment_made", CarPlateNumber="AAA 1", Amount="4.00"))
        visit = self._leaves("AAA 1")
        self.assertEqual(visit["status"], "Completed")
        self.assertEqual(self.state.stats()["cars_served"], 1)

    def test_a_tracked_car_that_did_not_pay_is_flagged(self):
        self.flow.add("BBB 2", "CHARGING", "S7", parked=True)
        visit = self._leaves("BBB 2")
        self.assertEqual(visit["status"], "Left without paying")
        self.assertEqual(visit["spot"], "S7")
        self.assertEqual(self.state.stats()["cars_served"], 1)


class RepeatedEventTests(unittest.TestCase):
    """The simulator re-sends events when the webhook is slow to answer."""

    def setUp(self):
        self.flow = StubFlow()
        self.state = DashboardState(self.flow, StubClient(), use_db=False)

    def test_a_repeated_departure_is_not_counted_twice(self):
        self.flow.add("CLT 059", "CHARGING", "S12", parked=True)
        leaving = {"EventClass": "car_spot_action", "CarPlateNumber": "CLT 059",
                   "SpotType": "ExitSpot", "SpotName": "EXIT_EXIT", "Direction": "CarOut",
                   "EventId": "same-id", "ServerDateTime": NOW}

        self.state.on_event(leaving)
        # CarFlow has now forgotten this car, and the simulator sends it again
        del self.flow.cars["CLT 059"]
        self.state.on_event(dict(leaving))

        sessions = self.state.snapshot()["sessions"]
        self.assertEqual(len(sessions), 1, "the repeat must not add a second visit")
        self.assertEqual(sessions[0]["spot"], "S12")
        self.assertEqual(self.state.stats()["cars_served"], 1)

    def test_a_repeated_payment_is_not_counted_twice(self):
        paid = event("payment_made", CarPlateNumber="AAA 1", Amount="4.00")
        self.state.on_event(paid)
        self.state.on_event(dict(paid))
        self.assertEqual(self.state.stats()["revenue"], 4.0)

    def test_a_repeated_arrival_does_not_double_the_arrivals_chart(self):
        arriving = event("car_spot_action", CarPlateNumber="BBB 2", SpotType="EntrySpot",
                         SpotName="ENTRY1", Direction="CarIn")
        self.state.on_event(arriving)
        self.state.on_event(dict(arriving))
        self.assertEqual(len(self.state.arrivals), 1)

    def test_events_without_an_id_are_still_processed(self):
        self.state.on_event({"EventClass": "payment_made", "CarPlateNumber": "CCC 3",
                             "Amount": "2.00", "ServerDateTime": NOW})
        self.state.on_event({"EventClass": "payment_made", "CarPlateNumber": "CCC 3",
                             "Amount": "2.00", "ServerDateTime": NOW})
        self.assertEqual(self.state.stats()["revenue"], 4.0)

    def test_the_seen_list_does_not_grow_for_ever(self):
        for i in range(6000):
            self.state.on_event(event("car_spot_action", CarPlateNumber=f"P {i}",
                                      SpotType="EntrySpot", SpotName="ENTRY1",
                                      Direction="CarIn", EventId=f"id-{i}"))
        self.assertLessEqual(len(self.state._seen_lookup), 5000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
