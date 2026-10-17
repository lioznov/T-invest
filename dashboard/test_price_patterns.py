from datetime import datetime, timedelta, timezone
from unittest import TestCase

from .signals import Candle, analyse
from .market_provider import demo_snapshot
from .price_patterns import scan_patterns, pattern_panel
from .pattern_study import VARIANTS, simulate_pattern


AT = datetime(2026, 9, 28, 7, tzinfo=timezone.utc)  # Monday 10:00 Moscow


def bars(extra):
    warm = [Candle(AT+timedelta(minutes=15*(i-100)), 100, 100.2, 99.8, 100, 100)
            for i in range(100)]
    return warm+[Candle(AT+timedelta(minutes=15*i), *values) for i, values in enumerate(extra)]


class PatternDetectionTests(TestCase):
    def test_reclaim_is_known_only_when_price_returns(self):
        candles = bars([(99.75, 99.9, 99.65, 99.79, 200), (99.79, 100.3, 99.77, 100.1, 200)])
        events = scan_patterns(candles)["reclaim"]
        self.assertIsNone(events[100])
        self.assertIsNotNone(events[101])
        self.assertAlmostEqual(events[101]["level"], 99.8)
        self.assertEqual(events[101]["floor"], 99.65)

    def test_reclaim_expires_without_retroactive_signal(self):
        candles = bars([(99.75, 99.9, 99.65, 99.79, 200),
                        (99.79, 99.85, 99.78, 99.81, 200),
                        (99.81, 99.85, 99.78, 99.81, 200),
                        (99.81, 100.3, 99.79, 100.1, 200)])
        self.assertTrue(all(e is None for e in scan_patterns(candles)["reclaim"]))

    def test_orb_requires_retest_then_another_confirming_candle(self):
        candles = bars([(100, 100.2, 99.8, 100, 100)]*4+
                        [(100, 100.65, 100, 100.5, 200),
                         (100.5, 100.6, 100.18, 100.45, 100),
                         (100.45, 100.85, 100.3, 100.8, 200)])
        events = scan_patterns(candles)["orb_retest"]
        self.assertIsNone(events[104])
        self.assertIsNone(events[105])
        self.assertIsNotNone(events[106])
        self.assertAlmostEqual(events[106]["level"], 100.2)

    def test_missing_opening_bar_prevents_orb(self):
        candles = bars([(100, 100.2, 99.8, 100, 100)]*4+
                        [(100, 100.65, 100, 100.5, 200),
                         (100.5, 100.6, 100.18, 100.45, 100),
                         (100.45, 100.85, 100.3, 100.8, 200)])
        del candles[101]
        self.assertTrue(all(e is None for e in scan_patterns(candles)["orb_retest"]))

    def test_flag_is_sequence_not_just_price_above_average(self):
        candles = bars([(100, 100.6, 99.9, 100.5, 300),
                        (100.5, 101.1, 100.4, 101, 300),
                        (101, 101.6, 100.9, 101.5, 300),
                        (101.5, 101.55, 101.1, 101.3, 100),
                        (101.3, 101.45, 101, 101.2, 100),
                        (101.2, 101.9, 101.1, 101.8, 300)])
        self.assertIsNotNone(scan_patterns(candles)["flag"][105])
        flat = bars([(100, 100.2, 99.8, 100, 100)]*10)
        self.assertTrue(all(e is None for e in scan_patterns(flat)["flag"]))

    def test_events_are_prefix_invariant(self):
        candles = bars([(99.75, 99.9, 99.65, 99.79, 200), (99.79, 100.3, 99.77, 100.1, 200)]
                        +[(100, 110, 99, 109, 90000)]*8)
        full = scan_patterns(candles)
        for n in (101, 102, 105):
            partial = scan_patterns(candles[:n])
            for key in full:
                self.assertEqual(partial[key], full[key][:n])

    def test_incomplete_future_candle_is_not_a_live_signal(self):
        now = AT+timedelta(minutes=5)
        snapshot = demo_snapshot("SBER", now)
        snapshot["candles"] = bars([(99.75, 100.3, 99.65, 100.1, 200)])
        panel = analyse(snapshot, now)["patterns"]
        self.assertFalse(any(item["detected"] for item in panel["items"]))
        self.assertFalse(panel["entry_allowed"])

    def test_stale_chart_clears_current_pattern_and_markers(self):
        snapshot = demo_snapshot("SBER", AT)
        snapshot["candles"] = bars([(99.75, 100.3, 99.65, 100.1, 200)])
        panel = analyse(snapshot, AT+timedelta(hours=3))["patterns"]
        self.assertEqual(panel["status"], "stale")
        self.assertEqual(panel["markers"], [])
        self.assertFalse(panel["market_confirmed"])
        self.assertFalse(panel["entry_allowed"])

    def test_inconsistent_book_blocks_existing_actionable_decision(self):
        snapshot = demo_snapshot("SBER", AT)
        snapshot["books"][-1]["is_consistent"] = False
        analysis = analyse(snapshot, AT)
        self.assertEqual(analysis["decision"], "WAIT")
        self.assertIsNone(analysis["plan"])
        self.assertTrue(any("несогласованным" in reason for reason in analysis["blockers"]))


def execution_fixture(count=2):
    start = AT+timedelta(minutes=30)
    candles = [Candle(start+timedelta(minutes=15*(i-100)), 100, 100.1, 99.9, 100, 100)
               for i in range(100+count)]
    events = {key: [None]*len(candles) for key in ("reclaim", "orb_retest", "flag")}
    events["reclaim"][99] = {"level": 99, "floor": 99, "atr": .4, "price": 100, "rvol": 2}
    return candles, events


class PatternExecutionTests(TestCase):
    def test_six_registered_variants(self):
        self.assertEqual(len(VARIANTS), 6)

    def test_next_open_and_stop_first_when_both_levels_touched(self):
        candles, events = execution_fixture(1)
        c = candles[100]
        candles[100] = Candle(c.time, 100, 103, 98, 102, 100)
        result = simulate_pattern(candles, events, VARIANTS["reclaim_plain"], "2026-09-28", "2026-09-29", 10)
        trade = result["trade_log"][0]
        self.assertEqual(trade["entry_quote"], 100)
        self.assertEqual(trade["reason"], "stop")
        self.assertEqual(trade["shares"] % 10, 0)
        self.assertLess(result["stress_return_pct"], result["return_pct"])

    def test_gap_uses_bad_open_and_can_exceed_planned_loss(self):
        candles, events = execution_fixture(2)
        c = candles[101]
        candles[101] = Candle(c.time, 97, 97, 97, 97, 100)
        result = simulate_pattern(candles, events, VARIANTS["reclaim_plain"], "2026-09-28", "2026-09-29", 10)
        trade = result["trade_log"][0]
        self.assertEqual(trade["exit_quote"], 97)
        self.assertEqual(trade["reason"], "gap_stop")
        self.assertGreater(-trade["pnl"], trade["planned_loss"])

    def test_entry_final_volume_is_not_future_veto(self):
        candles, events = execution_fixture(1)
        c = candles[100]
        candles[100] = Candle(c.time, c.open, c.high, c.low, c.close, 0)
        result = simulate_pattern(candles, events, VARIANTS["reclaim_volume"], "2026-09-28", "2026-09-29", 1)
        self.assertEqual(result["trades"], 1)
        self.assertEqual(result["unverified_trades"], 1)

    def test_missing_bar_invalidates_held_trade(self):
        candles, events = execution_fixture(2)
        c = candles[101]
        candles[101] = Candle(c.time+timedelta(minutes=15), c.open, c.high, c.low, c.close, c.volume)
        result = simulate_pattern(candles, events, VARIANTS["reclaim_plain"], "2026-09-28", "2026-09-29", 1)
        self.assertEqual(result["unverified_trades"], 1)
        self.assertEqual(result["trade_log"][0]["reason"], "missing_data")

    def test_volume_filter_is_based_on_signal_not_entry_candle(self):
        candles, events = execution_fixture(1)
        events["reclaim"][99]["rvol"] = .9
        plain = simulate_pattern(candles, events, VARIANTS["reclaim_plain"], "2026-09-28", "2026-09-29", 1)
        filtered = simulate_pattern(candles, events, VARIANTS["reclaim_volume"], "2026-09-28", "2026-09-29", 1)
        self.assertEqual(plain["trades"], 1)
        self.assertEqual(filtered["trades"], 0)
