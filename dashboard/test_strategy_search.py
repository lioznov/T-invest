"""Search harness correctness; synthetic fixtures are not market evidence."""
from datetime import datetime, timedelta, timezone
from unittest import TestCase
import json
import tempfile
from dataclasses import asdict
from pathlib import Path

from .signals import Candle
from .strategy_search import (STRATEGIES, REGISTRY, Strategy, feature_rows,
                              entry_signal, simulate, aggregate, passes_screen, validate_bars)


AT = datetime(2026, 1, 5, 7, tzinfo=timezone.utc)


def fixture(count=3, timeframe="day"):
    step = timedelta(days=1) if timeframe == "day" else timedelta(hours=1)
    bars = [Candle(AT+step*(i-100), 100, 100, 100, 100, 100) for i in range(100+count)]
    rows = feature_rows(bars)
    rows[99]["rsi2"] = 0
    strategy = Strategy("unit", "rsi", timeframe, 2, 5, False, 3, 5)
    return bars, rows, strategy


class SearchFeatureTests(TestCase):
    def test_cached_daily_inputs_exclude_weekend_dealer_bars(self):
        from .management.commands.search_strategies import read_input
        dates = [AT+timedelta(days=i) for i in range(7)]
        bars = [Candle(at, 100, 100, 100, 100, 100) for at in dates]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"input.json"
            saved = {"source": "T-Bank SDK GetCandles", "timeframe": "day",
                     "instrument": {"lot": 1}, "bars": [asdict(c) for c in bars]}
            path.write_text(json.dumps(saved, default=lambda x: x.isoformat()), encoding="utf-8")
            _, filtered = read_input(path)
        self.assertEqual(len(filtered), 5)
        self.assertTrue(all(c.time.weekday() < 5 for c in filtered))

    def test_registry_is_fixed_unique_and_has_two_timeframes(self):
        self.assertEqual(len(STRATEGIES), 24)
        self.assertEqual(len(REGISTRY), 24)
        self.assertEqual({s.timeframe for s in STRATEGIES}, {"day", "hour"})

    def test_features_are_prefix_invariant_and_channels_exclude_current_bar(self):
        bars = [Candle(AT+timedelta(days=i), 100+i, 101+i, 99+i, 100+i, 100) for i in range(150)]
        full = feature_rows(bars)
        self.assertEqual(feature_rows(bars[:120]), full[:120])
        self.assertEqual(full[-1]["channel20"], bars[-2].high)
        self.assertEqual(full[-1]["floor10"], bars[-11].low)

    def test_bands_and_rsi_for_flat_market_do_not_fabricate_a_signal(self):
        bars, rows, _ = fixture()
        computed = feature_rows(bars)
        self.assertEqual(computed[-1]["sigma20"], 0)
        self.assertEqual(computed[-1]["rsi2"], 50)
        for strategy in STRATEGIES:
            self.assertFalse(entry_signal(bars[-1], computed[-1], strategy))

    def test_mean_reversion_and_trend_filter_have_distinct_meaning(self):
        bars, rows, s = fixture()
        rows[-1].update(mean20=110, sigma20=2)
        plain = Strategy("plain", "bollinger", "day", 20, 2, False, 3, 5)
        filtered = Strategy("filter", "bollinger", "day", 20, 2, True, 3, 5)
        rows[-1]["ema"][100] = 105
        self.assertTrue(entry_signal(bars[-1], rows[-1], plain))
        self.assertFalse(entry_signal(bars[-1], rows[-1], filtered))

    def test_rejects_invalid_data(self):
        bars, _, _ = fixture()
        for example in ([bars[0], bars[0]], [Candle(AT, 100, 99, 101, 100, 1)],
                        [Candle(AT.replace(tzinfo=None), 100, 100, 100, 100, 1)]):
            with self.assertRaises(ValueError):
                validate_bars(example)


class SearchExecutionTests(TestCase):
    def test_signal_exit_is_next_open_not_previous_close(self):
        bars, rows, s = fixture(3)
        bars[100] = Candle(AT, 100, 120, 100, 120, 100)
        rows[100]["rsi2"] = 100
        bars[101] = Candle(AT+timedelta(days=1), 105, 105, 105, 105, 100)
        r = simulate(bars, rows, s, "2026-01-05", "2026-01-08", fee_bps=0, slip_bps=0)
        self.assertEqual(r["trade_log"][0]["entry_quote"], 100)
        self.assertEqual(r["trade_log"][0]["exit_quote"], 105)
        self.assertEqual(r["trade_log"][0]["reason"], "signal_next_open")

    def test_gap_stop_precedes_a_signal_exit_and_can_exceed_budget(self):
        bars, rows, s = fixture(2)
        rows[100]["rsi2"] = 100
        bars[101] = Candle(AT+timedelta(days=1), 90, 90, 90, 90, 100)
        r = simulate(bars, rows, s, "2026-01-05", "2026-01-07")
        trade = r["trade_log"][0]
        self.assertEqual(trade["exit_quote"], 90)
        self.assertEqual(trade["reason"], "gap_stop")
        self.assertGreater(-trade["pnl"], trade["planned_loss"])

    def test_stress_uses_identical_prices_and_whole_lot_quantities(self):
        bars, rows, s = fixture(1)
        r = simulate(bars, rows, s, "2026-01-05", "2026-01-06", lot=10)
        t = r["trade_log"][0]
        self.assertEqual(t["shares"] % 10, 0)
        expected = t["shares"]*(100*.999*.999-100*1.001*1.001)/100000*100
        self.assertAlmostEqual(r["stress_return_pct"], expected)
        self.assertLess(r["stress_return_pct"], r["return_pct"])

    def test_entry_volume_does_not_veto_an_entry_using_future_information(self):
        bars, rows, s = fixture(1)
        bars[100] = Candle(AT, 100, 100, 100, 100, 0)
        r = simulate(bars, rows, s, "2026-01-05", "2026-01-06")
        self.assertEqual(r["trades"], 1)
        self.assertEqual(r["unverified_trades"], 1)

    def test_possible_corporate_action_does_not_become_valid_profit(self):
        bars, rows, s = fixture(2)
        bars[101] = Candle(AT+timedelta(days=1), 150, 150, 150, 150, 100)
        r = simulate(bars, rows, s, "2026-01-05", "2026-01-07")
        self.assertEqual(r["unverified_trades"], 1)

    def test_missing_hour_inside_core_session_invalidates_trade(self):
        bars, rows, s = fixture(2, "hour")
        bars[101] = Candle(AT+timedelta(hours=2), 100, 100, 100, 100, 100)
        r = simulate(bars, rows, s, "2026-01-05", "2026-01-06")
        self.assertEqual(r["unverified_trades"], 1)

    def test_aggregate_is_equal_account_mean_not_sum_of_percentages(self):
        base = {"status": "ok", "trades": 0, "trade_log": [], "unverified_trades": 0}
        a = {**base, "return_pct": 10, "stress_return_pct": 8,
             "daily_curve": [{"day": "2026-01-05", "equity": 1.1}]}
        b = {**base, "return_pct": -10, "stress_return_pct": -12,
             "daily_curve": [{"day": "2026-01-05", "equity": .9}]}
        r = aggregate([a, b])
        self.assertAlmostEqual(r["return_pct"], 0)
        self.assertAlmostEqual(r["stress_return_pct"], -2)
        self.assertAlmostEqual(r["daily_curve"][0]["equity"], 1)

    def test_positive_result_without_observations_cannot_pass_screen(self):
        fake = {"accounts": 7, "trades": 0, "positive_accounts": 7,
                "return_pct": 5, "stress_return_pct": 3,
                "unverified_trades": 0, "max_drawdown_pct": 1}
        self.assertFalse(passes_screen(fake, fake))
