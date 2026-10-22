from datetime import datetime, timedelta, timezone
from unittest import TestCase

from .signals import Candle
from .strategy_search import feature_rows
from .rotation_search import prepare
from .rebound_search import RULES, Rebound, choose_stock, simulate_rebound


def dataset():
    first = datetime(2025, 1, 6, tzinfo=timezone.utc)
    times = [first+timedelta(days=i) for i in range(220) if (first+timedelta(days=i)).weekday() < 5]
    bars = [Candle(at, 100, 101, 99, 100, 100) for at in times]
    i = 110
    bars[i-1] = Candle(times[i-1], 100, 100, 89, 90, 100)
    bars[i] = Candle(times[i], 90, 96, 89, 95, 200)
    return prepare({"instrument": {"lot": 1}}, bars, feature_rows(bars)), times[i+1].date()


class ReboundTests(TestCase):
    def test_registry_eight_unique_long_rules(self):
        self.assertEqual(len(RULES), 8)
        self.assertEqual(len({r.key for r in RULES}), 8)

    def test_confirmed_recovery_is_based_on_prior_closed_day(self):
        data, day = dataset()
        rule = Rebound("unit", 2, "day", False)
        self.assertEqual(choose_stock({"A": data}, day, rule), "A")
        # Neither today's close nor future volume may affect today's ranking.
        i = data["dates"].index(day)
        data["bars"][i] = Candle(data["bars"][i].time, 1000, 2000, 1, 1, 999999)
        self.assertEqual(choose_stock({"A": data}, day, rule), "A")

    def test_low_volume_or_no_recovery_means_no_entry(self):
        data, day = dataset()
        rule = Rebound("unit", 2, "day", False)
        i = data["dates"].index(day)-1
        c = data["bars"][i]
        data["bars"][i] = Candle(c.time, c.open, c.high, c.low, c.close, 100)
        self.assertIsNone(choose_stock({"A": data}, day, rule))

    def test_real_fixture_has_whole_lots_and_stress_reduces_profit(self):
        data, day = dataset()
        data["lot"] = 10
        result = simulate_rebound({"A": data}, Rebound("unit", 2, "day", False), day.isoformat(), (day+timedelta(days=6)).isoformat())
        self.assertEqual(result["trades"], 1)
        self.assertTrue(all(t["shares"] > 0 and t["shares"] % 10 == 0 for t in result["trade_log"]))
        self.assertLess(result["stress_return_pct"], result["return_pct"])

    def test_entry_day_volume_does_not_veto_using_future_data(self):
        data, day = dataset()
        c = data["by_date"][day]
        data["by_date"][day] = Candle(c.time, c.open, c.high, c.low, c.close, 0)
        result = simulate_rebound({"A": data}, Rebound("unit", 2, "day", False), day.isoformat(), (day+timedelta(days=6)).isoformat())
        self.assertEqual(result["trades"], 1)
        self.assertEqual(result["unverified_trades"], 1)

    def test_gap_exit_uses_open_not_unreachable_stop(self):
        data, day = dataset()
        next_day = next(d for d in data["dates"] if d > day)
        c = data["by_date"][next_day]
        data["by_date"][next_day] = Candle(c.time, 70, 70, 70, 70, 100)
        result = simulate_rebound({"A": data}, Rebound("unit", 2, "day", False), day.isoformat(), (next_day+timedelta(days=1)).isoformat())
        self.assertEqual(result["trade_log"][0]["exit_quote"], 70)
        self.assertEqual(result["trade_log"][0]["reason"], "gap_stop")

    def test_missing_held_bar_invalidates_trade(self):
        data, day = dataset()
        next_day = next(d for d in data["dates"] if d > day)
        del data["by_date"][next_day]
        daily_result = simulate_rebound({"A": data}, Rebound("unit", 2, "day", False), day.isoformat(), (day+timedelta(days=12)).isoformat())
        self.assertEqual(daily_result["trades"], 1)
        self.assertEqual(daily_result["unverified_trades"], 1)

    def test_zero_trades_are_not_a_profitable_result(self):
        data, day = dataset()
        first = day+timedelta(days=20)
        result = simulate_rebound({"A": data}, Rebound("unit", 2, "day", False), first.isoformat(), (first+timedelta(days=10)).isoformat())
        self.assertEqual(result["trades"], 0)
        self.assertEqual(result["return_pct"], 0)
