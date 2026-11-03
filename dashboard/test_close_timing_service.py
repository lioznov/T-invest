import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from django.test import RequestFactory, SimpleTestCase, override_settings

from . import close_timing_service as service
from .close_timing_views import report


NOW = datetime(2026, 10, 23, 15, tzinfo=timezone.utc)
SPEC = SimpleNamespace(VERSION='close_timing_v1', source_hashes=lambda: {'study': 'a' * 64})


def fixture():
    paper = {'trades': 2, 'unknown_trades': 0, 'net_return_pct': -0.3,
             'max_drawdown_pct': 0.5, 'status': 'ok', 'stress_return_pct': -0.4,
             'stress_trades': 2, 'stress_unknown_trades': 0}
    period = {'samples': 10, 'days': 5, 'eligible_forecasts': 12, 'unknown_targets': 2,
              'direction_nonzero_samples': 10, 'mae_bps': 45.0, 'baseline_mae_bps': 42.0,
              'mean_baseline_mae_bps': 44.0, 'direction_accuracy_pct': 50.0,
              'coverage_pct': 80.0, 'interval_width_bps': 160.0, 'abstention_pct': 90.0,
              'start': '2026-09-01', 'end_exclusive': '2026-10-03',
              'paper': paper, 'always_buy': copy.deepcopy(paper), 'note': 'Нет новых сигналов.'}
    sample_report = {'trained_rows': 200, 'calibration_rows': 50,
                     'periods': {'test': copy.deepcopy(period), 'later': copy.deepcopy(period)}}
    return {'version': SPEC.VERSION, 'entry_allowed': False,
            'created_at': (NOW - timedelta(hours=1)).isoformat(),
            'source_sha256': SPEC.source_hashes(), 'note': 'Прошлая история, не доказательство прибыли.',
            'paired_note': 'Пересечение входных примеров, неизвестные исходы сохранены.',
            'variants': [{'id': name, 'cutoff': clocks[0], 'entry': clocks[1],
                          'label': name, 'report': copy.deepcopy(sample_report),
                          'paired_report': copy.deepcopy(sample_report),
                          'model': {'weights': [666]}, 'path': 'private/path'}
                         for name, clocks in service.VARIANTS.items()]}


class CloseTimingServiceTests(SimpleTestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.folder = self.root / 'dashboard' / 'ai_models'
        self.folder.mkdir(parents=True)
        self.path = self.folder / 'close-timing-v1-20261023-140000.json'
        config = override_settings(BASE_DIR=self.root)
        config.enable()
        self.addCleanup(config.disable)
        spec = patch.object(service, 'study_spec', return_value=SPEC)
        spec.start()
        self.addCleanup(spec.stop)

    def save(self, value=None, path=None):
        value = copy.deepcopy(fixture() if value is None else value)
        value['artifact_sha256'] = service.digest({k: v for k, v in value.items()
                                                 if k != 'artifact_sha256'})
        (path or self.path).write_text(json.dumps(value), encoding='utf-8')
        return value

    def test_projection_is_historical_read_only_and_keeps_missing_counts(self):
        self.save()
        value = service.payload(NOW)
        self.assertEqual(value['status'], 'historical')
        self.assertIs(value['entry_allowed'], False)
        self.assertEqual([v['id'] for v in value['variants']], list(service.VARIANTS))
        self.assertEqual(value['variants'][0]['paired_report']['periods']['test']['unknown_targets'], 2)
        for private in ('private/path', 'model', 'weights', 'source_sha256', 'artifact_sha256'):
            self.assertNotIn(private, json.dumps(value))
        self.assertNotIn(str(self.root), json.dumps(value))

    def test_missing_artifact_does_not_fabricate_a_report(self):
        result = service.payload(NOW)
        self.assertEqual(result['status'], 'unavailable')
        self.assertNotIn('variants', result)
        self.assertIs(result['entry_allowed'], False)

    def test_invalid_latest_does_not_fall_back(self):
        self.save()
        newer = self.folder / 'close-timing-v1-99999999-999999.json'
        newer.write_text('{broken', encoding='utf-8')
        self.assertEqual(service.payload(NOW)['status'], 'unavailable')

    def test_tamper_and_changed_sources_are_rejected(self):
        value = self.save()
        value['note'] = 'changed without rehash'
        self.path.write_text(json.dumps(value), encoding='utf-8')
        self.assertIsNone(service.load_artifact(NOW))
        value = fixture()
        value['source_sha256']['study'] = 'b' * 64
        self.save(value)
        self.assertIsNone(service.load_artifact(NOW))

    def test_newest_file_is_rechecked_even_when_size_and_path_stay_the_same(self):
        self.save()
        self.assertEqual(service.payload(NOW)['status'], 'historical')
        raw = self.path.read_text(encoding='utf-8')
        self.path.write_text(raw.replace('close_timing_v1', 'close_timing_v2'), encoding='utf-8')
        self.assertEqual(service.payload(NOW)['status'], 'unavailable')

    def test_future_naive_created_and_promoted_entries_fail_closed(self):
        for change in ({'created_at': (NOW + timedelta(seconds=1)).isoformat()},
                       {'created_at': '2026-10-23T12:00:00'},
                       {'entry_allowed': True}, {'version': 'unknown'}):
            with self.subTest(change=change):
                value = fixture()
                value.update(change)
                self.save(value)
                self.assertIsNone(service.load_artifact(NOW))

    def test_fixed_variants_and_clocks_cannot_change_or_duplicate(self):
        for alteration in ('missing', 'duplicate', 'cutoff', 'entry', 'unknown'):
            value = fixture()
            if alteration == 'missing':
                value['variants'].pop()
            elif alteration == 'duplicate':
                value['variants'][1] = copy.deepcopy(value['variants'][0])
            elif alteration in ('cutoff', 'entry'):
                value['variants'][0][alteration] = '17:44'
            else:
                value['variants'][0]['id'] = 'fitted_best'
            with self.subTest(alteration=alteration):
                self.save(value)
                self.assertIsNone(service.load_artifact(NOW))

    def test_nonfinite_metrics_and_impossible_paper_returns_rejected(self):
        for name in ('nonfinite', 'unknown_profit', 'zero_trades', 'bad_direction', 'bad_samples'):
            value = fixture()
            p = value['variants'][0]['report']['periods']['test']
            if name == 'nonfinite':
                p['mae_bps'] = 'NaN'
            elif name == 'unknown_profit':
                p['paper']['unknown_trades'] = 1
                p['paper']['status'] = 'incomplete'
            elif name == 'zero_trades':
                p['paper']['trades'] = 0
            elif name == 'bad_direction':
                p['direction_accuracy_pct'] = 100.1
            else:
                p['eligible_forecasts'] = 500
            with self.subTest(name=name):
                self.save(value)
                self.assertIsNone(service.load_artifact(NOW))

    def test_unknowns_remain_explicit_without_fabricated_pnl(self):
        value = fixture()
        paper = value['variants'][0]['paired_report']['periods']['test']['paper']
        paper.update(unknown_trades=1, status='incomplete', net_return_pct=None,
                     max_drawdown_pct=None, stress_return_pct=None, stress_unknown_trades=1)
        self.save(value)
        result = service.payload(NOW)
        self.assertEqual(result['status'], 'historical')
        projected = result['variants'][0]['paired_report']['periods']['test']['paper']
        self.assertEqual(projected['unknown_trades'], 1)
        self.assertIsNone(projected['net_return_pct'])

    def test_size_limit_applies_before_parsing(self):
        self.save()
        with patch.object(service, 'MAX_ARTIFACT_BYTES', 10):
            self.assertIsNone(service.load_artifact(NOW))

    def test_local_get_no_store_remote_deny_and_post_deny(self):
        factory = RequestFactory()
        with patch('dashboard.close_timing_views.payload', return_value={'status': 'historical', 'entry_allowed': False}) as produce:
            for addr in ('127.0.0.1', '::1'):
                response = report(factory.get('/api/close-timing/', REMOTE_ADDR=addr))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response['Cache-Control'], 'no-store')
            self.assertEqual(produce.call_count, 2)
            denied = report(factory.get('/api/close-timing/', REMOTE_ADDR='203.0.113.1'))
            self.assertEqual(denied.status_code, 403)
            self.assertEqual(denied['Cache-Control'], 'no-store')
            self.assertEqual(report(factory.post('/api/close-timing/', REMOTE_ADDR='127.0.0.1')).status_code, 405)
            self.assertEqual(produce.call_count, 2)
