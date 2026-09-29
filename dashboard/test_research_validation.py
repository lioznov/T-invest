"""Checks for chronological research splits, execution timing and cost sensitivity."""
from datetime import datetime, timedelta, timezone
from math import prod
from unittest import TestCase
from unittest.mock import patch

from .signals import Candle, backtest, chronological_validation, features, simulate


START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def flat_candles(count):
    return [Candle(START+timedelta(minutes=15*i), 100, 100, 100, 100, 100)
            for i in range(count)]


class ResearchValidationTests(TestCase):
    def test_windows_are_disjoint_cover_evaluation_and_expand_prior_context(self):
        candles = flat_candles(1003)
        result = chronological_validation(candles)
        self.assertEqual(result["status"], "ok")
        windows = result["windows"]
        self.assertEqual(len(windows), 4)
        self.assertEqual(windows[0]["start_index"], len(candles)//5)
        self.assertEqual(windows[-1]["end_index"], len(candles))
        evaluated = []
        for window in windows:
            start, end = window["start_index"], window["end_index"]
            self.assertGreaterEqual(end-start, 100)
            self.assertEqual(window["history_count"], start)
            self.assertEqual(window["start"], candles[start].time.isoformat())
            self.assertEqual(window["end"], candles[end-1].time.isoformat())
            evaluated.extend(range(start, end))
        self.assertEqual(evaluated, list(range(windows[0]["start_index"], len(candles))))
        self.assertEqual(len(evaluated), len(set(evaluated)))

    def test_small_history_keeps_original_comparison_but_requests_more_validation_data(self):
        result = backtest(flat_candles(499))
        self.assertEqual(result["status"], "ok")
        self.assertTrue(all("development" in row and "holdout" in row for row in result["strategies"]))
        self.assertEqual(result["validation"]["status"], "insufficient")
        self.assertEqual(result["validation"]["windows"], [])
        self.assertEqual(chronological_validation(flat_candles(500))["status"], "ok")

    def test_future_price_changes_cannot_change_finished_window(self):
        candles = [Candle(START+timedelta(minutes=15*i), 100+i*.02,
                          100.15+i*.02, 99.95+i*.02, 100.1+i*.02, 100+i%7*30)
                   for i in range(600)]
        before = chronological_validation(candles)
        boundary = before["windows"][0]["end_index"]
        changed = candles[:boundary]+[
            Candle(c.time, c.open*5, c.high*6, c.low*.5, c.close*4, c.volume*20)
            for c in candles[boundary:]]
        after = chronological_validation(changed)
        for left, right in zip(before["strategies"], after["strategies"]):
            self.assertEqual(left["windows"][0], right["windows"][0])
        self.assertEqual(before["windows"][0], after["windows"][0])

    def test_signal_from_close_can_only_fill_at_next_open(self):
        candles = flat_candles(100)+[
            Candle(START+timedelta(minutes=1500), 100, 110, 100, 110, 100),
            Candle(START+timedelta(minutes=1515), 120, 120, 120, 120, 100),
        ]
        with patch("dashboard.signals.candle_signal", side_effect=lambda c, f, strategy: c.close > 100):
            result = simulate(candles, "trend", 100, 102, 0, 0)
        self.assertEqual(result["trades"], 1)
        self.assertEqual(result["recent_trades"][0]["entry_time"], candles[101].time.isoformat())
        self.assertEqual(result["return_pct"], 0)

    def test_stress_uses_identical_windows_and_doubled_round_trip_costs(self):
        candles = flat_candles(500)
        with patch("dashboard.signals.candle_signal", return_value=True):
            report = chronological_validation(candles, fee_bps=7, slippage_bps=11)
            row = report["strategies"][0]
            expected_stress = [simulate(candles, "trend", window["start_index"], window["end_index"], 14, 22)
                               for window in report["windows"]]
        for window, expected in zip(row["windows"], expected_stress):
            self.assertAlmostEqual(window["stress_return_pct"], expected["return_pct"])
            self.assertLess(window["stress_return_pct"], window["return_pct"])
        self.assertAlmostEqual(row["stress_return_pct"],
                               (prod(1+r["return_pct"]/100 for r in expected_stress)-1)*100)
        self.assertAlmostEqual(row["return_pct"],
                               (prod(1+r["return_pct"]/100 for r in row["windows"])-1)*100)
        # Flat prices make each net outcome a loss: compounding drawdown spans windows.
        self.assertAlmostEqual(row["max_drawdown_pct"], -row["return_pct"])
        self.assertGreater(row["max_drawdown_pct"], max(w["max_drawdown_pct"] for w in row["windows"]))
        self.assertEqual(row["verdict"], "negative")

    def test_gap_above_target_precedes_later_intrabar_stop(self):
        candles = flat_candles(101)+[
            Candle(START+timedelta(minutes=1515), 102, 103, 98, 100, 100),
        ]
        with patch("dashboard.signals.candle_signal", return_value=True):
            report = simulate(candles, "trend", 100, 102, 0, 0)
        self.assertEqual(report["trades"], 1)
        self.assertEqual(report["recent_trades"][0]["reason"], "gap_target")
        self.assertAlmostEqual(report["return_pct"], .6)

    def test_cost_stress_preserves_exit_path_when_slippage_would_shift_target(self):
        candles = flat_candles(100)+[
            Candle(START+timedelta(minutes=1500), 100, 100.75, 100, 100.7, 100),
            Candle(START+timedelta(minutes=1515), 98, 98, 98, 98, 100),
        ]
        with patch("dashboard.signals.candle_signal", side_effect=lambda c, f, strategy: c.time == candles[99].time):
            report = simulate(candles, "trend", 100, 102, 10, 10, stress_costs=(20, 20))
            shifted_plan = simulate(candles, "trend", 100, 102, 20, 20)
        baseline_entry = 100*1.001
        baseline_target = baseline_entry*(1+2*.003)
        expected = (baseline_target*.998*.998/(100*1.002*1.002)-1)*100
        self.assertAlmostEqual(report["stress_return_pct"], expected)
        self.assertLess(report["stress_return_pct"], report["return_pct"])
        self.assertEqual(report["recent_trades"][0]["reason"], "target")
        self.assertEqual(shifted_plan["recent_trades"][0]["reason"], "gap_stop")
        self.assertGreater(report["stress_return_pct"], shifted_plan["return_pct"])

    def test_profit_factor_uses_cash_pnl_with_reinvested_equity(self):
        candles = flat_candles(100)+[
            Candle(START+timedelta(minutes=1500), 100, 100, 99, 100, 100),
            Candle(START+timedelta(minutes=1515), 100, 101, 100, 100, 100),
        ]
        with patch("dashboard.signals.candle_signal", return_value=True):
            report = simulate(candles, "trend", 100, 102, 0, 0)
        self.assertEqual(report["trades"], 2)
        self.assertAlmostEqual(report["profit_factor"], .997*.006/.003)

    def test_no_trades_does_not_turn_into_positive_evidence(self):
        report = chronological_validation(flat_candles(500))
        for row in report["strategies"]:
            self.assertEqual(row["total_trades"], 0)
            self.assertEqual(row["return_pct"], 0)
            self.assertEqual(row["positive_windows"], 0)
            self.assertEqual(row["verdict"], "needs_evidence")
        expected_benchmark = (100*.9995*.9995/(100*1.0005*1.0005)-1)*100
        self.assertAlmostEqual(report["windows"][0]["buy_hold_pct"], expected_benchmark)

    def test_indicator_history_is_calculated_once_for_all_comparisons(self):
        with patch("dashboard.signals.features", wraps=features) as calculate:
            report = backtest(flat_candles(500))
        self.assertEqual(report["validation"]["status"], "ok")
        self.assertEqual(calculate.call_count, 1)
