from dataclasses import replace
from datetime import datetime, timezone
from django.test import SimpleTestCase
from .signals import Candle
from .monthly_momentum import prepare
from .management.commands.audit_monthly_calendar import review_result


class MonthlyCalendarTests(SimpleTestCase):
    def sample(self):
        bars = [Candle(datetime(2025,12,30,tzinfo=timezone.utc),100,102,99,101,100),
                Candle(datetime(2026,1,5,tzinfo=timezone.utc),101,102,99,100,100)]
        result = {'return_pct': .25, 'unknown_trades': 1, 'verified': False, 'curve': [{'equity':100250}],
                  'quality_incidents': [{'day':'2026-01-05','ticker':'A','reason':'gap_or_corporate_action'}],
                  'trade_log': [{'ticker':'A','entry_day':'2025-12-20','exit_day':'2026-01-06','verified':False,'pnl':250}]}
        return bars, result

    def test_known_closure_review_preserves_money_and_original(self):
        bars, result = self.sample()
        reviewed = review_result(result, {'A':prepare(bars,1)})
        self.assertTrue(reviewed['verified'])
        self.assertEqual(reviewed['unknown_trades'],0)
        self.assertEqual(reviewed['return_pct'],result['return_pct'])
        self.assertEqual(reviewed['trade_log'][0]['pnl'],250)
        self.assertFalse(result['verified'])

    def test_corporate_action_is_not_excused_by_holiday(self):
        bars, result = self.sample()
        bars[1] = replace(bars[1],open=1,high=1.1,low=.9,close=1)
        self.assertFalse(review_result(result,{'A':prepare(bars,1)})['verified'])

    def test_unlisted_gap_stays_unknown(self):
        bars, result = self.sample()
        bars[0] = replace(bars[0],time=datetime(2025,12,29,tzinfo=timezone.utc))
        self.assertFalse(review_result(result,{'A':prepare(bars,1)})['verified'])

    def test_other_quality_incident_still_blocks(self):
        bars, result = self.sample()
        result['quality_incidents'].append({'day':'2026-01-05','ticker':'A','reason':'zero_volume_held_bar'})
        self.assertFalse(review_result(result,{'A':prepare(bars,1)})['verified'])
