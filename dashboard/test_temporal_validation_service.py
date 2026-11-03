import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from django.test import RequestFactory, SimpleTestCase, override_settings

from . import temporal_validation_service as service
from .temporal_validation_views import report


NOW = datetime(2026, 10, 23, 15, tzinfo=timezone.utc)
SPEC = SimpleNamespace(VERSION='morning_temporal_validation_v1', source_hashes=lambda: {'study': 'a' * 64})
SPEC.FOLDS = tuple({'id': name, 'periods': {
    'train': {'start': '2018-03-07', 'end_exclusive': f'{int(bounds[0][:4])-1}-01-01'},
    'calibration': {'start': f'{int(bounds[0][:4])-1}-01-01', 'end_exclusive': bounds[0]},
    'test': {'start': bounds[0], 'end_exclusive': bounds[1]},
}} for name, bounds in service.TESTS.items())


def fixture():
    paper = {'trades': 2, 'unknown_trades': 0, 'status': 'ok', 'net_return_pct': -0.3,
             'max_drawdown_pct': 0.5, 'stress_return_pct': -0.4,
             'stress_trades': 2, 'stress_unknown_trades': 0}
    stats = {'samples': 10, 'days': 5, 'eligible_forecasts': 12, 'unknown_targets': 2,
             'mae_bps': 45.0, 'baseline_mae_bps': 42.0, 'mean_baseline_mae_bps': 44.0,
             'direction_accuracy_pct': 50.0, 'direction_nonzero_samples': 9,
             'coverage_pct': 80.0, 'interval_width_bps': 160.0, 'abstention_pct': 90.0,
             'rmse_bps': 80.0, 'baseline_rmse_bps': 75.0, 'mean_baseline_rmse_bps': 79.0,
             'mape_pct': 0.5, 'baseline_mape_pct': 0.45, 'mean_baseline_mape_pct': 0.48,
             'mean_signed_error_bps': -5.0, 'direction_baseline_pct': 60.0,
             'direction_baseline_label': 'Всегда рост',
             'actual_up': 6, 'actual_down': 3, 'actual_zero': 1,
             'paper': paper, 'always_buy': copy.deepcopy(paper), 'note': 'Прошлая история.'}
    stock = {'samples': 10, 'mae_rub': 1.5, 'baseline_mae_rub': 1.3,
             'mean_baseline_mae_rub': 1.4, 'rmse_rub': 2.5, 'baseline_rmse_rub': 2.3,
             'mean_baseline_rmse_rub': 2.4, 'mape_pct': 0.5,
             'baseline_mape_pct': 0.45, 'mean_baseline_mape_pct': 0.48}
    def example(day):
        local = datetime.fromisoformat(day + 'T17:45:00+03:00')
        return {'ticker': 'SBER', 'as_of': local.isoformat(),
                'target_at': (local + timedelta(days=1)).replace(hour=10, minute=0).isoformat(),
                'anchor_price': 300.0, 'predicted_price': 303.0, 'actual_price': 301.0,
                'absolute_error_rub': 2.0, 'absolute_error_pct': 2 / 301 * 100, 'kind': 'latest',
                'private_path': 'private/example.json'}
    folds = []
    for name, test in service.TESTS.items():
        year = int(test[0][:4])
        periods = {'train': {'start': '2018-03-07', 'end_exclusive': f'{year-1}-01-01'},
                   'calibration': {'start': f'{year-1}-01-01', 'end_exclusive': f'{year}-01-01'},
                   'test': {'start': test[0], 'end_exclusive': test[1]}}
        day = {2024: '2024-10-21', 2025: '2025-10-20', 2026: '2026-10-19'}[year]
        primary = {**copy.deepcopy(stats), **periods['test']}
        current = {**copy.deepcopy(stats), 'start': '2026-01-01', 'end_exclusive': '2026-10-23'}
        folds.append({'id': name, 'label': str(year), 'periods': periods,
                      'trained_rows': 2000, 'calibration_rows': 500,
                      'model': {'weights': [666]}, 'report': primary,
                      'stocks': {'SBER': copy.deepcopy(stock)}, 'examples': [example(day)],
                      'current_year_report': current,
                      'current_year_stocks': {'SBER': copy.deepcopy(stock)},
                      'current_year_examples': [example('2026-10-19')]})
    coverage = {'bars': 200000, 'requested_from': '2018-01-01T00:00:00Z',
                'to': '2026-10-23T00:00:00Z', 'first_actual': '2018-03-01T00:00:00Z',
                'last_actual': '2026-10-22T23:45:00+03:00', 'chunks': ['private/data.json']}
    return {'version': SPEC.VERSION, 'created_at': (NOW - timedelta(hours=1)).isoformat(),
            'entry_allowed': False, 'source_sha256': SPEC.source_hashes(),
            'protocol_sha256': 'b' * 64, 'folds': folds, 'coverage': {'SBER': coverage},
            'note': 'Не доказательство будущей прибыли.', 'publication_note': 'Ретроспектива.'}


class TemporalValidationServiceTests(SimpleTestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.folder = self.root / 'dashboard' / 'ai_models'
        self.folder.mkdir(parents=True)
        self.path = self.folder / 'morning-temporal-v1-20261023-140000.json'
        config = override_settings(BASE_DIR=self.root)
        config.enable()
        self.addCleanup(config.disable)
        spec = patch.object(service, 'model_spec', return_value=SPEC)
        spec.start()
        self.addCleanup(spec.stop)

    def save(self, value=None, path=None):
        value = copy.deepcopy(fixture() if value is None else value)
        value['artifact_sha256'] = service.digest({k: v for k, v in value.items() if k != 'artifact_sha256'})
        (path or self.path).write_text(json.dumps(value), encoding='utf-8')
        return value

    def test_projection_keeps_both_comparisons_metrics_actuals_and_no_private_fields(self):
        self.save()
        value = service.payload(NOW)
        self.assertEqual(value['status'], 'historical')
        self.assertIs(value['entry_allowed'], False)
        self.assertEqual([f['id'] for f in value['folds']], list(service.TESTS))
        fold = value['folds'][0]
        self.assertEqual(fold['report']['baseline_mape_pct'], 0.45)
        self.assertEqual(fold['current_year_report']['actual_up'], 6)
        self.assertEqual(fold['stocks']['SBER']['baseline_rmse_rub'], 2.3)
        self.assertEqual(fold['examples'][0]['actual_price'], 301)
        for secret in ('weights', 'private/', 'source_sha256', 'artifact_sha256', 'protocol_sha256'):
            self.assertNotIn(secret, json.dumps(value))
        self.assertNotIn(str(self.root), json.dumps(value))

    def test_missing_or_broken_latest_never_falls_back_to_older_report(self):
        self.assertEqual(service.payload(NOW)['status'], 'unavailable')
        self.save()
        (self.folder / 'morning-temporal-v1-99999999.json').write_text('{bad', encoding='utf-8')
        value = service.payload(NOW)
        self.assertEqual(value['status'], 'unavailable')
        self.assertNotIn('folds', value)

    def test_bad_integrity_version_sources_protocol_and_future_creation_rejected(self):
        for change in ({'created_at': (NOW + timedelta(seconds=1)).isoformat()},
                       {'created_at': '2026-10-23T12:00:00'}, {'entry_allowed': True},
                       {'version': 'unknown'}, {'source_sha256': {'study': 'c' * 64}},
                       {'protocol_sha256': 'bad'}):
            value = fixture()
            value.update(change)
            with self.subTest(change=change):
                self.save(value)
                self.assertIsNone(service.load_artifact(NOW))
        value = self.save()
        value['note'] = 'changed without rehash'
        self.path.write_text(json.dumps(value), encoding='utf-8')
        self.assertIsNone(service.load_artifact(NOW))

    def test_missing_duplicate_or_changed_fold_and_overlapping_windows_rejected(self):
        for change in ('missing', 'duplicate', 'unknown', 'overlap', 'wrong_test', 'bad_date'):
            value = fixture()
            f = value['folds'][0]
            if change == 'missing':
                value['folds'].pop()
            elif change == 'duplicate':
                value['folds'][1] = copy.deepcopy(f)
            elif change == 'unknown':
                f['id'] = 'winner'
            elif change == 'overlap':
                f['periods']['train']['end_exclusive'] = '2024-01-01'
            elif change == 'wrong_test':
                f['periods']['test']['start'] = '2024-02-01'
            else:
                f['periods']['train']['start'] = '2018-02-30'
            with self.subTest(change=change):
                self.save(value)
                self.assertIsNone(service.load_artifact(NOW))

    def test_extras_and_stock_counts_must_be_finite_consistent_and_nonnegative(self):
        for change in ('rmse', 'mape', 'direction', 'label', 'moves', 'stocks'):
            value = fixture()
            f = value['folds'][0]
            if change == 'rmse':
                f['report']['rmse_bps'] = 'NaN'
            elif change == 'mape':
                f['report']['baseline_mape_pct'] = -1
            elif change == 'direction':
                f['report']['direction_baseline_pct'] = 101
            elif change == 'label':
                f['report']['direction_baseline_label'] = 'always choose winner'
            elif change == 'moves':
                f['report']['actual_up'] = 7
            else:
                f['stocks']['SBER']['samples'] = 9
            with self.subTest(change=change):
                self.save(value)
                self.assertIsNone(service.load_artifact(NOW))

    def test_negative_and_zero_predictions_are_not_clamped_or_hidden(self):
        for prediction in (-2.0, 0.0):
            value = fixture()
            example = value['folds'][0]['examples'][0]
            example.update(predicted_price=prediction, absolute_error_rub=301 - prediction,
                           absolute_error_pct=(301 - prediction) / 301 * 100)
            self.save(value)
            self.assertEqual(service.payload(NOW)['folds'][0]['examples'][0]['predicted_price'], prediction)

    def test_example_actual_prices_errors_dates_and_duplicates_must_be_consistent(self):
        for change in ('actual', 'error', 'pct', 'future', 'clock', 'duplicate', 'kind'):
            value = fixture()
            row = value['folds'][0]['examples'][0]
            if change == 'actual':
                row['actual_price'] = 0
            elif change == 'error':
                row['absolute_error_rub'] = 1
            elif change == 'pct':
                row['absolute_error_pct'] = 2 / 300 * 100
            elif change == 'future':
                row['target_at'] = '2027-01-01T10:00:00+03:00'
            elif change == 'clock':
                row['as_of'] = '2024-10-21T17:30:00+03:00'
            elif change == 'duplicate':
                value['folds'][0]['examples'].append(copy.deepcopy(row))
            else:
                row['kind'] = 'best_profit'
            with self.subTest(change=change):
                self.save(value)
                self.assertIsNone(service.load_artifact(NOW))

    def test_unknown_outcomes_preserved_and_false_profit_cannot_be_published(self):
        value = fixture()
        paper = value['folds'][0]['current_year_report']['paper']
        paper.update(status='incomplete', unknown_trades=1, net_return_pct=None,
                     max_drawdown_pct=None, stress_unknown_trades=1, stress_return_pct=None)
        self.save(value)
        result = service.payload(NOW)
        self.assertEqual(result['status'], 'historical')
        self.assertEqual(result['folds'][0]['current_year_report']['paper']['unknown_trades'], 1)
        paper['net_return_pct'] = 999
        self.save(value)
        self.assertIsNone(service.load_artifact(NOW))

    def test_coverage_counts_and_actual_ranges_are_strict(self):
        for change in ({'bars': True}, {'bars': -1}, {'bars': 0}, {'bars': 2**53},
                       {'first_actual': '2018-03-01T00:00:00'},
                       {'first_actual': '2017-01-01T00:00:00Z'},
                       {'last_actual': '2027-01-01T00:00:00Z', 'to': '2028-01-01T00:00:00Z'}):
            value = fixture()
            value['coverage']['SBER'].update(change)
            with self.subTest(change=change):
                self.save(value)
                self.assertIsNone(service.load_artifact(NOW))

    def test_size_bound_and_same_path_tampering_rechecked(self):
        self.save()
        with patch.object(service, 'MAX_ARTIFACT_BYTES', 10):
            self.assertIsNone(service.load_artifact(NOW))
        self.assertEqual(service.payload(NOW)['status'], 'historical')
        raw = self.path.read_text(encoding='utf-8')
        self.path.write_text(raw.replace('morning_temporal_validation_v1', 'morning_temporal_validation_v2'), encoding='utf-8')
        self.assertIsNone(service.load_artifact(NOW))

    def test_empty_stock_projection_is_allowed_only_when_no_known_samples_exist(self):
        self.assertEqual(service._stocks({}, 0), {})
        with self.assertRaises(ValueError):
            service._stocks({}, 1)

    def test_fixed_training_and_report_periods_are_checked_not_only_chronology(self):
        for field in ('train', 'primary_report', 'common_report'):
            value = fixture()
            fold = value['folds'][0]
            if field == 'train':
                fold['periods']['train']['start'] = '2018-04-01'
            elif field == 'primary_report':
                fold['report']['start'] = '2024-02-01'
            else:
                fold['current_year_report']['end_exclusive'] = '2026-10-22'
            with self.subTest(field=field):
                self.save(value)
                self.assertIsNone(service.load_artifact(NOW))

    def test_example_future_label_cannot_cross_the_test_period_boundary(self):
        value = fixture()
        value['folds'][0]['examples'][0].update(
            as_of='2024-12-31T17:45:00+03:00', target_at='2025-01-01T10:00:00+03:00')
        self.save(value)
        self.assertIsNone(service.load_artifact(NOW))

    def test_local_get_no_store_and_remote_post_head_rejected_without_model_reads(self):
        factory = RequestFactory()
        with patch('dashboard.temporal_validation_views.payload', return_value={'status': 'historical', 'entry_allowed': False}) as produce:
            for addr in ('127.0.0.1', '::1'):
                response = report(factory.get('/api/temporal-validation/', REMOTE_ADDR=addr))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response['Cache-Control'], 'no-store')
            self.assertEqual(produce.call_count, 2)
            denied = report(factory.get('/api/temporal-validation/', REMOTE_ADDR='203.0.113.1'))
            self.assertEqual(denied.status_code, 403)
            self.assertEqual(denied['Cache-Control'], 'no-store')
            for request in (factory.post('/api/temporal-validation/'), factory.head('/api/temporal-validation/')):
                self.assertEqual(report(request).status_code, 405)
            self.assertEqual(produce.call_count, 2)
