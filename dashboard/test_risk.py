from decimal import Decimal
from unittest import TestCase

from django.test import SimpleTestCase

from .risk import position_size


class PositionSizeTests(TestCase):
    def test_whole_lots_respect_both_capital_and_loss_limits(self):
        for capital in (10000, 100000, 500000):
            for lot in (1, 10, 100):
                report = position_size(capital, .5, 10, 300, 297, 306, lot, 5, 5)
                self.assertEqual(report['shares'] % lot, 0)
                self.assertLessEqual(report['position_cost'], capital * .10)
                self.assertLessEqual(report['planned_loss'], capital * .005)

    def test_costs_reduce_size_and_include_both_sides(self):
        free = position_size(100000, .5, 100, 100, 99, 103, 1, 0, 0)
        paid = position_size(100000, .5, 100, 100, 99, 103, 1, 5, 5)
        self.assertLess(paid['shares'], free['shares'])
        per_share = Decimal('100') * Decimal('1.0005') ** 2 - Decimal('99') * Decimal('.9995') ** 2
        self.assertAlmostEqual(paid['planned_loss'], float((paid['shares'] * per_share).quantize(Decimal('.01'))))

    def test_unaffordable_lot_does_not_suggest_fractional_lot(self):
        report = position_size(1000, .5, 10, 300, 297, 306, 10)
        self.assertEqual(report['lots'], 0)
        self.assertIsNone(report['reward_risk'])

    def test_rejects_invalid_stop_nonfinite_values_and_fractional_lots(self):
        base = dict(capital=100000, risk_pct=.5, allocation_pct=10, entry=300, stop=297, target=306, lot=10)
        for values in ({'capital': 'NaN'}, {'stop': 300}, {'target': 299}, {'lot': 1.5}, {'fee_bps': -1},
                       {'entry':'1e-999999', 'stop':'5e-1000000', 'target':'2e-999999'},
                       {'entry':'300.00000000000000000000000000001', 'stop':300, 'fee_bps':0, 'slippage_bps':0}):
            with self.assertRaises(ValueError):
                position_size(**(base | values))


class RiskApiTests(SimpleTestCase):
    def test_api_rejects_missing_parameters_and_calculates_valid_plan(self):
        self.assertEqual(self.client.get('/api/risk/').status_code, 400)
        response = self.client.get('/api/risk/', {'capital': 100000, 'risk_pct': .5, 'allocation_pct': 10,
                                                'entry': 300, 'stop': 297, 'target': 306, 'lot': 10})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['shares'], 30)
        self.assertEqual(response['Cache-Control'], 'no-store')

    def test_paper_demo_cannot_be_mistaken_for_live_trade_history(self):
        response = self.client.get('/api/paper/?ticker=SBER&mode=demo')
        self.assertEqual(response.json()['status'], 'unavailable')
        self.assertEqual(self.client.get('/api/paper/?fee=NaN').status_code, 400)
