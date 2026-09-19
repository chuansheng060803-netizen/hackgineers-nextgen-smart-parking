"""Tests for store.py: the single writer of simulator data.

Fixtures are shaped exactly like the real simulator's answers and webhooks
(recorded 19-20 Sep 2026), including their oddities: string amounts, the plate
of a penalty in ComponentName, "Closed" as a gate action, out-of-order events.
"""
import json
import os
import sqlite3
import tempfile
import unittest

from store import Store, items_from_response, STATE_TABLES


def spot(name="S3", detected=0, **extra):
    return {"name": name, "purpose": "Park", "parkingForCarType": "Any", "zoneParent": "ZONE1",
            "detectedCars": detected, "broken": False, "isUnderMaintenance": False, **extra}


def barrier(name="gateA", state="Closed"):
    return {"name": name, "zoneParent": "ZONE1", "broken": False, "isUnderMaintenance": False,
            "state": state}


def car_event(seq, plate="WBT 862", spot_name="ENTRY1", spot_type="EntrySpot", direction="CarIn",
              event_id=None, minutes="3"):
    return {"EventClass": "car_spot_action", "CarPlateNumber": plate, "CarType": "Normal",
            "SpotName": spot_name, "SpotType": spot_type, "Direction": direction,
            "PlannedParkingDurationInMinutes": minutes, "EventId": event_id or f"ev-{seq}",
            "SequenceId": seq, "Signature": None, "ServerDateTime": "2026-09-19 23:47:20"}


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "test.db")
        self.store = Store(self.path).start()
        self.addCleanup(self.dir.cleanup)
        self.addCleanup(self.store.stop)

    def q(self, sql, *args):
        """Read straight from the file, the way the dashboard will."""
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]
        finally:
            conn.close()

    def settle(self):
        self.assertTrue(self.store.flush(), "writer did not catch up")


class TestSchema(StoreTestCase):
    def test_every_simulator_table_exists(self):
        names = {r["name"] for r in self.q("SELECT name FROM sqlite_master WHERE type='table'")}
        wanted = {"list_parking_spots", "list_barriers", "list_zones", "list_lights", "list_alarms",
                  "status", "webhook_events", "car_spot_action", "payment_made", "penalty",
                  "gate_action", "component_broken", "component_fixed", "carbon_monoxide_event",
                  "state_changes", "poll_status", "ingest_meta"}
        self.assertTrue(wanted <= names, wanted - names)

    def test_columns_keep_the_simulators_own_names(self):
        cols = {r["name"] for r in self.q("PRAGMA table_info(list_parking_spots)")}
        self.assertTrue({"name", "purpose", "parkingForCarType", "zoneParent", "detectedCars",
                         "broken", "isUnderMaintenance"} <= cols)
        cols = {r["name"] for r in self.q("PRAGMA table_info(penalty)")}
        self.assertTrue({"FineAmount", "ComponentName", "Type", "Reason"} <= cols)

    def test_wal_mode_so_the_dashboard_can_read_while_we_write(self):
        self.assertEqual(self.q("PRAGMA journal_mode")[0]["journal_mode"], "wal")

    def test_does_not_touch_the_older_tables(self):
        # The older schema.sql tables are not created or altered by the store.
        names = {r["name"] for r in self.q("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("parking_sessions", names)


class TestWebhookEvents(StoreTestCase):
    def test_car_event_is_stored_with_exact_names_and_values(self):
        self.store.submit_event(car_event(10106))
        self.settle()
        row = self.q("SELECT * FROM car_spot_action")[0]
        self.assertEqual(row["CarPlateNumber"], "WBT 862")
        self.assertEqual(row["SpotName"], "ENTRY1")
        self.assertEqual(row["SpotType"], "EntrySpot")
        self.assertEqual(row["Direction"], "CarIn")
        self.assertEqual(row["PlannedParkingDurationInMinutes"], "3")   # text, as sent
        self.assertEqual(row["SequenceId"], 10106)
        self.assertEqual(row["ServerDateTime"], "2026-09-19 23:47:20")
        self.assertEqual(json.loads(row["raw_json"])["EventId"], "ev-10106")

    def test_same_event_twice_is_stored_once(self):
        event = car_event(1)
        self.store.submit_event(event)
        self.store.submit_event(dict(event))
        self.settle()
        self.assertEqual(len(self.q("SELECT * FROM car_spot_action")), 1)
        self.assertEqual(len(self.q("SELECT * FROM webhook_events")), 1)
        self.assertEqual(self.store.stats["events_duplicate"], 1)

    def test_payment_amount_stays_text(self):
        self.store.submit_event({"EventClass": "payment_made", "CarPlateNumber": "TLT 388",
                                 "Amount": "1.03", "Reason": "Car Payment", "EventId": "p1",
                                 "SequenceId": 5, "Signature": None,
                                 "ServerDateTime": "2026-09-20 00:01:06"})
        self.settle()
        row = self.q("SELECT * FROM payment_made")[0]
        self.assertEqual((row["CarPlateNumber"], row["Amount"], row["Reason"]),
                         ("TLT 388", "1.03", "Car Payment"))

    def test_penalty_keeps_the_plate_in_componentname(self):
        self.store.submit_event({"EventClass": "penalty", "Reason": "Car escaped without paying "
                                 "after parking for some time.", "FineAmount": "10", "Type": "Car",
                                 "ComponentName": "GVR 362", "EventId": "f1", "SequenceId": 3296,
                                 "Signature": None, "ServerDateTime": "2026-09-19 18:54:52"})
        self.settle()
        row = self.q("SELECT * FROM penalty")[0]
        self.assertEqual((row["ComponentName"], row["FineAmount"], row["Type"]),
                         ("GVR 362", "10", "Car"))

    def test_gate_action(self):
        self.store.submit_event({"EventClass": "gate_action", "Name": "gateA", "Action": "Closed",
                                 "EventId": "g1", "SequenceId": 1562, "Signature": None,
                                 "ServerDateTime": "2026-09-19 18:09:17"})
        self.settle()
        row = self.q("SELECT * FROM gate_action")[0]
        self.assertEqual((row["Name"], row["Action"]), ("gateA", "Closed"))

    def test_events_that_arrive_out_of_order_are_all_kept(self):
        for seq in (1087, 1086, 1085):
            self.store.submit_event(car_event(seq))
        self.settle()
        rows = self.q('SELECT "SequenceId" FROM car_spot_action ORDER BY "SequenceId"')
        self.assertEqual([r["SequenceId"] for r in rows], [1085, 1086, 1087])

    def test_unknown_class_is_kept_in_webhook_events_only(self):
        self.store.submit_event({"EventClass": "something_new", "EventId": "x1", "SequenceId": 1,
                                 "Signature": None, "ServerDateTime": "2026-09-20 00:00:00"})
        self.settle()
        self.assertEqual(self.q("SELECT \"EventClass\" FROM webhook_events")[0]["EventClass"],
                         "something_new")

    def test_unconfirmed_level_2_events_keep_everything_in_raw_json(self):
        self.store.submit_event({"EventClass": "component_broken", "EventId": "b1", "SequenceId": 9,
                                 "Signature": None, "ServerDateTime": "2026-09-20 00:00:00",
                                 "ComponentName": "gateB", "Whatever": 7})
        self.settle()
        raw = json.loads(self.q("SELECT raw_json FROM component_broken")[0]["raw_json"])
        self.assertEqual((raw["ComponentName"], raw["Whatever"]), ("gateB", 7))

    def test_event_without_an_id_is_still_stored_once(self):
        event = {"EventClass": "gate_action", "Name": "gateA", "Action": "Open"}
        self.store.submit_event(event)
        self.store.submit_event(dict(event))
        self.settle()
        self.assertEqual(len(self.q("SELECT * FROM gate_action")), 1)

    def test_list_of_events_and_junk_do_not_break_the_writer(self):
        self.store.submit_event([car_event(1), car_event(2)])
        self.store.submit_event("not an object")
        self.store.submit_event(None)
        self.store.submit_event(car_event(3))
        self.settle()
        self.assertEqual(len(self.q("SELECT * FROM car_spot_action")), 3)

    def test_last_event_time_is_recorded(self):
        self.store.submit_event(car_event(1), received_at="2026-09-20T00:00:00.000+00:00")
        self.settle()
        meta = {r["key"]: r["value"] for r in self.q("SELECT * FROM ingest_meta")}
        self.assertEqual(meta["last_event_at"], "2026-09-20T00:00:00.000+00:00")


class TestRestState(StoreTestCase):
    def test_first_answer_creates_rows_and_logs_starting_values(self):
        self.store.submit_state("list-parking-spots", [spot("S1"), spot("S2", detected=1)])
        self.settle()
        rows = self.q("SELECT * FROM list_parking_spots ORDER BY name")
        self.assertEqual([r["name"] for r in rows], ["S1", "S2"])
        self.assertEqual(rows[1]["detectedCars"], 1)
        self.assertEqual(rows[0]["broken"], 0)                       # False stored as 0
        self.assertEqual(rows[0]["parkingForCarType"], "Any")
        self.assertTrue(rows[0]["first_seen_at"])

    def test_same_answer_again_writes_nothing(self):
        for _ in range(3):
            self.store.submit_state("list-parking-spots", [spot("S1"), spot("S2")])
        self.settle()
        first = self.q("SELECT updated_at FROM list_parking_spots ORDER BY name")
        changes = self.q("SELECT COUNT(*) n FROM state_changes")[0]["n"]
        for _ in range(3):
            self.store.submit_state("list-parking-spots", [spot("S1"), spot("S2")])
        self.settle()
        self.assertEqual(self.q("SELECT updated_at FROM list_parking_spots ORDER BY name"), first)
        self.assertEqual(self.q("SELECT COUNT(*) n FROM state_changes")[0]["n"], changes)
        self.assertEqual(self.store.stats["state_rows_changed"], 2)

    def test_a_change_updates_only_that_row_and_logs_the_field(self):
        self.store.submit_state("list-parking-spots", [spot("S1"), spot("S2")])
        self.settle()
        before = self.q("SELECT name, updated_at FROM list_parking_spots ORDER BY name")
        self.store.submit_state("list-parking-spots", [spot("S1", detected=1), spot("S2")])
        self.settle()
        after = self.q("SELECT name, updated_at, detectedCars FROM list_parking_spots ORDER BY name")
        self.assertEqual(after[0]["detectedCars"], 1)
        self.assertEqual(after[1]["updated_at"], before[1]["updated_at"])   # S2 untouched
        log = self.q("SELECT * FROM state_changes WHERE field = 'detectedCars' AND \"name\" = 'S1' "
                     "ORDER BY id")
        self.assertEqual([(r["old_value"], r["new_value"]) for r in log], [(None, "0"), ("0", "1")])
        self.assertEqual(log[-1]["endpoint"], "list-parking-spots")

    def test_barrier_state_change_is_logged(self):
        self.store.submit_state("list-barriers", [barrier("gateA", "Closed")])
        self.store.submit_state("list-barriers", [barrier("gateA", "Open")])
        self.settle()
        self.assertEqual(self.q("SELECT state FROM list_barriers")[0]["state"], "Open")
        log = self.q("SELECT old_value, new_value FROM state_changes WHERE field='state' ORDER BY id")
        self.assertEqual([(r["old_value"], r["new_value"]) for r in log],
                         [(None, "Closed"), ("Closed", "Open")])

    def test_zones_and_lights_use_the_given_names(self):
        self.store.submit_state("list-zones", [{"name": "ZONE1", "gasCarbonMonoxideLevel": 0,
                                                "risk": "Safe"}])
        self.store.submit_state("list-lights", [{"name": "t_0", "group": "G1", "zoneParent": "ZONE1",
                                                 "isOn": True}])
        self.settle()
        self.assertEqual(self.q("SELECT * FROM list_zones")[0]["risk"], "Safe")
        light = self.q('SELECT "group", "isOn" FROM list_lights')[0]
        self.assertEqual((light["group"], light["isOn"]), ("G1", 1))

    def test_status_is_one_row(self):
        self.store.submit_state("status", {"isActive": False, "cars": 5})
        self.store.submit_state("status", {"isActive": True, "cars": 6})
        self.settle()
        rows = self.q("SELECT * FROM status")
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["id"], rows[0]["isActive"], rows[0]["cars"]), (1, 1, 6))

    def test_extra_unknown_fields_are_not_lost(self):
        self.store.submit_state("list-parking-spots", [spot("S1", newField="hello")])
        self.settle()
        raw = json.loads(self.q("SELECT raw_json FROM list_parking_spots")[0]["raw_json"])
        self.assertEqual(raw["newField"], "hello")

    def test_alarms_appear_and_disappear(self):
        self.store.submit_state("list-alarms", [{"name": "CO high", "level": 3}])
        self.settle()
        self.assertEqual([r["name"] for r in self.q("SELECT name FROM list_alarms")], ["CO high"])
        self.store.submit_state("list-alarms", [])
        self.settle()
        self.assertEqual(self.q("SELECT * FROM list_alarms"), [])

    def test_alarm_without_a_name_gets_a_stable_key(self):
        alarm = {"kind": "smoke", "zone": "ZONE1"}
        self.store.submit_state("list-alarms", [alarm])
        self.store.submit_state("list-alarms", [dict(alarm)])
        self.settle()
        self.assertEqual(len(self.q("SELECT * FROM list_alarms")), 1)

    def test_a_restart_does_not_relog_unchanged_state(self):
        self.store.submit_state("list-parking-spots", [spot("S1"), spot("S2")])
        self.settle()
        n = self.q("SELECT COUNT(*) n FROM state_changes")[0]["n"]
        self.store.stop()
        again = Store(self.path).start()
        self.addCleanup(again.stop)
        again.submit_state("list-parking-spots", [spot("S1"), spot("S2")])
        self.assertTrue(again.flush())
        self.assertEqual(self.q("SELECT COUNT(*) n FROM state_changes")[0]["n"], n)
        again.submit_state("list-parking-spots", [spot("S1", detected=1), spot("S2")])
        self.assertTrue(again.flush())
        self.assertEqual(self.q("SELECT COUNT(*) n FROM state_changes")[0]["n"], n + 1)

    def test_an_impossible_answer_is_a_poll_error_not_an_empty_list(self):
        self.store.submit_state("list-parking-spots", [spot("S1")])
        self.store.submit_state("list-parking-spots", None)          # empty body
        self.store.submit_state("list-parking-spots", "oops")
        self.settle()
        self.assertEqual([r["name"] for r in self.q("SELECT name FROM list_parking_spots")], ["S1"])
        status = self.q("SELECT * FROM poll_status WHERE endpoint = 'list-parking-spots'")[0]
        self.assertEqual(status["error_count"], 2)
        self.assertIn("expected a list", status["last_error"])

    def test_items_that_are_not_objects_are_skipped(self):
        self.store.submit_state("list-parking-spots", ["junk", spot("S1"), {"purpose": "no name"}])
        self.settle()
        self.assertEqual([r["name"] for r in self.q("SELECT name FROM list_parking_spots")], ["S1"])

    def test_an_error_body_never_looks_like_an_empty_list(self):
        self.store.submit_state("list-alarms", [{"name": "CO high"}])
        self.store.submit_state("list-alarms", {"error": "boom", "details": []})
        self.store.submit_state("list-barriers", {"barriers": [barrier("gateB", "Open")]})
        self.settle()
        self.assertEqual([r["name"] for r in self.q("SELECT name FROM list_alarms")], ["CO high"])
        self.assertEqual(self.q("SELECT * FROM list_barriers"), [])
        self.assertEqual(self.q("SELECT error_count FROM poll_status WHERE endpoint='list-alarms'")[0]
                         ["error_count"], 1)

    def test_an_alarm_whose_content_changes_is_updated(self):
        self.store.submit_state("list-alarms", [{"name": "A1", "active": False}])
        self.store.submit_state("list-alarms", [{"name": "A1", "active": True}])
        self.settle()
        self.assertEqual(self.store.stats["job_errors"], 0)
        self.assertTrue(json.loads(self.q("SELECT raw_json FROM list_alarms")[0]["raw_json"])["active"])

    def test_unknown_endpoint_is_ignored(self):
        self.store.submit_state("list-nothing", [])
        self.settle()


class TestFreshness(StoreTestCase):
    def test_success_records_when_the_endpoint_was_last_read(self):
        self.store.submit_state("list-zones", [{"name": "ZONE1", "gasCarbonMonoxideLevel": 0,
                                                "risk": "Safe"}], duration_ms=12)
        self.settle()
        row = self.q("SELECT * FROM poll_status WHERE endpoint='list-zones'")[0]
        self.assertTrue(row["last_ok_at"])
        self.assertEqual((row["ok_count"], row["error_count"], row["item_count"],
                          row["last_duration_ms"]), (1, 0, 1, 12))

    def test_error_then_recovery(self):
        self.store.submit_poll_error("list-zones", "Cannot reach simulator", 30)
        self.settle()
        row = self.q("SELECT * FROM poll_status WHERE endpoint='list-zones'")[0]
        self.assertEqual((row["error_count"], row["last_error"]), (1, "Cannot reach simulator"))
        self.store.submit_state("list-zones", [{"name": "ZONE1", "gasCarbonMonoxideLevel": 0,
                                                "risk": "Safe"}])
        self.settle()
        row = self.q("SELECT * FROM poll_status WHERE endpoint='list-zones'")[0]
        self.assertIsNone(row["last_error"])                # cleared at once on recovery
        self.assertTrue(row["last_error_at"])               # but the history is kept
        self.assertEqual((row["ok_count"], row["error_count"]), (1, 1))

    def test_repeated_success_does_not_rewrite_status_every_time(self):
        for _ in range(50):
            self.store.submit_state("status", {"isActive": True, "cars": 1})
        self.settle()
        # 50 reads inside one batch: one freshness write, and only one state row change.
        self.assertEqual(self.store.stats["state_rows_changed"], 1)
        self.assertEqual(self.q("SELECT ok_count FROM poll_status WHERE endpoint='status'")[0]["ok_count"], 1)


class TestWriterSafety(StoreTestCase):
    def test_stop_writes_everything_still_queued(self):
        for seq in range(1, 501):
            self.store.submit_event(car_event(seq))
        self.store.stop()
        self.assertEqual(self.q("SELECT COUNT(*) n FROM car_spot_action")[0]["n"], 500)

    def test_a_bad_job_is_skipped_and_the_rest_still_written(self):
        original = self.store._apply_event

        def flaky(conn, event, when):
            if event["EventId"] == "ev-2":
                raise RuntimeError("boom")
            return original(conn, event, when)

        self.store._apply_event = flaky
        for seq in (1, 2, 3):
            self.store.submit_event(car_event(seq))
        self.settle()
        ids = [r["EventId"] for r in self.q('SELECT "EventId" FROM car_spot_action ORDER BY "EventId"')]
        self.assertEqual(ids, ["ev-1", "ev-3"])
        self.assertEqual(self.store.stats["job_errors"], 1)

    def test_a_failed_state_job_leaves_no_false_cache(self):
        real = self.store._poll_ok
        calls = {"n": 0}

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom after the row was written")
            return real(*args, **kwargs)

        self.store._poll_ok = flaky
        self.store.submit_state("list-parking-spots", [spot("S1")])
        self.settle()
        self.assertEqual(self.q("SELECT * FROM list_parking_spots"), [])        # rolled back
        self.store.submit_state("list-parking-spots", [spot("S1")])             # same answer again
        self.settle()
        self.assertEqual(len(self.q("SELECT * FROM list_parking_spots")), 1)    # written this time

    def test_many_threads_submitting_at_once(self):
        import threading

        def worker(base):
            for i in range(100):
                self.store.submit_event(car_event(base + i, event_id=f"t{base}-{i}"))

        threads = [threading.Thread(target=worker, args=(b * 1000,)) for b in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.settle()
        self.assertEqual(self.q("SELECT COUNT(*) n FROM car_spot_action")[0]["n"], 800)

    def test_reading_while_writing_does_not_block(self):
        for seq in range(1, 301):
            self.store.submit_event(car_event(seq))
        for _ in range(20):
            self.q("SELECT COUNT(*) n FROM car_spot_action")      # dashboard-style reads mid-write
        self.settle()
        self.assertEqual(self.q("SELECT COUNT(*) n FROM car_spot_action")[0]["n"], 300)


class TestWriterSurvives(StoreTestCase):
    """Regressions found by the code review: nothing may kill the writer or lose a flush."""

    def test_a_failed_batch_is_retried_and_flush_is_still_answered(self):
        real = self.store._write_batch
        calls = {"n": 0}

        def flaky(conn, work):
            calls["n"] += 1
            if calls["n"] == 1:
                raise sqlite3.OperationalError("database is locked")
            return real(conn, work)

        self.store._write_batch = flaky
        for seq in (1, 2, 3):
            self.store.submit_event(car_event(seq))
        self.settle()                                            # flush() must not time out
        self.assertEqual(self.q("SELECT COUNT(*) n FROM car_spot_action")[0]["n"], 3)

    def test_a_batch_that_keeps_failing_still_answers_flush_and_writer_lives(self):
        real = self.store._write_batch
        self.store._write_batch = lambda conn, work: (_ for _ in ()).throw(RuntimeError("disk full"))
        self.store.submit_event(car_event(1))
        self.assertTrue(self.store.flush(timeout=10))
        self.assertTrue(self.store.is_alive())
        self.store._write_batch = real                           # the disk recovers
        self.store.submit_event(car_event(2))
        self.settle()
        self.assertEqual([r["SequenceId"] for r in self.q("SELECT SequenceId FROM car_spot_action")], [2])

    def test_a_cache_reload_failure_does_not_kill_the_writer(self):
        real = self.store._load_cache
        state = {"boom": True}

        def flaky(conn):
            if state["boom"]:
                state["boom"] = False
                raise RuntimeError("cannot reload")
            return real(conn)

        self.store._load_cache = flaky
        original = self.store._apply_event
        self.store._apply_event = lambda c, e, w: (_ for _ in ()).throw(RuntimeError("bad job")) \
            if e["EventId"] == "ev-1" else original(c, e, w)
        self.store.submit_event(car_event(1))
        self.store.submit_event(car_event(2))
        self.settle()
        self.assertTrue(self.store.is_alive())
        self.assertEqual(self.q("SELECT COUNT(*) n FROM car_spot_action")[0]["n"], 1)

    def test_another_program_committing_at_the_same_time_loses_nothing(self):
        import threading
        import time
        ready, made = threading.Event(), []

        def other_program():                                    # the older backend writing to its own tables
            other = sqlite3.connect(self.path, timeout=15)      # its own connection, made in this thread
            other.execute("CREATE TABLE IF NOT EXISTS foreign_t (x INTEGER)")
            other.commit()
            ready.set()
            for _ in range(150):
                other.execute("INSERT INTO foreign_t VALUES (1)")
                other.commit()
                made.append(1)
                time.sleep(0.002)
            other.close()

        thread = threading.Thread(target=other_program)
        thread.start()
        self.assertTrue(ready.wait(5))
        for seq in range(1, 601):
            self.store.submit_event(car_event(seq))
            if seq % 100 == 0:
                self.store.submit_event(car_event(seq, event_id="poison-%d" % seq))
            if seq % 20 == 0:
                time.sleep(0.004)                               # interleave with the other program's commits
        self.settle()
        thread.join()
        self.assertEqual(len(made), 150)                        # it really did write in between
        self.assertEqual(self.q("SELECT COUNT(*) n FROM car_spot_action")[0]["n"], 606)
        self.assertEqual(self.q("SELECT COUNT(*) n FROM foreign_t")[0]["n"], 150)
        self.assertEqual(self.store.stats["job_errors"], 0)

    def test_after_stop_new_data_is_refused_not_silently_queued(self):
        self.store.stop()
        self.store.submit_event(car_event(1))
        self.assertEqual(self.store.backlog(), 0)
        self.assertFalse(self.store.flush())
        self.assertFalse(self.store.is_alive())

    def test_can_start_again_after_a_stop(self):
        self.store.stop()
        self.store.start()
        self.store.submit_event(car_event(1))
        self.settle()
        self.assertEqual(len(self.q("SELECT * FROM car_spot_action")), 1)


class TestItemsFromResponse(unittest.TestCase):
    def test_rules(self):
        spec = STATE_TABLES["list-parking-spots"]
        self.assertEqual(items_from_response(spec, []), [])
        with self.assertRaises(ValueError):
            items_from_response(spec, None)
        with self.assertRaises(ValueError):
            items_from_response(STATE_TABLES["status"], [])


if __name__ == "__main__":
    unittest.main()
