from datetime import datetime, timedelta, timezone
from unittest import TestCase

from .signals import Candle
from .strategy_search import feature_rows
from .rotation_search import ROTATIONS, Rotation, prepare, choose_stock, simulate_rotation, schedule_bucket


START = datetime(2025, 1, 6, tzinfo=timezone.utc)


def dataset(slope):
    dates = [START+timedelta(days=i) for i in range(220) if (START+timedelta(days=i)).weekday() < 5]
    bars = [Candle(at, 100+i*slope, 100.2+i*slope, 99.8+i*slope, 100+i*slope, 100)
            for i, at in enumerate(dates)]
    return prepare({"instrument": {"lot": 1}}, bars, feature_rows(bars))


class RotationTests(TestCase):
    def test_registry_has_eighteen_unique_rules(self):
        self.assertEqual(len(ROTATIONS), 18)
        self.assertEqual(len({r.key for r in ROTATIONS}), 18)

    def test_long_schedules_have_fixed_anchor_not_sample_dependent(self):
        day = datetime(2020, 1, 6).date()
        for schedule, width in (("two_weeks", 14), ("three_weeks", 21)):
            bucket = schedule_bucket(day, schedule)
            self.assertEqual(schedule_bucket(day+timedelta(days=width), schedule), bucket+1)
            self.assertEqual(schedule_bucket(day+timedelta(days=width-1), schedule), bucket)
        with self.assertRaises(ValueError):
            schedule_bucket(day, "unknown")

    def test_ranking_uses_only_previous_dates(self):
        data = {"UP": dataset(.2), "DOWN": dataset(-.2)}
        day = data["UP"]["dates"][130]
        rule = Rotation("unit", "momentum", 20, "week", True)
        self.assertEqual(choose_stock(data, day, rule), "UP")
        # Today's and future closes must not change today's chosen stock.
        data["DOWN"]["bars"][130:] = [Candle(c.time, 1000, 1001, 999, 1000, 100)
                                              for c in data["DOWN"]["bars"][130:]]
        self.assertEqual(choose_stock(data, day, rule), "UP")

    def test_no_positive_momentum_means_cash_not_fabricated_profit(self):
        data = {"A": dataset(-.1), "B": dataset(-.2)}
        rule = Rotation("unit", "momentum", 20, "week", True)
        result = simulate_rotation(data, rule, "2025-06-01", "2025-08-01")
        self.assertEqual(result["trades"], 0)
        self.assertEqual(result["return_pct"], 0)

    def test_stress_is_lower_and_lots_have_valid_quantities(self):
        data = {"UP": dataset(.2), "DOWN": dataset(-.1)}
        data["UP"]["lot"] = 10
        rule = Rotation("unit", "momentum", 20, "week", True)
        result = simulate_rotation(data, rule, "2025-06-01", "2025-08-01")
        self.assertGreater(result["trades"], 0)
        self.assertLess(result["stress_return_pct"], result["return_pct"])
        self.assertTrue(all(t["shares"] > 0 and t["shares"] % 10 == 0 for t in result["trade_log"]))

    def test_reversal_selects_decline_using_past_prices(self):
        data = {"UP": dataset(.2), "DOWN": dataset(-.2)}
        day = data["UP"]["dates"][130]
        rule = Rotation("unit", "reversal", 5, "week", False)
        self.assertEqual(choose_stock(data, day, rule), "DOWN")
