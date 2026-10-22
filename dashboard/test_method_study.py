"""Synthetic fixtures verify research arithmetic, NOT strategy profitability."""
import io
import json
import tempfile
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from django.core.management import call_command
from django.test import override_settings

from .method_study import (METHODS, compare_methods, simulate_method, study_features,
                           study_signals, validate_candles)
from .signals import Candle


START = datetime(2026, 1, 5, 7, tzinfo=timezone.utc)  # Monday, 10:00 Moscow.


def flat(count=800):
    return [Candle(START+timedelta(minutes=15*i), 100, 100, 100, 100, 100)
            for i in range(count)]


def execution_fixture(at=None, count=20):
    at = at or START+timedelta(minutes=30)
    candles = [Candle(at+timedelta(minutes=15*(i-100)), 100, 100, 100, 100, 100)
               for i in range(100+count)]
    rows = study_features(candles)
    flags = [False]*len(candles)
    flags[99] = True
    return candles, rows, flags


class MethodFeatureTests(TestCase):
    def test_unfinished_hour_never_enters_hourly_ema(self):
        candles = flat(400)
        c = candles[-1]
        candles[-1] = Candle(c.time, 100, 110, 100, 110, 100)
        rows = study_features(candles)
        self.assertEqual(rows[-2]["hourly_count"], 99)
        self.assertFalse(rows[-2]["hourly_trend"])
        self.assertEqual(rows[-1]["hourly_count"], 100)
        self.assertTrue(rows[-1]["hourly_trend"])
        self.assertAlmostEqual(rows[-1]["hourly_ema"], 100+10*2/51)

    def test_missing_quarter_cannot_create_a_completed_hour(self):
        candles = flat(8)
        del candles[1]
        rows = study_features(candles)
        self.assertEqual(rows[2]["hourly_count"], 0)
        self.assertEqual(rows[-1]["hourly_count"], 1)

    def test_seasonal_volume_uses_prior_weekdays_same_clock_slot(self):
        dates = [START+timedelta(days=i) for i in range(30)
                 if (START+timedelta(days=i)).weekday() < 5]
        candles = [Candle(at, 100, 100, 100, 100, 100) for at in dates]
        c = candles[10]
        candles[10] = Candle(c.time, 100, 100, 100, 100, 200)
        rows = study_features(candles)
        self.assertIsNone(rows[9]["seasonal_rvol"])
        self.assertEqual(rows[10]["seasonal_samples"], 10)
        self.assertEqual(rows[10]["seasonal_rvol"], 2)
        self.assertEqual(rows[11]["seasonal_rvol"], 1)
        self.assertEqual(rows[-1]["seasonal_samples"], 20)

    def test_other_clock_slots_do_not_contaminate_volume_reference(self):
        candles = [Candle(START+timedelta(days=i), 100, 100, 100, 100, 100)
                   for i in range(20)]
        c = candles[-1]
        candles[-1] = Candle(c.time+timedelta(minutes=15), 100, 100, 100, 100, 100000)
        self.assertIsNone(study_features(candles)[-1]["seasonal_rvol"])

    def test_features_and_signals_are_prefix_invariant(self):
        candles = [Candle(c.time, 100+i*.01, 100.15+i*.01, 99.95+i*.01,
                          100.1+i*.01, 100+i%7*100) for i, c in enumerate(flat(1200))]
        full = study_features(candles)
        prefix = study_features(candles[:700])
        self.assertEqual(prefix, full[:700])
        signals = study_signals(candles, full)
        short = study_signals(candles[:700], prefix)
        for key in METHODS:
            self.assertEqual(short[key], signals[key][:700])

    def test_rejects_duplicate_naive_nonfinite_and_invalid_ohlc(self):
        c = flat(1)[0]
        examples = [[c, c], [Candle(c.time.replace(tzinfo=None), 100, 100, 100, 100, 1)],
                    [Candle(c.time, 100, float("inf"), 100, 100, 1)],
                    [Candle(c.time, 100, 99, 101, 100, 1)],
                    [Candle(c.time+timedelta(minutes=1), 100, 100, 100, 100, 1)],
                    [Candle(c.time, 100, 100, 100, 100, -1)]]
        for example in examples:
            with self.subTest(example=example), self.assertRaises(ValueError):
                validate_candles(example)

    def test_hour_and_seasonal_filters_are_distinct_ablations(self):
        candles = flat(110)
        rows = study_features(candles)
        rows[-1].update(ema=99, rising=True, vwap=99, rvol=2,
                        seasonal_rvol=.5, hourly_trend=False)
        flags = study_signals(candles, rows)
        self.assertTrue(flags["baseline"][-1])
        self.assertFalse(flags["hourly"][-1])
        self.assertFalse(flags["seasonal"][-1])
        rows[-1].update(seasonal_rvol=2, hourly_trend=True)
        flags = study_signals(candles, rows)
        self.assertTrue(flags["combined"][-1])


class MethodExecutionTests(TestCase):
    def test_signal_can_only_fill_next_open(self):
        candles, rows, flags = execution_fixture(count=2)
        flags[99], flags[100] = False, True
        c = candles[101]
        candles[101] = Candle(c.time, 120, 120, 120, 120, 100)
        report = simulate_method(candles, rows, flags, 100, 102, 1, fee_bps=0, slippage_bps=0)
        self.assertEqual(report["trades"], 1)
        self.assertEqual(report["trade_log"][0]["entry_quote"], 120)
        self.assertEqual(report["trade_log"][0]["entry_time"], c.time.isoformat())
        self.assertEqual(report["return_pct"], 0)

    def test_stop_wins_when_both_levels_are_inside_entry_bar(self):
        candles, rows, flags = execution_fixture(count=1)
        c = candles[100]
        candles[100] = Candle(c.time, 100, 102, 99, 101, 100)
        report = simulate_method(candles, rows, flags, 100, 101, 1, fee_bps=0, slippage_bps=0)
        trade = report["trade_log"][0]
        self.assertEqual(trade["reason"], "stop")
        self.assertAlmostEqual(trade["exit_quote"], 99.7)
        self.assertLess(report["return_pct"], 0)

    def test_price_gap_can_exceed_planned_risk(self):
        candles, rows, flags = execution_fixture(count=2)
        c = candles[101]
        candles[101] = Candle(c.time, 90, 90, 90, 90, 100)
        report = simulate_method(candles, rows, flags, 100, 102, 1)
        trade = report["trade_log"][0]
        self.assertEqual(trade["reason"], "gap_stop")
        self.assertGreater(-trade["pnl"], trade["planned_loss"])
        self.assertGreater(-report["return_pct"], .5)

    def test_closes_at_predeclared_end_of_day_without_future_calendar_peeking(self):
        candles, rows, flags = execution_fixture(START.replace(hour=14), count=12)
        report = simulate_method(candles, rows, flags, 100, len(candles), 1)
        trade = report["trade_log"][0]
        self.assertEqual(trade["exit_bar"], START.replace(hour=14, minute=45).isoformat())
        self.assertEqual(trade["reason"], "time")

    def test_maximum_hold_is_three_clock_hours(self):
        candles, rows, flags = execution_fixture(count=20)
        report = simulate_method(candles, rows, flags, 100, len(candles), 1)
        self.assertEqual(report["trade_log"][0]["exit_bar"], candles[111].time.isoformat())

    def test_weekends_and_outside_entry_hours_do_not_trade(self):
        for at in (START.replace(hour=6), START.replace(hour=15), START+timedelta(days=5)):
            candles, rows, flags = execution_fixture(at, count=2)
            report = simulate_method(candles, rows, flags, 100, len(candles), 1)
            self.assertEqual(report["trades"], 0)

    def test_at_most_one_entry_per_moscow_day(self):
        candles, rows, flags = execution_fixture(count=20)
        for i in range(100, 120):
            c = candles[i]
            candles[i] = Candle(c.time, 100, 101, 100, 100, 100)
            flags[i] = i % 3 == 0
        report = simulate_method(candles, rows, flags, 100, 120, 1)
        self.assertEqual(report["trades"], 1)

    def test_missing_entry_bar_prevents_fill(self):
        candles, rows, flags = execution_fixture(count=1)
        before = candles[99]
        candles[99] = Candle(before.time-timedelta(minutes=15), 100, 100, 100, 100, 100)
        report = simulate_method(candles, rows, flags, 100, 101, 1)
        self.assertEqual(report["trades"], 0)
        self.assertEqual(report["skipped_gaps"], 1)

    def test_missing_quotes_while_holding_are_explicitly_unverified(self):
        candles, rows, flags = execution_fixture(count=2)
        c = candles[101]
        candles[101] = Candle(c.time+timedelta(minutes=15), 101, 101, 101, 101, 100)
        report = simulate_method(candles, rows, flags, 100, 102, 1)
        self.assertEqual(report["unverified_trades"], 1)
        self.assertEqual(report["trade_log"][0]["reason"], "missing_quotes")

    def test_stress_reprices_same_quotes_shares_and_targets(self):
        candles, rows, flags = execution_fixture(count=1)
        c = candles[100]
        candles[100] = Candle(c.time, 100, 100.65, 100, 100.6, 100)
        report = simulate_method(candles, rows, flags, 100, 101, 1, fee_bps=7, slippage_bps=11)
        trade = report["trade_log"][0]
        self.assertEqual(trade["reason"], "target")
        expected_pnl = trade["shares"]*(100.6*(1-.0022)*(1-.0014)-100*(1+.0022)*(1+.0014))
        self.assertAlmostEqual(report["stress_return_pct"], expected_pnl/100000*100)
        self.assertLess(report["stress_return_pct"], report["return_pct"])

    def test_lots_and_allocation_limit_are_respected(self):
        candles, rows, flags = execution_fixture(count=1)
        report = simulate_method(candles, rows, flags, 100, 101, 10)
        trade = report["trade_log"][0]
        self.assertEqual(trade["shares"] % 10, 0)
        self.assertLessEqual(trade["shares"]*100*1.0005*1.0005, 25000)
        self.assertLessEqual(trade["planned_loss"], 500)
        no_budget = simulate_method(candles, rows, flags, 100, 101, 1000)
        self.assertEqual(no_budget["trades"], 0)
        self.assertEqual(no_budget["skipped_lots"], 1)

    def test_smaller_allocation_does_not_improve_trade_edge(self):
        candles, rows, flags = execution_fixture(count=1)
        small = simulate_method(candles, rows, flags, 100, 101, 1, allocation_pct=25)
        large = simulate_method(candles, rows, flags, 100, 101, 1, allocation_pct=100)
        self.assertAlmostEqual(small["mean_trade_pct"], large["mean_trade_pct"])
        self.assertGreater(small["return_pct"], large["return_pct"])

    def test_entry_bar_volume_cannot_veto_entry_with_lookahead(self):
        candles, rows, flags = execution_fixture(count=1)
        c = candles[100]
        candles[100] = Candle(c.time, 100, 100, 100, 100, 0)
        report = simulate_method(candles, rows, flags, 100, 101, 1)
        self.assertEqual(report["trades"], 1)
        self.assertEqual(report["unverified_trades"], 1)

    def test_previous_zero_volume_can_causally_block_entry(self):
        candles, rows, flags = execution_fixture(count=1)
        c = candles[99]
        candles[99] = Candle(c.time, 100, 100, 100, 100, 0)
        self.assertEqual(simulate_method(candles, rows, flags, 100, 101, 1)["trades"], 0)

    def test_atr_levels_with_float_tails_are_accepted(self):
        candles, rows, flags = execution_fixture(count=1)
        rows[99]["atr"] = .2718281828459045
        report = simulate_method(candles, rows, flags, 100, 101, 1)
        self.assertEqual(report["trades"], 1)


class MethodComparisonTests(TestCase):
    def test_four_nonoverlapping_windows_and_no_false_positive_without_trades(self):
        result = compare_methods(flat(1003), 1)
        indices = [i for w in result["windows"] for i in range(w["start_index"], w["end_index"])]
        self.assertEqual(indices, list(range(result["context_bars"], 1003)))
        self.assertEqual(len(indices), len(set(indices)))
        for method in result["methods"]:
            self.assertEqual(method["trades"], 0)
            self.assertEqual(method["verdict"], "insufficient_trades")
            self.assertEqual(method["positive_windows"], 0)
            self.assertIsNone(method["mean_trade_pct"])

    def test_not_enough_history_is_not_a_result(self):
        self.assertEqual(compare_methods(flat(799), 1)["status"], "insufficient")

    def test_later_prices_cannot_change_completed_window(self):
        candles = flat(1000)
        first = compare_methods(candles, 1)
        boundary = first["windows"][0]["end_index"]
        for i in range(boundary, len(candles)):
            c = candles[i]
            candles[i] = Candle(c.time, 200, 202, 198, 201, 1000)
        second = compare_methods(candles, 1)
        for left, right in zip(first["methods"], second["methods"]):
            self.assertEqual(left["windows"][0], right["windows"][0])

    def test_offline_command_saves_exact_inputs_and_never_calls_broker(self):
        with tempfile.TemporaryDirectory() as directory, override_settings(BASE_DIR=Path(directory)):
            data = Path(directory)/"data"
            data.mkdir()
            fixture = {"at": START.isoformat(), "results": [{"ticker": "SBER",
                       "instrument": {"lot": 1, "uid": "unit-test-only"},
                       "candles": [asdict(c) for c in flat(800)]}]}
            (data/"research-test.json").write_text(json.dumps(fixture, default=lambda x: x.isoformat()), encoding="utf-8")
            with patch("dashboard.market_provider.open_client") as broker:
                call_command("study_methods", tickers=["SBER"], stdout=io.StringIO())
                broker.assert_not_called()
            reports = list(data.glob("method-study-*.json"))
            self.assertEqual(len(reports), 1)
            report = json.loads(reports[0].read_text(encoding="utf-8"))
            self.assertEqual(len(report["results"][0]["candles"]), 800)
            self.assertEqual(len(report["protocol_sha256"]), 64)
            self.assertEqual(report["summaries"][0]["positive_tickers"], 0)
            with patch("dashboard.market_provider.open_client") as broker:
                call_command("study_methods", tickers=["SBER"], input=str(reports[0]), stdout=io.StringIO())
                broker.assert_not_called()
            self.assertEqual(len(list(data.glob("method-study-*.json"))), 2)

    def test_replay_outside_data_is_rejected(self):
        from django.core.management.base import CommandError
        with tempfile.TemporaryDirectory() as directory, override_settings(BASE_DIR=Path(directory)):
            source = Path(directory)/"outside.json"
            source.write_text("{}", encoding="utf-8")
            with self.assertRaises(CommandError):
                call_command("study_methods", input=str(source), stdout=io.StringIO())
