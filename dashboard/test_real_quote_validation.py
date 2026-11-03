"""Exact-clock quote checks; all examples here are synthetic, not broker data."""
import copy
import unittest
from datetime import datetime, timedelta

from . import real_quote_validation as study


def bar(at, opening=100., closing=100., volume=10):
    return {'time': at.isoformat(), 'open': opening, 'high': max(opening, closing)+1,
            'low': min(opening, closing)-1, 'close': closing, 'volume': volume,
            'is_complete': True, 'source': study.SOURCE}


def quotes():
    at = datetime.fromisoformat(study.ORIGIN)
    return [bar(at-timedelta(minutes=15), closing=101.), bar(at+timedelta(minutes=15), opening=102.),
            bar(study.m.clock(at.date()+timedelta(days=1), 10), opening=103.)]


class QuoteValidationTests(unittest.TestCase):
    def test_uses_known_close_then_exact_future_opens(self):
        self.assertEqual(study.price_points(quotes(), study.ORIGIN),
                         {'anchor_price': 101., 'entry_price': 102., 'actual_price': 103.})

    def test_missing_target_stays_unknown_without_nearest_bar_substitution(self):
        bars = quotes()
        target = datetime.fromisoformat(bars[-1]['time'])
        bars[-1] = bar(target-timedelta(minutes=15), opening=999.)
        self.assertIsNone(study.price_points(bars, study.ORIGIN)['actual_price'])

    def test_zero_volume_is_not_a_verified_fill_price(self):
        bars = quotes(); bars[-1]['volume'] = 0
        self.assertIsNone(study.price_points(bars, study.ORIGIN)['actual_price'])

    def test_dealer_incomplete_duplicate_or_future_bars_are_rejected(self):
        for change in ('dealer', 'incomplete', 'duplicate', 'late'):
            bars = quotes()
            if change == 'dealer': bars[-1]['source'] = 'CANDLE_SOURCE_DEALER_WEEKEND'
            if change == 'incomplete': bars[-1]['is_complete'] = False
            if change == 'duplicate': bars.append(copy.deepcopy(bars[-1]))
            if change == 'late': bars[-1]['time'] = (datetime.fromisoformat(bars[-1]['time'])+timedelta(minutes=15)).isoformat()
            with self.subTest(change=change), self.assertRaises(ValueError):
                study.price_points(bars, study.ORIGIN)

    def test_history_revision_cannot_silently_change_features_or_entry(self):
        record = {'base_row': {'anchor_price': 101.}, 'cached_entry_for_audit_only': 102.}
        prices = study.price_points(quotes(), study.ORIGIN)
        study.audit_points(record, prices)
        for name in ('anchor_price', 'entry_price'):
            with self.assertRaises(ValueError):
                study.audit_points(record, {**prices, name: 999.})

    def test_three_fixed_decisions_and_strict_threshold(self):
        result = study.actions([.1, .2, .3])
        self.assertTrue(result['selected_median']); self.assertFalse(result['selected_unanimous'])
        self.assertAlmostEqual(result['mean_net_pct'], .2)
        for values in ([.2, .3], [.2, float('nan'), .3], [.2, True, .3]):
            with self.assertRaises(ValueError): study.actions(values)
