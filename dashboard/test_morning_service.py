import copy
import json
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from django.test import RequestFactory, SimpleTestCase, override_settings

from . import morning_service as service
from .morning_views import forecast


NOW = datetime(2026, 10, 22, 17, 50, tzinfo=service.MSK)
SPEC = SimpleNamespace(VERSION='morning_ridge_v1', FEATURES=('return_1d', 'range_1d'),
                       RULES={'as_of_msk': '17:45', 'target_msk': '10:00'},
                       source_hashes=lambda: {'morning_model.py': 'a' * 64})


def fixture():
    period = {'samples': 70, 'days': 10, 'mae_bps': 42.0, 'baseline_mae_bps': 45.0,
              'direction_accuracy_pct': 52.0, 'coverage_pct': 80.0,
              'paper': {'trades': 12, 'net_return_pct': -0.7, 'max_drawdown_pct': 2.1,
                        'unknown_trades': 0, 'stress_return_pct': -1.1}}
    return {'version': SPEC.VERSION, 'features': list(SPEC.FEATURES), 'rules': dict(SPEC.RULES),
            'source_sha256': SPEC.source_hashes(), 'entry_allowed': False,
            'trained_at': (NOW - timedelta(days=1)).isoformat(), 'tickers': ['SBER', 'GAZP'],
            'model': {'center': [0, 0], 'scale': [1, 1], 'weights': [0, 0.2, 0.1],
                      'interval_abs_pct': 1.5},
            'report': {'trained_rows': 700, 'calibration_rows': 100,
                       'periods': {'test': copy.deepcopy(period), 'later': copy.deepcopy(period)},
                       'note': 'Два более поздних периода; доходность пока не доказана.'},
            'forecasts': {'SBER': {'as_of': NOW.replace(hour=17, minute=45).isoformat(),
                                   'target_at': (NOW + timedelta(days=1)).replace(hour=10, minute=0).isoformat(),
                                   'anchor_price': 300.0, 'predicted_price': 301.5,
                                   'lower_price': 297.0, 'upper_price': 306.0,
                                   'predicted_change_pct': 0.5, 'direction': 'uncertain', 'historical': False}},
            'note': 'Исследование без реальных сделок.'}


class MorningServiceTests(SimpleTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.model_dir = self.root / 'dashboard' / 'ai_models'
        self.model_dir.mkdir(parents=True)
        self.path = self.model_dir / 'morning-ridge-v1-20261022-170000.json'
        settings_patch = override_settings(BASE_DIR=self.root)
        settings_patch.enable()
        self.addCleanup(settings_patch.disable)
        spec_patch = patch.object(service, 'model_spec', return_value=SPEC)
        spec_patch.start()
        self.addCleanup(spec_patch.stop)

    def save(self, value=None, path=None):
        value = copy.deepcopy(value if value is not None else fixture())
        value['artifact_sha256'] = service.digest({k: v for k, v in value.items() if k != 'artifact_sha256'})
        (path or self.path).write_text(json.dumps(value), encoding='utf-8')
        return value

    def test_current_projection_is_research_only_and_does_not_leak_internal_values(self):
        self.save()
        result = service.forecast_payload('SBER', now=NOW)
        self.assertEqual(result['status'], 'ok')
        self.assertFalse(result['entry_allowed'])
        self.assertEqual(result['forecast']['anchor_price'], 300)
        self.assertEqual(result['report']['periods']['test']['samples'], 70)
        for key in ('model', 'weights', 'center', 'scale', 'artifact_sha256', 'source_sha256'):
            self.assertNotIn(key, json.dumps(result))
        self.assertNotIn(str(self.root), json.dumps(result))

    def test_demo_never_contains_a_numerical_forecast(self):
        self.save()
        result = service.forecast_payload('SBER', 'demo', NOW)
        self.assertEqual(result['status'], 'demo')
        self.assertNotIn('forecast', result)
        self.assertIn('report', result)
        self.assertFalse(result['entry_allowed'])

    def test_missing_model_demo_and_live_are_not_fabricated(self):
        for mode, status in (('demo', 'demo'), ('live', 'unavailable')):
            result = service.forecast_payload('SBER', mode, NOW)
            self.assertEqual(result['status'], status)
            self.assertNotIn('forecast', result)

    def test_unknown_ticker_and_missing_saved_forecast_have_no_price(self):
        self.save()
        for ticker in ('UNKNOWN', 'GAZP'):
            result = service.forecast_payload(ticker, now=NOW)
            self.assertEqual(result['status'], 'unavailable')
            self.assertNotIn('forecast', result)
            self.assertIn('report', result)

    def test_expired_forecast_is_history_without_relabeling_its_date(self):
        value = self.save()
        for later in (NOW.replace(hour=18, minute=0), NOW + timedelta(minutes=26), NOW + timedelta(days=1)):
            result = service.forecast_payload('SBER', now=later)
            self.assertEqual(result['status'], 'historical')
            self.assertTrue(result['forecast']['historical'])
            self.assertEqual(result['forecast']['as_of'], value['forecasts']['SBER']['as_of'])
            self.assertEqual(result['forecast']['target_at'], value['forecasts']['SBER']['target_at'])
            self.assertFalse(value['forecasts']['SBER']['historical'])

    def test_explicit_historical_forecast_is_never_current_even_before_target(self):
        value = fixture()
        value['forecasts']['SBER']['historical'] = True
        self.save(value)
        self.assertEqual(service.forecast_payload('SBER', now=NOW)['status'], 'historical')

    def test_friday_weekend_wrong_cutoff_and_multi_day_target_are_not_current(self):
        for day, hour, minute, horizon in ((23, 17, 45, 1), (24, 17, 45, 1),
                                            (22, 18, 0, 1), (22, 17, 45, 4)):
            with self.subTest(day=day, hour=hour, minute=minute, horizon=horizon):
                value = fixture()
                origin = NOW.replace(day=day, hour=hour, minute=minute)
                value['forecasts']['SBER']['as_of'] = origin.isoformat()
                value['forecasts']['SBER']['target_at'] = (origin + timedelta(days=horizon)).replace(hour=10, minute=0).isoformat()
                self.save(value)
                result = service.forecast_payload('SBER', now=origin + timedelta(minutes=5))
                self.assertEqual(result['status'], 'historical')

    def test_future_origin_or_future_known_outcome_is_rejected(self):
        for kind in ('future_origin', 'future_actual'):
            value = fixture()
            item = value['forecasts']['SBER']
            if kind == 'future_origin':
                item['as_of'] = (NOW + timedelta(minutes=1)).isoformat()
            else:
                item.update(historical=True, actual_price=302)
            self.save(value)
            self.assertIsNone(service.load_artifact(NOW))

    def test_historical_actual_price_can_be_displayed_after_its_time(self):
        value = fixture()
        value['forecasts']['SBER'].update(historical=True, actual_price=302)
        self.save(value)
        result = service.forecast_payload('SBER', now=NOW + timedelta(days=1))
        self.assertEqual(result['status'], 'historical')
        self.assertEqual(result['forecast']['actual_price'], 302)

    def test_tampering_is_rejected_even_if_size_and_mtime_are_unchanged(self):
        value = self.save()
        stat = self.path.stat()
        value['model']['weights'][1] = 0.8
        self.path.write_text(json.dumps(value), encoding='utf-8')
        import os
        os.utime(self.path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.assertEqual(self.path.stat().st_size, stat.st_size)
        self.assertIsNone(service.load_artifact(NOW))

    def test_changed_source_features_rules_and_entry_permission_are_rejected(self):
        changes = ({'source_sha256': {'morning_model.py': 'b' * 64}}, {'features': ['wrong']},
                   {'rules': {}}, {'entry_allowed': True}, {'version': 'wrong'},
                   {'trained_at': (NOW + timedelta(days=1)).isoformat()})
        for change in changes:
            with self.subTest(change=change):
                self.save({**fixture(), **change})
                self.assertIsNone(service.load_artifact(NOW))

    def test_bad_shapes_boolean_numbers_and_bad_timestamps_are_rejected(self):
        changes = ((('model', 'scale'), [1, 0]), (('model', 'weights'), [0]),
                   (('model', 'center'), [True, 0]), (('model', 'interval_abs_pct'), -1),
                   (('trained_at',), '2026-10-21T17:45:00'),
                   (('forecasts', 'SBER', 'as_of'), '2026-10-22T17:45:00'),
                   (('forecasts', 'SBER', 'target_at'), '2026-10-21T10:00:00+03:00'),
                   (('forecasts', 'SBER', 'lower_price'), 305),
                   (('forecasts', 'SBER', 'anchor_price'), 0),
                   (('forecasts', 'SBER', 'predicted_change_pct'), 20),
                   (('forecasts', 'SBER', 'direction'), 'down'),
                   (('report', 'trained_rows'), True),
                   (('report', 'periods', 'test', 'coverage_pct'), 101))
        for keys, bad in changes:
            with self.subTest(keys=keys):
                value = fixture()
                target = value
                for key in keys[:-1]:
                    target = target[key]
                target[keys[-1]] = bad
                self.save(value)
                self.assertIsNone(service.load_artifact(NOW))

    def test_oversized_malformed_or_nonfinite_json_is_rejected(self):
        for raw in (b' ' * (service.MAX_ARTIFACT_BYTES + 1), b'{bad json', b'[]', b'{"x": NaN}'):
            self.path.write_bytes(raw)
            self.assertIsNone(service.load_artifact(NOW))

    def test_latest_broken_artifact_does_not_fall_back_to_an_older_one(self):
        self.save()
        (self.model_dir / 'morning-ridge-v1-99999999.json').write_text('{}', encoding='utf-8')
        self.assertIsNone(service.load_artifact(NOW))

    def test_reference_outside_model_directory_is_rejected(self):
        self.save()
        actual_resolve = Path.resolve
        outside = self.root / 'external.json'

        def resolve(path, *args, **kwargs):
            return outside if path == self.path else actual_resolve(path, *args, **kwargs)

        with patch.object(Path, 'resolve', resolve):
            self.assertIsNone(service.load_artifact(NOW))

    def test_unknown_report_keys_are_not_returned_to_browser(self):
        value = fixture()
        value['report']['secret_path'] = 'DO_NOT_RETURN'
        value['report']['periods']['test']['model_weights'] = 'DO_NOT_RETURN'
        self.save(value)
        self.assertNotIn('DO_NOT_RETURN', json.dumps(service.forecast_payload('SBER', now=NOW)))

    def test_empty_period_metrics_are_missing_not_zero(self):
        value = fixture()
        value['report']['periods']['test'].update(samples=0, days=0, mae_bps=None,
                                                  baseline_mae_bps=None,
                                                  direction_accuracy_pct=None, coverage_pct=None)
        self.save(value)
        result = service.forecast_payload('SBER', now=NOW)
        self.assertIsNone(result['report']['periods']['test']['mae_bps'])

    def test_optional_actual_none_and_full_paper_coverage_are_supported(self):
        value = fixture()
        value['forecasts']['SBER'].update(historical=True, actual_price=None)
        period = value['report']['periods']['test']
        period.update(unknown_targets=4, eligible_forecasts=74, direction_nonzero_samples=69,
                      mean_baseline_mae_bps=44.0, interval_width_bps=300.0, abstention_pct=95.0)
        period['paper'].update(status='incomplete', selected_candidates=15, stress_trades=10,
                               stress_unknown_trades=2, mean_anchor_to_entry_pct=0.2,
                               unknown_trades=2, net_return_pct=None, max_drawdown_pct=None, stress_return_pct=None)
        period['always_buy'] = copy.deepcopy(period['paper'])
        self.save(value)
        result = service.forecast_payload('SBER', now=NOW)
        self.assertEqual(result['status'], 'historical')
        public = result['report']['periods']['test']
        self.assertEqual(public['unknown_targets'], 4)
        self.assertEqual(public['paper']['status'], 'incomplete')
        self.assertIsNone(public['paper']['net_return_pct'])
        self.assertEqual(public['always_buy']['stress_unknown_trades'], 2)
        self.assertNotIn('actual_price', result['forecast'])

    def test_incomplete_paper_cannot_publish_a_numeric_full_return(self):
        value = fixture()
        value['report']['periods']['test']['paper']['status'] = 'incomplete'
        self.save(value)
        self.assertIsNone(service.load_artifact(NOW))

    def test_incomplete_stress_return_or_empty_sample_metrics_are_not_misreported(self):
        for kind in ('stress', 'drawdown', 'empty', 'coverage'):
            value = fixture()
            period = value['report']['periods']['test']
            if kind == 'stress':
                period['paper']['stress_unknown_trades'] = 1
            elif kind == 'drawdown':
                period['paper'].update(status='incomplete', net_return_pct=None)
            elif kind == 'empty':
                period.update(samples=0, days=0)
            else:
                period.update(eligible_forecasts=70, unknown_targets=2)
            self.save(value)
            self.assertIsNone(service.load_artifact(NOW))

    def test_unknown_or_zero_trades_never_publish_returns_even_without_status(self):
        for kind in ('unknown', 'empty', 'empty_stress', 'false_ok'):
            with self.subTest(kind=kind):
                value = fixture()
                paper = value['report']['periods']['test']['paper']
                if kind == 'unknown':
                    paper['unknown_trades'] = 2
                elif kind == 'empty':
                    paper['trades'] = 0
                elif kind == 'empty_stress':
                    paper['stress_trades'] = 0
                else:
                    paper.update(status='ok', unknown_trades=2,
                                 net_return_pct=None, max_drawdown_pct=None)
                self.save(value)
                self.assertIsNone(service.load_artifact(NOW))

    def test_no_trades_metrics_remain_null_in_public_report(self):
        value = fixture()
        paper = value['report']['periods']['test']['paper']
        paper.update(trades=0, stress_trades=0, net_return_pct=None,
                     max_drawdown_pct=None, stress_return_pct=None)
        self.save(value)
        result = service.forecast_payload('SBER', now=NOW)
        self.assertEqual(result['status'], 'ok')
        for key in ('net_return_pct', 'max_drawdown_pct', 'stress_return_pct'):
            self.assertIsNone(result['report']['periods']['test']['paper'][key])

    def test_period_dates_are_projected_together_with_exclusive_end(self):
        value = fixture()
        value['report']['periods']['test'].update(start='2026-09-01', end_exclusive='2026-10-03')
        self.save(value)
        period = service.forecast_payload('SBER', now=NOW)['report']['periods']['test']
        self.assertEqual(period['start'], '2026-09-01')
        self.assertEqual(period['end_exclusive'], '2026-10-03')

    def test_missing_reversed_or_not_strict_period_dates_reject_artifact(self):
        for changes in ({'start': '2026-09-01'}, {'end_exclusive': '2026-10-03'},
                        {'start': '2026-09-1', 'end_exclusive': '2026-10-03'},
                        {'start': '2026-09-01T00:00:00', 'end_exclusive': '2026-10-03'},
                        {'start': '2026-10-03', 'end_exclusive': '2026-10-03'},
                        {'start': '2026-10-04', 'end_exclusive': '2026-10-03'},
                        {'start': '2026-02-30', 'end_exclusive': '2026-10-03'}):
            with self.subTest(changes=changes):
                value = fixture()
                value['report']['periods']['test'].update(changes)
                self.save(value)
                self.assertIsNone(service.load_artifact(NOW))

    def reference(self, artifact=None, forecasts=None, **changes):
        artifact = artifact or self.save()
        value = {'version': SPEC.VERSION, 'model_artifact_sha256': artifact['artifact_sha256'],
                 'source_sha256': SPEC.source_hashes(), 'entry_allowed': False,
                 'created_at': NOW.isoformat(),
                 'forecasts': copy.deepcopy(forecasts if forecasts is not None else artifact['forecasts'])}
        value.update(changes)
        value['artifact_sha256'] = service.digest({k: v for k, v in value.items() if k != 'artifact_sha256'})
        directory = self.root / 'data' / 'morning'
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / 'forecast-20261022-175000.json'
        path.write_text(json.dumps(value), encoding='utf-8')
        return value, path

    def test_separate_forecast_is_bound_to_the_exact_trained_artifact(self):
        artifact = fixture()
        artifact['forecasts']['SBER']['historical'] = True
        artifact = self.save(artifact)
        fresh = fixture()['forecasts']
        self.reference(artifact, fresh)
        result = service.forecast_payload('SBER', now=NOW)
        self.assertEqual(result['status'], 'ok')
        self.assertFalse(result['forecast']['historical'])
        self.assertNotIn('model_artifact_sha256', json.dumps(result))

    def test_reference_wrong_binding_source_future_date_or_permission_has_no_fallback(self):
        artifact = self.save()
        changes = ({'model_artifact_sha256': 'b' * 64}, {'source_sha256': {}},
                   {'created_at': (NOW + timedelta(minutes=1)).isoformat()},
                   {'entry_allowed': True}, {'version': 'wrong'})
        for change in changes:
            with self.subTest(change=change):
                self.reference(artifact, **change)
                result = service.forecast_payload('SBER', now=NOW)
                self.assertEqual(result['status'], 'unavailable')
                self.assertNotIn('forecast', result)
                self.assertIn('report', result)

    def test_reference_tamper_oversize_and_bad_latest_do_not_fall_back(self):
        artifact = self.save()
        reference, path = self.reference(artifact)
        reference['forecasts']['SBER']['anchor_price'] = 400
        for raw in (json.dumps(reference).encode(), b' ' * (service.MAX_ARTIFACT_BYTES + 1), b'{}'):
            path.write_bytes(raw)
            self.assertEqual(service.forecast_payload('SBER', now=NOW)['status'], 'unavailable')
        self.reference(artifact)
        (path.parent / 'forecast-99999999.json').write_text('{}', encoding='utf-8')
        self.assertEqual(service.forecast_payload('SBER', now=NOW)['status'], 'unavailable')

    def test_latest_reference_missing_selected_ticker_has_no_historical_fallback(self):
        artifact = self.save()
        self.reference(artifact, {'GAZP': copy.deepcopy(artifact['forecasts']['SBER'])})
        result = service.forecast_payload('SBER', now=NOW)
        self.assertEqual(result['status'], 'unavailable')
        self.assertNotIn('forecast', result)

    def test_reference_outside_reference_directory_is_rejected(self):
        artifact = self.save()
        _, path = self.reference(artifact)
        original_resolve = Path.resolve

        def resolve(item, *args, **kwargs):
            return self.root / 'external.json' if item == path else original_resolve(item, *args, **kwargs)

        with patch.object(Path, 'resolve', resolve):
            result = service.forecast_payload('SBER', now=NOW)
        self.assertEqual(result['status'], 'unavailable')

    def test_reference_never_accepts_a_known_future_outcome(self):
        artifact = self.save()
        forecasts = copy.deepcopy(artifact['forecasts'])
        forecasts['SBER'].update(historical=True, actual_price=302)
        self.reference(artifact, forecasts)
        self.assertEqual(service.forecast_payload('SBER', now=NOW)['status'], 'unavailable')


class MorningViewTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def test_loopback_get_and_query_normalization(self):
        with patch('dashboard.morning_views.forecast_payload', return_value={'status': 'ok', 'entry_allowed': False}) as payload:
            result = forecast(self.factory.get('/api/morning-forecast/', {'ticker': ' sber ', 'mode': 'live'}, REMOTE_ADDR='127.0.0.1'))
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result['Cache-Control'], 'no-store')
        payload.assert_called_once_with('SBER', 'live')

    def test_ipv6_loopback_allowed(self):
        with patch('dashboard.morning_views.forecast_payload', return_value={'status': 'unavailable'}) as payload:
            self.assertEqual(forecast(self.factory.get('/', REMOTE_ADDR='::1')).status_code, 200)
        payload.assert_called_once()

    def test_remote_request_cannot_spoof_loopback_via_forwarded_header(self):
        with patch('dashboard.morning_views.forecast_payload', side_effect=AssertionError('must not read files')):
            response = forecast(self.factory.get('/', REMOTE_ADDR='192.0.2.4', HTTP_X_FORWARDED_FOR='127.0.0.1'))
        self.assertEqual(response.status_code, 403)
        self.assertFalse(json.loads(response.content)['entry_allowed'])

    def test_post_is_not_supported(self):
        with patch('dashboard.morning_views.forecast_payload', side_effect=AssertionError('must not train')):
            self.assertEqual(forecast(self.factory.post('/', REMOTE_ADDR='127.0.0.1')).status_code, 405)

    def test_invalid_ticker_and_mode_never_reach_service(self):
        with patch('dashboard.morning_views.forecast_payload', side_effect=AssertionError('invalid input')):
            for query in ({'ticker': '../SBER'}, {'ticker': ''}, {'ticker': 'A' * 16},
                          {'ticker': 'SBER', 'mode': 'train'}, {'ticker': '<script>'}):
                self.assertEqual(forecast(self.factory.get('/', query, REMOTE_ADDR='127.0.0.1')).status_code, 400)
