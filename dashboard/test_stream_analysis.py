"""Synthetic correctness tests; not evidence of market profitability."""
import json
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
from unittest import TestCase
from unittest.mock import patch
import hashlib

from django.test import override_settings
from .continuous_watch import ExtraArchive, Journal
from .market_provider import MarketUnavailable
from .market_stream_provider import apply_quality_guard, exchange_candles, live_snapshot
from .microstructure_archive import MicrostructureArchive, stamp
from .pressure_study import evaluate_segment, execution, ofi, prepare, summary
from .stream_inputs import stream_window
from .test_microstructure_archive import NOW, INSTRUMENT, book, trade


def row(second, kind='book', **changes):
    at = NOW+timedelta(seconds=second)
    payload = {'bids': [[100., 80]], 'asks': [[100.05, 20]], 'lot_size': 10}
    if kind == 'trade':
        payload = {'price': 100.05, 'quantity': 10, 'direction': 'TRADE_DIRECTION_BUY', 'lot_size': 10}
    elif kind == 'info':
        payload = {'status': 'SECURITY_TRADING_STATUS_NORMAL_TRADING', 'market_orders_available': True}
    return dict(id=second+1, ticker='SBER', instrument_uid=INSTRUMENT['uid'], kind=kind,
                received_at=stamp(at), exchange_at=stamp(at), causal_eligible=1, is_consistent=1,
                source='TRADE_SOURCE_EXCHANGE' if kind == 'trade' else 'ORDERBOOK_TYPE_EXCHANGE',
                payload=payload, **changes)


class PressureStudyTests(TestCase):
    def rows(self, count=251):
        result = []
        for i in range(count):
            result.extend([row(i, 'trade'), row(i)])
        return result

    def test_observed_ofi_unchanged_price_changes_quantity(self):
        a, b = row(0), row(1)
        b['payload']['bids'][0][1] = 90
        b['payload']['asks'][0][1] = 15
        self.assertEqual(ofi(a, b), 15)

    def test_four_fixed_long_variants_no_orders(self):
        result = evaluate_segment(self.rows(), [row(0, 'info')], 'SBER', 'fixture')
        self.assertTrue(result['persistent_bid_60s'])
        self.assertFalse(result['positive_ofi_60s'])
        self.assertGreater(summary(result['persistent_bid_180s'])['unverified'], 0)
        self.assertFalse(summary(result['persistent_bid_60s'])['entry_allowed'])

    def test_future_trades_cannot_create_early_signal(self):
        rows = [r for r in self.rows() if r['kind'] == 'book' or r['id'] > 101]
        result = evaluate_segment(rows, [row(0, 'info')], 'SBER', 'fixture')
        self.assertTrue(all(r['signal_at'] >= stamp(NOW+timedelta(seconds=100)) for r in result['persistent_bid_60s']))

    def test_future_status_cannot_create_earlier_signal(self):
        result = evaluate_segment(self.rows(), [row(100, 'info')], 'SBER', 'fixture')
        self.assertTrue(all(r['signal_at'] > stamp(NOW+timedelta(seconds=100)) for r in result['persistent_bid_60s']))

    def test_unknown_status_never_signals(self):
        result = evaluate_segment(self.rows(), [], 'SBER', 'fixture')
        self.assertTrue(all(not r for r in result.values()))

    def test_execution_uses_next_ask_and_future_bid_with_costs(self):
        result = execution(NOW, [prepare(row(i)) for i in range(1, 62)], 60)
        self.assertEqual(result['entry_at'], stamp(NOW+timedelta(seconds=1)).replace('.000000', ''))
        self.assertEqual(result['exit_at'], stamp(NOW+timedelta(seconds=61)).replace('.000000', ''))
        self.assertLess(result['return_pct'], result['gross_quote_return_pct'])
        self.assertLess(result['stress_return_pct'], result['return_pct'])

    def test_bad_future_book_does_not_get_dropped(self):
        values = [row(i) for i in range(1, 62)]
        values[20]['is_consistent'] = 0
        self.assertEqual(execution(NOW, [prepare(r) for r in values], 60)['status'], 'unverified')

    def test_future_gap_does_not_get_filled(self):
        values = [prepare(row(i)) for i in range(1, 62) if not 20 <= i <= 26]
        self.assertEqual(execution(NOW, values, 60)['reason'], 'bad_book_or_gap')

    def test_segment_end_and_delayed_entry_are_unknown(self):
        self.assertEqual(execution(NOW, [prepare(row(1))], 60)['reason'], 'segment_ended')
        self.assertEqual(execution(NOW, [prepare(row(6))], 60)['status'], 'unverified')

    def test_no_trades_is_unknown_not_zero_profit(self):
        result = summary([])
        self.assertIsNone(result['mean_trade_return_pct'])
        self.assertFalse(result['sufficient_sample'])

    def test_cooldown_is_fixed_even_for_unknown_execution(self):
        result = evaluate_segment(self.rows(101), [row(0, 'info')], 'SBER', 'fixture')
        self.assertEqual(len(result['persistent_bid_180s']), 1)
        self.assertEqual(result['persistent_bid_180s'][0]['status'], 'unverified')


class StreamInputTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)/'watch'
        self.session = self.root/'fixture'
        self.session.mkdir(parents=True)
        self.journal = Journal(self.session/'health.sqlite3', ['SBER'], 300)
        self.addCleanup(self.journal.close)
        self.market = MicrostructureArchive(self.session/'0001-book.sqlite3')
        self.addCleanup(self.market.db.close)
        self.market.begin_run(['SBER'], 300, NOW)
        self.extra = ExtraArchive(self.session/'0001-chart.sqlite3')
        self.addCleanup(self.extra.close)
        self.extra.append('info', NS(time=NOW, trading_status='SECURITY_TRADING_STATUS_NORMAL_TRADING',
            market_order_available_flag=True), INSTRUMENT, NOW, set())

    def window(self, at=NOW+timedelta(seconds=120)):
        with patch('dashboard.stream_inputs.load_health', return_value={'alive': True, 'tickers': ['SBER']}):
            return stream_window(self.root, 'SBER', INSTRUMENT, at)

    def fill(self):
        for i in range(121):
            at = NOW+timedelta(seconds=i)
            self.market.append('book', book(at), INSTRUMENT, at)
            self.market.append('trade', trade(at), INSTRUMENT, at)

    def test_valid_stream_keeps_clocks_and_lot_units(self):
        self.fill()
        result = self.window()
        self.assertFalse(result['data_quality']['action_blocked'])
        self.assertTrue(result['trading'])
        self.assertEqual(result['trades'][0]['quantity'], 2)
        self.assertTrue(result['books'][-1]['is_consistent'])

    def test_latest_inconsistent_book_refuses_older_valid(self):
        self.fill()
        at = NOW+timedelta(seconds=122)
        self.market.append('book', book(at, is_consistent=False), INSTRUMENT, at)
        with self.assertRaises(MarketUnavailable):
            self.window(at)

    def test_gap_blocks_action_but_can_display_chart(self):
        self.fill()
        at = NOW+timedelta(seconds=130)
        self.market.append('book', book(at), INSTRUMENT, at)
        self.assertTrue(self.window(at)['data_quality']['action_blocked'])

    def test_unclassified_trade_blocks_action(self):
        self.fill()
        at = NOW+timedelta(seconds=120)
        self.market.append('trade', trade(at, direction='UNKNOWN'), INSTRUMENT, at)
        self.assertTrue(self.window()['data_quality']['action_blocked'])

    def test_stale_status_refuses_analysis(self):
        self.fill()
        at = NOW+timedelta(seconds=400)
        self.market.append('book', book(at), INSTRUMENT, at)
        with self.assertRaises(MarketUnavailable):
            self.window(at)

    def test_guard_blocks_long_and_exit_and_preserves_score(self):
        for decision in ('LONG', 'EXIT'):
            analysis = {'decision': decision, 'plan': {'entry': 100}, 'score': 100, 'blockers': []}
            apply_quality_guard(analysis, {'data_quality': {'action_blocked': True, 'blocked_reason': 'gap'}})
            self.assertEqual(analysis['decision'], 'WAIT')
            self.assertIsNone(analysis['plan'])
            self.assertEqual(analysis['score'], 100)
            self.assertEqual(analysis['blockers'], ['gap'])

    def test_no_worker_uses_legacy_only_as_blocked_fallback(self):
        with override_settings(BASE_DIR=Path(self.temp.name)), \
                patch('dashboard.continuous_watch.load_health', return_value={'alive': False}), \
                patch('dashboard.market_stream_provider.legacy_snapshot', return_value={'books': []}):
            self.assertTrue(live_snapshot('SBER', now=NOW)['data_quality']['action_blocked'])

    def test_exchange_minute_candles_not_misvalidated_as_15min(self):
        from t_tech.invest.schemas import CandleSource
        at = NOW.replace(minute=1)
        quotation = lambda units: NS(units=units, nano=0)
        candle = NS(time=at, open=quotation(100), high=quotation(101), low=quotation(99), close=quotation(100), volume=10, is_complete=True,
                    candle_source=CandleSource.CANDLE_SOURCE_EXCHANGE)
        client = NS(market_data=NS(get_candles=lambda **kwargs: NS(candles=[candle])))
        result = exchange_candles(client, 'uid', at, at+timedelta(minutes=2), 1, 1)
        self.assertEqual(len(result), 1)
        candle.candle_source = NS(name='CANDLE_SOURCE_DEALER')
        with self.assertRaises(MarketUnavailable):
            exchange_candles(client, 'uid', at, at+timedelta(minutes=2), 1, 1)


class StreamEvidenceTests(TestCase):
    def test_hourly_review_does_not_mix_old_provider_with_new_one(self):
        from .forward_review import review_observations
        at = NOW.isoformat()
        payload = {'strategy_version': 'six_rules_v1', 'provider_version': 'legacy',
                   'review_ready': True, 'decision': 'LONG', 'score': 100,
                   'book': {'time': at, 'bids': [[100, 1]], 'asks': [[101, 1]]}}
        result = review_observations([(at, payload)], provider_version='stream_exchange_v1')
        self.assertEqual(result['observations'], 0)
        self.assertEqual(result['excluded_legacy_observations'], 1)

    def test_paper_review_rejects_blocked_quote_even_when_ready_true(self):
        from .paper_journal import review_paper_trades
        from .test_paper_journal import row as paper_row
        at, payload = paper_row(0)
        payload['data_quality'] = {'action_blocked': True}
        result = review_paper_trades([(at, payload)], provider_version='stream_exchange_v1')
        self.assertEqual(result['summary']['total'], 0)

    def saved_report(self, directory):
        from .management.commands.study_pressure import hashes
        from .pressure_study import RULES, VERSION, VARIANTS
        data = Path(directory)/'data'
        data.mkdir()
        spec = {'version': VERSION, 'variants': VARIANTS, 'rules': RULES, 'entry_allowed': False, 'session': 'fixture'}
        protocol = data/'pressure-stream-protocol-v1.json'
        protocol.write_text(json.dumps({'spec': spec, 'source_sha256': hashes()}), encoding='utf-8')
        event = {'ticker': 'SBER', 'signal_at': NOW.isoformat(), 'status': 'verified_quote_replay',
                 'return_pct': 1., 'stress_return_pct': .8, 'gross_quote_return_pct': 1.2}
        report = {'version': VERSION, 'entry_allowed': False, 'protocol_sha256': hashlib.sha256(protocol.read_bytes()).hexdigest(),
                  'source_sha256': hashes(), 'session': 'fixture', 'at': NOW.isoformat(),
                  'outcomes': {key: [event] for key in VARIANTS}}
        path = data/'pressure-stream-study-fixture.json'
        path.write_text(json.dumps(report), encoding='utf-8')
        return path, report

    def test_positive_stream_research_never_authorizes_entry(self):
        from .pressure_research import load_pressure_research
        with TemporaryDirectory() as directory, override_settings(BASE_DIR=Path(directory)):
            self.saved_report(directory)
            result = load_pressure_research('SBER')
            self.assertEqual(result['status'], 'ok')
            self.assertFalse(result['entry_allowed'])
            self.assertTrue(all(not r['sufficient_sample'] for r in result['variants']))

    def test_tampered_stream_report_fail_closed(self):
        from .pressure_research import load_pressure_research
        with TemporaryDirectory() as directory, override_settings(BASE_DIR=Path(directory)):
            path, saved = self.saved_report(directory)
            saved['protocol_sha256'] = '0'*64
            path.write_text(json.dumps(saved), encoding='utf-8')
            self.assertEqual(load_pressure_research('SBER')['status'], 'insufficient')

    def test_unknown_stock_not_zero_return(self):
        from .pressure_research import load_pressure_research
        with TemporaryDirectory() as directory, override_settings(BASE_DIR=Path(directory)):
            self.saved_report(directory)
            result = load_pressure_research('TATN')
            self.assertEqual(result['status'], 'insufficient')
            self.assertEqual(result['variants'], [])

    def test_stream_report_with_boolean_return_is_rejected(self):
        from .pressure_research import load_pressure_research
        with TemporaryDirectory() as directory, override_settings(BASE_DIR=Path(directory)):
            path, saved = self.saved_report(directory)
            saved['outcomes'][next(iter(saved['outcomes']))][0]['return_pct'] = True
            path.write_text(json.dumps(saved), encoding='utf-8')
            self.assertEqual(load_pressure_research('SBER')['status'], 'insufficient')
