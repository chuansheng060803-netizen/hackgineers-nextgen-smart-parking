"""Tests for biz_metrics.py and forecast.py with small hand-made tables."""
import unittest

import numpy as np
import pandas as pd

import biz_metrics as bm
import forecast as fc


def ev(seq, plate, spot, spot_type, direction, time, car_type="Normal"):
    return {"SequenceId": seq, "ServerDateTime": time, "CarPlateNumber": plate, "CarType": car_type,
            "SpotName": spot, "SpotType": spot_type, "Direction": direction}


def one_visit(plate="AAA 111", base=100, t0="2026-09-20 10:00:00", spot="S1"):
    t = pd.Timestamp(t0)
    def at(m): return (t + pd.Timedelta(minutes=m)).strftime(bm.TIME_FORMAT)
    return [ev(base + 1, plate, "ENTRY1", "EntrySpot", "CarIn", at(0)),
            ev(base + 2, plate, "ENTRY1", "EntrySpot", "CarOut", at(1)),
            ev(base + 3, plate, spot, "Park", "CarIn", at(2)),
            ev(base + 4, plate, spot, "Park", "CarOut", at(12)),
            ev(base + 5, plate, "EXIT_EXIT", "ExitSpot", "CarIn", at(12)),
            ev(base + 6, plate, "EXIT_EXIT", "ExitSpot", "CarOut", at(13))]


def by_time(rows):
    """Number the events in time order, the way the simulator's SequenceId runs."""
    ordered = sorted(rows, key=lambda r: r["ServerDateTime"])
    return [dict(r, SequenceId=i + 1) for i, r in enumerate(ordered)]


class TestPeriods(unittest.TestCase):
    def test_presets_follow_the_newest_data(self):
        p = bm.presets("2026-09-20 00:01:28")
        self.assertEqual(p["Today"], (pd.Timestamp("2026-09-20"), pd.Timestamp("2026-09-21")))
        self.assertEqual(p["Yesterday"][0], pd.Timestamp("2026-09-19"))
        self.assertEqual(p["Last 7 days"][0], pd.Timestamp("2026-09-14"))
        self.assertEqual(p["This month"], (pd.Timestamp("2026-09-01"), pd.Timestamp("2026-10-01")))

    def test_bucket_size_follows_the_length_of_the_period(self):
        d = pd.Timestamp("2026-09-01")
        self.assertEqual(bm.auto_group(d, d + pd.Timedelta(hours=3)), "15 minutes")
        self.assertEqual(bm.auto_group(d, d + pd.Timedelta(days=1)), "Hour")
        self.assertEqual(bm.auto_group(d, d + pd.Timedelta(days=30)), "Day")
        self.assertEqual(bm.auto_group(d, d + pd.Timedelta(days=200)), "Month")

    def test_buckets_cover_the_period_and_the_end_is_not_included(self):
        s = bm.bucket_starts("2026-09-20", "2026-09-21", "h")
        self.assertEqual((len(s), s[0], s[-1]), (24, pd.Timestamp("2026-09-20 00:00"), pd.Timestamp("2026-09-20 23:00")))
        m = bm.bucket_starts("2026-01-15", "2026-04-01", "MS")
        self.assertEqual(list(m), [pd.Timestamp("2026-01-01"), pd.Timestamp("2026-02-01"), pd.Timestamp("2026-03-01")])

    def test_previous_period_is_the_same_length_just_before(self):
        self.assertEqual(bm.previous_period("2026-09-20", "2026-09-21"),
                         (pd.Timestamp("2026-09-19"), pd.Timestamp("2026-09-20")))


class TestBuckets(unittest.TestCase):
    def setUp(self):
        self.df = pd.DataFrame({"time": pd.to_datetime(["2026-09-20 10:05", "2026-09-20 10:50", "2026-09-20 12:10",
                                                        "2026-09-21 00:00"]),
                                "amount": [1.0, 2.0, 4.0, 99.0]})

    def test_sums_per_hour_with_zero_for_quiet_hours_and_excludes_the_end(self):
        s = bm.sum_by_bucket(self.df, "time", "amount", "2026-09-20 09:00", "2026-09-20 13:00", "h")
        self.assertEqual(list(s.values), [0.0, 3.0, 0.0, 4.0])

    def test_nothing_at_all_gives_zeros_not_an_error(self):
        s = bm.sum_by_bucket(pd.DataFrame(columns=["time", "amount"]), "time", "amount", "2026-09-20", "2026-09-21", "h")
        self.assertEqual((len(s), s.sum()), (24, 0.0))

    def test_month_buckets(self):
        df = pd.DataFrame({"time": pd.to_datetime(["2026-01-31", "2026-02-01"]), "amount": [1.0, 2.0]})
        s = bm.sum_by_bucket(df, "time", "amount", "2026-01-01", "2026-03-01", "MS")
        self.assertEqual(list(s.values), [1.0, 2.0])

    def test_count_by_bucket(self):
        s = bm.count_by_bucket(self.df, "time", "2026-09-20 09:00", "2026-09-20 13:00", "h")
        self.assertEqual(list(s.values), [0.0, 2.0, 0.0, 1.0])


class TestVisits(unittest.TestCase):
    def test_one_full_visit(self):
        v = bm.build_visits(pd.DataFrame(one_visit()))
        self.assertEqual(len(v), 1)
        row = v.iloc[0]
        self.assertEqual((row["plate"], row["spot"], row["state"]), ("AAA 111", "S1", "Left"))
        self.assertAlmostEqual(row["minutes_parked"], 10.0)

    def test_same_plate_twice_is_two_visits(self):
        rows = one_visit(base=100) + one_visit(base=200, t0="2026-09-20 11:00:00")
        self.assertEqual(len(bm.build_visits(pd.DataFrame(rows))), 2)

    def test_out_of_order_events_are_put_right_by_sequence_id(self):
        rows = one_visit()
        rows[2], rows[3] = rows[3], rows[2]
        v = bm.build_visits(pd.DataFrame(rows))
        self.assertAlmostEqual(v.iloc[0]["minutes_parked"], 10.0)

    def test_a_visit_whose_exit_was_missed_is_labelled_so_once_the_data_has_moved_on(self):
        rows = one_visit()[:3] + one_visit("LATE 999", base=500, t0="2026-09-20 15:00:00")
        states = dict(zip(*[bm.build_visits(pd.DataFrame(rows))[c] for c in ("plate", "state")]))
        self.assertEqual(states["AAA 111"], "No exit seen")
        self.assertEqual(states["LATE 999"], "Left")

    def test_a_car_still_parked_is_inside(self):
        v = bm.build_visits(pd.DataFrame(one_visit()[:3]))
        self.assertEqual(v.iloc[0]["state"], "Parked")

    def test_events_that_start_mid_visit_do_not_crash(self):
        v = bm.build_visits(pd.DataFrame(one_visit()[3:]))
        self.assertEqual(len(v), 1)

    def test_no_events(self):
        self.assertTrue(bm.build_visits(pd.DataFrame()).empty)

    def test_a_bill_is_matched_to_its_visit(self):
        v = bm.build_visits(pd.DataFrame(one_visit()))
        charges = pd.DataFrame({"CarPlateNumber": ["AAA 111", "BBB 222"], "billed_total": [10.0, 5.0],
                                "billed_at": pd.to_datetime(["2026-09-20 10:12:05", "2026-09-20 10:12:06"]),
                                "status": ["paid", "paid"]})
        out = bm.attach_charges(v, charges)
        self.assertEqual((out.iloc[0]["billed"], out.iloc[0]["payment"]), (10.0, "Paid"))

    def test_an_unpaid_bill_shows_as_not_paid_and_no_charges_is_fine(self):
        v = bm.build_visits(pd.DataFrame(one_visit()))
        charges = pd.DataFrame({"CarPlateNumber": ["AAA 111"], "billed_total": [10.0],
                                "billed_at": pd.to_datetime(["2026-09-20 10:12:05"]), "status": ["billed"]})
        self.assertEqual(bm.attach_charges(v, charges).iloc[0]["payment"], "Not paid")
        self.assertTrue(pd.isna(bm.attach_charges(v, pd.DataFrame()).iloc[0]["billed"]))


class TestOccupancy(unittest.TestCase):
    def test_curve_counts_cars_in_bays(self):
        rows = by_time(one_visit(base=100) + one_visit("BBB 222", base=200, t0="2026-09-20 10:05:00", spot="S2"))
        curve = bm.occupancy_curve(pd.DataFrame(rows))
        self.assertEqual(int(curve.max()), 2)
        self.assertEqual(int(curve.iloc[-1]), 0)

    def test_peak_per_bucket_as_percent_and_carried_forward(self):
        rows = by_time(one_visit(base=100) + one_visit("BBB 222", base=200, t0="2026-09-20 10:05:00", spot="S2"))
        curve = bm.occupancy_curve(pd.DataFrame(rows))
        peak = bm.occupancy_by_bucket(curve, 4, "2026-09-20 09:00", "2026-09-20 12:00", "h")
        self.assertTrue(np.isnan(peak.iloc[0]))            # before any data: unknown, not 0
        self.assertEqual(peak.iloc[1], 50.0)               # 2 of 4 bays at the 10:00 hour peak
        self.assertEqual(peak.iloc[2], 0.0)                # everyone gone by 11:00

    def test_buckets_after_the_newest_data_are_unknown_not_a_flat_line(self):
        curve = bm.occupancy_curve(pd.DataFrame(by_time(one_visit(spot="S1"))))
        peak = bm.occupancy_by_bucket(curve, 4, "2026-09-20 09:00", "2026-09-20 14:00", "h", until="2026-09-20 10:13:00")
        self.assertFalse(np.isnan(peak.iloc[1]))                 # 10:00 hour: data exists
        self.assertTrue(np.isnan(peak.iloc[2:]).all())           # 11:00 onwards: the future

    def test_a_missed_car_out_does_not_make_occupancy_drift_up(self):
        rows = [ev(1, "A", "S1", "Park", "CarIn", "2026-09-20 10:00:00"),      # A's CarOut was never recorded
                ev(2, "B", "S1", "Park", "CarIn", "2026-09-20 10:05:00"),      # B now sits in the same bay
                ev(3, "B", "S1", "Park", "CarOut", "2026-09-20 10:09:00")]
        curve = bm.occupancy_curve(pd.DataFrame(rows))
        self.assertEqual(list(curve.values), [1.0, 1.0, 0.0])

    def test_occupancy_can_never_exceed_the_bays(self):
        rows = [ev(i, f"C{i}", f"S{i % 3}", "Park", "CarIn", f"2026-09-20 10:{i:02d}:00") for i in range(1, 30)]
        self.assertLessEqual(bm.occupancy_curve(pd.DataFrame(rows)).max(), 3)

    def test_starting_mid_stay_never_goes_negative(self):
        rows = [ev(1, "A", "S1", "Park", "CarOut", "2026-09-20 10:00:00"),
                ev(2, "B", "S2", "Park", "CarIn", "2026-09-20 10:01:00")]
        self.assertGreaterEqual(bm.occupancy_curve(pd.DataFrame(rows)).min(), 0)


class TestActiveHours(unittest.TestCase):
    def test_only_hours_with_events_count_as_operating(self):
        times = pd.Series(pd.to_datetime(["2026-09-20 10:05", "2026-09-20 10:40", "2026-09-20 13:01"]))
        self.assertEqual(list(bm.active_hours(times)), [pd.Timestamp("2026-09-20 10:00"), pd.Timestamp("2026-09-20 13:00")])
        self.assertEqual(len(bm.active_hours(pd.Series([], dtype="datetime64[ns]"))), 0)


class TestSummary(unittest.TestCase):
    def test_headline_numbers(self):
        income = pd.DataFrame({"time": pd.to_datetime(["2026-09-20 10:00", "2026-09-20 11:00", "2026-09-19 10:00"]),
                               "amount": [2.0, 3.0, 100.0]})
        pen = pd.DataFrame({"time": pd.to_datetime(["2026-09-20 10:30"]), "amount": [10.0]})
        v = bm.build_visits(pd.DataFrame(one_visit()))
        s = bm.summarize(income, pen, v, "2026-09-20", "2026-09-21")
        # a penalty costs 5 credits whatever fine the simulator prints, and never reduces the income
        self.assertEqual((s["income"], s["paying_cars"], s["penalty_count"], s["credits_lost"]), (5.0, 2, 1, 5))
        self.assertAlmostEqual(s["income_per_car"], 2.5)
        self.assertAlmostEqual(s["avg_stay_minutes"], 10.0)

    def test_three_errors_cost_fifteen_credits(self):
        pen = pd.DataFrame({"time": pd.to_datetime(["2026-09-20 10:00", "2026-09-20 10:05", "2026-09-20 23:59", "2026-09-21 00:00"]),
                            "amount": [10.0, 10.0, 10.0, 10.0]})
        s = bm.summarize(pd.DataFrame(), pen, pd.DataFrame(), "2026-09-20", "2026-09-21")
        self.assertEqual((s["penalty_count"], s["credits_lost"]), (3, 15))

    def test_empty_period_is_all_zero(self):
        s = bm.summarize(pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), "2026-09-20", "2026-09-21")
        self.assertEqual((s["income"], s["credits_lost"], s["income_per_car"]), (0.0, 0, 0.0))

    def test_change_percent(self):
        self.assertEqual(bm.change_percent(150, 100), 50.0)
        self.assertIsNone(bm.change_percent(5, 0))

    def test_best_bucket(self):
        s = pd.Series([1.0, 5.0, 2.0], index=pd.date_range("2026-09-20", periods=3, freq="h"))
        self.assertEqual(bm.best_bucket(s), (pd.Timestamp("2026-09-20 01:00"), 5.0))
        self.assertIsNone(bm.best_bucket(pd.Series([0.0, 0.0])))


class TestForecast(unittest.TestCase):
    def hourly(self, values, start="2026-09-10"):
        return pd.Series(values, index=pd.date_range(start, periods=len(values), freq="h"), dtype=float)

    def test_too_little_data_gives_no_forecast_and_says_why(self):
        frame, info = fc.forecast_hourly(self.hourly([1, 2]))
        self.assertTrue(frame.empty)
        self.assertIn("at least", info["note"])

    def test_short_history_carries_the_recent_level_flat(self):
        frame, info = fc.forecast_hourly(self.hourly([5] * 10), horizon_hours=6)
        self.assertEqual(info["method"], "recent level")
        self.assertEqual(len(frame), 6)
        self.assertTrue(np.allclose(frame["forecast"], 5.0))
        self.assertEqual(frame["time"].iloc[0], pd.Timestamp("2026-09-10 10:00"))

    def test_a_daily_rhythm_is_learned_from_two_or_more_days(self):
        day = [0] * 8 + [10] * 8 + [2] * 8                 # quiet night, busy day, calm evening
        frame, info = fc.forecast_hourly(self.hourly(day * 4), horizon_hours=24)
        self.assertEqual(info["method"], "daily pattern")
        self.assertTrue(np.allclose(frame["forecast"].to_numpy(), day))
        self.assertTrue(np.allclose(frame["high"] - frame["low"], 0))

    def test_a_busier_last_day_lifts_the_forecast(self):
        day = [1] * 24
        history = day * 3 + [2] * 24
        frame, _ = fc.forecast_hourly(self.hourly(history), horizon_hours=3)
        self.assertGreater(frame["forecast"].iloc[0], 1.0)

    def test_range_is_never_below_zero_and_wraps_the_forecast(self):
        rng = np.random.default_rng(1)
        frame, _ = fc.forecast_hourly(self.hourly(rng.integers(0, 8, size=100)), horizon_hours=12)
        self.assertTrue((frame["low"] >= 0).all())
        self.assertTrue(((frame["low"] <= frame["forecast"]) & (frame["forecast"] <= frame["high"])).all())

    def test_hours_the_park_was_closed_are_left_out_not_counted_as_zero(self):
        idx = pd.DatetimeIndex(["2026-09-10 10:00", "2026-09-10 11:00", "2026-09-10 12:00", "2026-09-11 10:00"])
        y = pd.Series([6.0, 6.0, 6.0, 6.0], index=idx)
        frame, info = fc.forecast_hourly(y, 2, fill_gaps=False)
        self.assertEqual(info["history_hours"], 4)
        self.assertTrue(np.allclose(frame["forecast"], 6.0))       # not dragged down by the closed hours

    def test_missing_hours_count_as_zero(self):
        s = self.hourly([4, 4, 4, 4])
        gappy = s.drop(s.index[1:3])
        self.assertEqual(len(fc.complete_hours(gappy)), 4)
        self.assertEqual(fc.complete_hours(gappy).iloc[1], 0.0)


if __name__ == "__main__":
    unittest.main()
