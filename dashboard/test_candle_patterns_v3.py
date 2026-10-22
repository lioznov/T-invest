from dataclasses import replace
from datetime import timedelta
from unittest import TestCase

from .candle_patterns_v3 import VARIANTS, scan_candle_patterns
from .test_price_patterns import bars


def scan(extra):
    candles = bars(extra)
    rows = [{"ema": 99, "rising": True, "atr": .4, "rvol": 2} for _ in candles]
    return candles, rows, scan_candle_patterns(candles, rows)


class CandlePatternsV3Tests(TestCase):
    def test_six_fixed_long_hypotheses(self):
        self.assertEqual(len(VARIANTS), 6)
        self.assertEqual(sum(v["flow"] for v in VARIANTS.values()), 3)

    def test_engulf_needs_complete_second_bar(self):
        _, _, events = scan([(100.15, 100.2, 99.95, 99.95, 100),
                             (99.94, 100.25, 99.90, 100.20, 200)])
        self.assertIsNone(events["engulf_pullback"][100])
        self.assertIsNotNone(events["engulf_pullback"][101])

    def test_hammer_needs_separate_confirmation(self):
        _, _, events = scan([(99.95, 100.03, 99.75, 100, 100),
                             (100, 100.3, 99.99, 100.2, 200)])
        self.assertIsNone(events["hammer_confirm"][100])
        self.assertIsNotNone(events["hammer_confirm"][101])

    def test_hammer_support_failure_invalidates_confirmation(self):
        _, _, events = scan([(99.95, 100.03, 99.75, 100, 100),
                             (100, 100.3, 99.7, 100.2, 200)])
        self.assertIsNone(events["hammer_confirm"][101])

    def test_inside_bar_requires_later_breakout(self):
        _, _, events = scan([(100, 100.3, 99.7, 100, 100),
                             (100, 100.15, 99.85, 100.05, 100),
                             (100.05, 100.5, 99.95, 100.4, 200)])
        self.assertIsNone(events["inside_break"][101])
        self.assertIsNotNone(events["inside_break"][102])

    def test_all_prefixes_are_causal(self):
        candles, rows, full = scan([(100, 100.3, 99.7, 100, 100),
                                    (100, 100.15, 99.85, 100.05, 100),
                                    (100.05, 100.5, 99.95, 100.4, 200)] + [(100, 120, 90, 110, 9999)]*3)
        for n in range(100, len(candles)+1):
            partial = scan_candle_patterns(candles[:n], rows[:n])
            for key in full:
                self.assertEqual(partial[key], full[key][:n])

    def test_gap_zero_volume_and_downtrend_veto(self):
        candles, rows, _ = scan([(99.95, 100.03, 99.75, 100, 100),
                                (100, 100.3, 99.99, 100.2, 200)])
        for bad in (replace(candles[-1], time=candles[-1].time+timedelta(minutes=15)),
                    replace(candles[-1], volume=0)):
            self.assertIsNone(scan_candle_patterns(candles[:-1]+[bad], rows)["hammer_confirm"][-1])
        rows[-1]["rising"] = False
        self.assertIsNone(scan_candle_patterns(candles, rows)["hammer_confirm"][-1])

    def test_missing_features_rejected(self):
        with self.assertRaises(ValueError):
            scan_candle_patterns(bars([]), [])
