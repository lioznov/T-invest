"""Synthetic exact-time/unknown-source checks, no price queries."""
import copy
import unittest

from . import moex_quote_validation as v


def page(rows):
    return {'candles':{'columns':['open','close','high','low','volume','begin','end'], 'data':rows}}


def candle(begin, opening, closing, volume=10):
    return [opening,closing,max(opening,closing)+1,min(opening,closing)-1,volume,begin,begin[:-2]+'59']


ROWS = [candle('2026-10-22 17:44:00',99,100),candle('2026-10-22 18:00:00',101,102),
        candle('2026-10-23 10:00:00',103,104)]
FETCHED = '2026-11-03T17:00:00+00:00'


class MoexQuoteTests(unittest.TestCase):
    def test_exact_opening_prices_and_pre_cutoff_close_are_selected(self):
        self.assertEqual(v.price_points([page(ROWS)],FETCHED),
            {'anchor_price':100,'entry_price':101,'actual_price':103})

    def test_missing_exit_or_zero_volume_stays_unknown(self):
        for rows in (ROWS[:-1], ROWS[:-1]+[candle('2026-10-23 10:00:00',103,104,0)]):
            self.assertIsNone(v.price_points([page(rows)],FETCHED)['actual_price'])

    def test_nearby_minute_is_never_used_as_entry_or_anchor(self):
        rows = [candle('2026-10-22 17:43:00',99,100),candle('2026-10-22 18:01:00',101,102)]
        p=v.price_points([page(rows)],FETCHED)
        self.assertIsNone(p['anchor_price']); self.assertIsNone(p['entry_price'])

    def test_duplicate_or_unclosed_or_invalid_price_candle_fails(self):
        cases = [ROWS+[ROWS[-1]], [list(ROWS[0])], [list(ROWS[0])]]
        cases[1][0][0]=float('nan'); cases[2][0][-1]='2026-10-22 18:00:00'
        for rows in cases:
            with self.assertRaises(ValueError):v.price_points([page(rows)],FETCHED)
        with self.assertRaises(ValueError):v.price_points([page(ROWS)],'2026-10-22T16:00:00+00:00')

    def test_wrong_dates_and_aware_iss_wall_clock_fail(self):
        for begin in ('2026-10-24 10:00:00','2026-10-22T17:44:00+03:00'):
            with self.assertRaises(ValueError):v.price_points([page([candle(begin,99,100)])],FETCHED)

    def test_cursor_uses_actual_count_and_requires_an_empty_page_without_cursor(self):
        self.assertFalse(v.cursor_finished(page(ROWS),0,3))
        self.assertTrue(v.cursor_finished(page([]),3,0))
        p=page(ROWS); p['candles.cursor']={'columns':['TOTAL'],'data':[[7]]}
        self.assertFalse(v.cursor_finished(p,0,3));self.assertTrue(v.cursor_finished(p,4,3))

    def test_lot_identity_currency_and_boolean_are_checked(self):
        p={'securities':{'columns':['SECID','LOTSIZE','CURRENCYID'],'data':[['SBER',10,'SUR']]}}
        self.assertEqual(v.lot_metadata(p,'SBER')['lot'],10)
        for change in (['GAZP',10,'SUR'],['SBER',True,'SUR'],['SBER',10,'USD']):
            bad=copy.deepcopy(p);bad['securities']['data']=[change]
            with self.assertRaises(ValueError):v.lot_metadata(bad,'SBER')

    def test_cross_provider_revision_blocks_instead_of_being_corrected(self):
        r={'base_row':{'anchor_price':100},'cached_entry_for_audit_only':101}
        v.real.audit_points(r,{'anchor_price':100,'entry_price':101,'actual_price':103})
        with self.assertRaises(ValueError):v.real.audit_points(r,{'anchor_price':100.01,'entry_price':101,'actual_price':103})

    def test_raw_table_and_unregistered_symbols_are_rejected(self):
        with self.assertRaises(ValueError):v.table({'candles':{'columns':['open'],'data':[[1,2]]}},'candles')
        with self.assertRaises(ValueError):v.url_for('TATN',True)
