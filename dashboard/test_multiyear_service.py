import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from django.test import RequestFactory, SimpleTestCase, override_settings

from . import multiyear_service as service
from .multiyear_views import report


NOW = datetime(2026, 10, 23, 15, tzinfo=timezone.utc)
SPEC = SimpleNamespace(VERSION='multiyear_morning_v1', source_hashes=lambda: {'study': 'a' * 64})


def fixture():
    paper = {'trades': 2, 'unknown_trades': 0, 'net_return_pct': -0.3,
             'max_drawdown_pct': 0.5, 'status': 'ok', 'stress_return_pct': -0.4,
             'stress_trades': 2, 'stress_unknown_trades': 0}
    period = {'samples': 100, 'days': 50, 'eligible_forecasts': 102, 'unknown_targets': 2,
              'direction_nonzero_samples': 99, 'mae_bps': 45.0, 'baseline_mae_bps': 42.0,
              'mean_baseline_mae_bps': 44.0, 'direction_accuracy_pct': 50.0,
              'coverage_pct': 80.0, 'interval_width_bps': 160.0, 'abstention_pct': 90.0,
              'start': '2025-01-01', 'end_exclusive': '2026-01-01',
              'paper': paper, 'always_buy': copy.deepcopy(paper), 'note': 'Прошлая история.'}
    sample_report = {'trained_rows': 2000, 'calibration_rows': 500,
                     'periods': {'test': copy.deepcopy(period), 'later': copy.deepcopy(period)}}
    variants = []
    for name, (kind, cutoff, entry) in service.VARIANTS.items():
        value = {'id': name, 'kind': kind, 'label': name, 'report': copy.deepcopy(sample_report),
                 'model': {'weights': [666]}, 'path': 'private/path'}
        if kind == 'intraday':
            value.update(cutoff=cutoff, entry=entry)
        else:
            for p in value['report']['periods'].values():
                p['paper'].update(trades=0, net_return_pct=None, max_drawdown_pct=None,
                                  stress_trades=0, stress_return_pct=None)
        variants.append(value)
    dates = {'bars': 700, 'first_actual': '2018-01-01T00:00:00+00:00',
             'last_actual': '2026-10-22T00:00:00+00:00', 'requested_from': '2000-01-01T00:00:00+00:00',
             'to': '2026-10-23T00:00:00+00:00', 'chunks': ['private/data.json'], 'empty_chunks': 5}
    return {'version': SPEC.VERSION, 'entry_allowed': False,
            'created_at': (NOW - timedelta(hours=1)).isoformat(),
            'source_sha256': SPEC.source_hashes(), 'note': 'Прибыль не доказана.',
            'publication_note': 'Опубликованы все четыре проверки.', 'variants': variants,
            'coverage': {'SBER': {'day': copy.deepcopy(dates), 'intraday': copy.deepcopy(dates)}}}


class MultiyearServiceTests(SimpleTestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.folder = self.root / 'dashboard' / 'ai_models'
        self.folder.mkdir(parents=True)
        self.path = self.folder / 'multiyear-morning-v1-20261023-140000.json'
        config = override_settings(BASE_DIR=self.root)
        config.enable()
        self.addCleanup(config.disable)
        spec = patch.object(service, 'model_spec', return_value=SPEC)
        spec.start()
        self.addCleanup(spec.stop)

    def save(self, value=None, path=None):
        value = copy.deepcopy(fixture() if value is None else value)
        value['artifact_sha256'] = service.digest({k: v for k, v in value.items()
                                                 if k != 'artifact_sha256'})
        (path or self.path).write_text(json.dumps(value), encoding='utf-8')
        return value

    def test_projection_historical_keeps_unknowns_and_actual_requested_ranges(self):
        self.save()
        result = service.payload(NOW)
        self.assertEqual(result['status'], 'historical')
        self.assertIs(result['entry_allowed'], False)
        self.assertEqual([v['id'] for v in result['variants']], list(service.VARIANTS))
        coverage = result['coverage']['SBER']['day']
        self.assertEqual(coverage['bars'], 700)
        self.assertTrue(coverage['requested_from'].startswith('2000'))
        self.assertTrue(coverage['first_actual'].startswith('2018'))
        self.assertEqual(result['variants'][1]['report']['periods']['test']['unknown_targets'], 2)
        for private in ('private/path', 'private/data.json', 'weights', 'source_sha256',
                        'artifact_sha256', 'chunks', 'model'):
            self.assertNotIn(private, json.dumps(result))

    def test_daily_projection_never_contains_paper_or_always_buy_returns(self):
        self.save()
        result = service.payload(NOW)
        daily = result['variants'][0]
        self.assertEqual(daily['kind'], 'daily_observational')
        self.assertIn('покупка по закрытию не моделировалась', daily['notice'])
        for period in daily['report']['periods'].values():
            self.assertNotIn('paper', period)
            self.assertNotIn('always_buy', period)
        self.assertIn('paper', result['variants'][1]['report']['periods']['test'])

    def test_daily_actual_close_fill_trading_is_rejected_not_published(self):
        value = fixture()
        value['variants'][0]['report']['periods']['test']['paper'].update(
            trades=1, net_return_pct=10, max_drawdown_pct=1)
        self.save(value)
        self.assertIsNone(service.load_artifact(NOW))

    def test_missing_model_or_invalid_latest_has_no_fabricated_fallback(self):
        self.assertEqual(service.payload(NOW)['status'], 'unavailable')
        self.save()
        (self.folder / 'multiyear-morning-v1-99999999.json').write_text('{bad', encoding='utf-8')
        self.assertEqual(service.payload(NOW)['status'], 'unavailable')
        self.assertNotIn('coverage', service.payload(NOW))

    def test_integrity_sources_version_and_future_naive_created_fail_closed(self):
        for change in ({'created_at': (NOW + timedelta(seconds=1)).isoformat()},
                       {'created_at': '2026-10-23T12:00:00'}, {'entry_allowed': True},
                       {'version': 'unknown'}, {'source_sha256': {'study': 'b' * 64}}):
            with self.subTest(change=change):
                value = fixture()
                value.update(change)
                self.save(value)
                self.assertIsNone(service.load_artifact(NOW))
        value = self.save()
        value['note'] = 'changed without rehash'
        self.path.write_text(json.dumps(value), encoding='utf-8')
        self.assertIsNone(service.load_artifact(NOW))

    def test_fixed_model_kinds_and_clocks_cannot_change_or_duplicate(self):
        for alteration in ('missing', 'duplicate', 'cutoff', 'entry', 'unknown', 'kind'):
            value = fixture()
            if alteration == 'missing':
                value['variants'].pop()
            elif alteration == 'duplicate':
                value['variants'][1] = copy.deepcopy(value['variants'][0])
            elif alteration in ('cutoff', 'entry'):
                value['variants'][1][alteration] = '17:44'
            elif alteration == 'kind':
                value['variants'][0]['kind'] = 'intraday'
            else:
                value['variants'][0]['id'] = 'fitted_best'
            with self.subTest(alteration=alteration):
                self.save(value)
                self.assertIsNone(service.load_artifact(NOW))

    def test_coverage_rejects_bad_counts_and_unordered_naive_future_dates(self):
        cases = [{'bars': True}, {'bars': -1}, {'bars': 0}, {'bars': 1}, {'bars': 2**53},
                 {'first_actual': '2018-01-01T00:00:00'},
                 {'first_actual': '2027-01-01T00:00:00+00:00'},
                 {'first_actual': '1999-01-01T00:00:00+00:00'},
                 {'to': '2000-01-01T00:00:00+00:00'},
                 {'to': '2028-01-01T00:00:00+00:00', 'last_actual': '2027-01-01T00:00:00+00:00'}]
        for change in cases:
            with self.subTest(change=change):
                value = fixture()
                value['coverage']['SBER']['day'].update(change)
                self.save(value)
                self.assertIsNone(service.load_artifact(NOW))

    def test_absent_coverage_dates_only_allowed_for_zero_candles(self):
        value = fixture()
        value['coverage']['SBER']['day'].update(bars=0, first_actual=None, last_actual=None)
        self.save(value)
        self.assertEqual(service.payload(NOW)['coverage']['SBER']['day']['bars'], 0)
        value['coverage']['SBER']['day']['bars'] = 7
        self.save(value)
        self.assertIsNone(service.load_artifact(NOW))

    def test_empty_or_bad_stock_coverage_rejected(self):
        for coverage in ({}, {'<script>': fixture()['coverage']['SBER']}, {'SBER': {}}, []):
            with self.subTest(coverage=coverage):
                value = fixture()
                value['coverage'] = coverage
                self.save(value)
                self.assertIsNone(service.load_artifact(NOW))

    def test_nonfinite_metric_and_false_profit_with_unknown_outcomes_rejected(self):
        for alteration in ('metric', 'unknown_profit', 'zero_trades'):
            value = fixture()
            p = value['variants'][1]['report']['periods']['test']
            if alteration == 'metric':
                p['mae_bps'] = 'Infinity'
            elif alteration == 'unknown_profit':
                p['paper'].update(status='incomplete', unknown_trades=1)
            else:
                p['paper']['trades'] = 0
            with self.subTest(alteration=alteration):
                self.save(value)
                self.assertIsNone(service.load_artifact(NOW))

    def test_unknown_selected_outcomes_are_kept_with_null_returns(self):
        value = fixture()
        paper = value['variants'][1]['report']['periods']['test']['paper']
        paper.update(unknown_trades=1, status='incomplete', net_return_pct=None,
                     max_drawdown_pct=None, stress_return_pct=None, stress_unknown_trades=1)
        self.save(value)
        result = service.payload(NOW)
        self.assertEqual(result['status'], 'historical')
        projected = result['variants'][1]['report']['periods']['test']['paper']
        self.assertEqual(projected['unknown_trades'], 1)
        self.assertIsNone(projected['net_return_pct'])

    def test_size_bound_and_same_path_tamper_rechecked_on_every_read(self):
        self.save()
        with patch.object(service, 'MAX_ARTIFACT_BYTES', 10):
            self.assertIsNone(service.load_artifact(NOW))
        self.assertEqual(service.payload(NOW)['status'], 'historical')
        raw = self.path.read_text(encoding='utf-8')
        self.path.write_text(raw.replace('multiyear_morning_v1', 'multiyear_morning_v2'), encoding='utf-8')
        self.assertIsNone(service.load_artifact(NOW))

    def test_local_get_no_store_remote_and_post_are_denied_without_model_reads(self):
        factory = RequestFactory()
        with patch('dashboard.multiyear_views.payload', return_value={'status': 'historical', 'entry_allowed': False}) as produce:
            for addr in ('127.0.0.1', '::1'):
                response = report(factory.get('/api/multiyear-research/', REMOTE_ADDR=addr))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response['Cache-Control'], 'no-store')
            self.assertEqual(produce.call_count, 2)
            denied = report(factory.get('/api/multiyear-research/', REMOTE_ADDR='203.0.113.1'))
            self.assertEqual(denied.status_code, 403)
            self.assertEqual(denied['Cache-Control'], 'no-store')
            self.assertEqual(report(factory.post('/api/multiyear-research/', REMOTE_ADDR='127.0.0.1')).status_code, 405)
            self.assertEqual(produce.call_count, 2)
