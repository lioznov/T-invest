"""Temporal validation command contracts; synthetic cache and mocked fitting only."""
import copy
import json
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings

from . import temporal_validation as study
from .management.commands import validate_morning_splits as command
from .test_temporal_validation import row, constant_model


def fake_folds():
    report = {'samples': 1, 'mae_bps': 2., 'rmse_bps': 3., 'baseline_mae_bps': 4.,
              'paper': {'trades': 0, 'net_return_pct': None}}
    return [{**copy.deepcopy(spec), 'trained_rows': 1, 'calibration_rows': 1,
             'model': constant_model(), 'report': copy.deepcopy(report), 'stocks': {}, 'examples': [],
             'current_year_report': copy.deepcopy(report), 'current_year_stocks': {},
             'current_year_examples': []} for spec in study.FOLDS]


class TemporalProtocolTests(SimpleTestCase):
    def test_protocol_repeatable_and_modified_sources_rejected_without_overwrite(self):
        with TemporaryDirectory() as directory, patch.object(study, 'source_hashes', return_value={'code': 'first'}):
            root = Path(directory)
            first = command.freeze(root)
            path = root/'morning-temporal-protocol-v1.json'
            before = path.read_bytes()
            self.assertEqual(command.freeze(root), first)
            with patch.object(study, 'source_hashes', return_value={'code': 'changed'}):
                with self.assertRaises(CommandError):
                    command.freeze(root)
            self.assertEqual(path.read_bytes(), before)
            saved = json.loads(before)
            self.assertFalse(saved['entry_allowed'])
            self.assertEqual(saved['spec']['folds'], list(study.FOLDS))

    def test_changed_fold_definition_is_rejected_even_if_source_hash_stub_is_unchanged(self):
        with TemporaryDirectory() as directory, patch.object(study, 'source_hashes', return_value={'code': 'fixed'}):
            root = Path(directory)
            command.freeze(root)
            before = (root/'morning-temporal-protocol-v1.json').read_bytes()
            changed = copy.deepcopy(study.FOLDS)
            changed[0]['periods']['test']['start'] = '2024-06-01'
            with patch.object(study, 'FOLDS', changed), self.assertRaises(CommandError):
                command.freeze(root)
            self.assertEqual((root/'morning-temporal-protocol-v1.json').read_bytes(), before)


class TemporalCommandTests(SimpleTestCase):
    def test_freezes_before_cache_read_uses_only15m_control_and_publishes_all_three_folds(self):
        events, captured = [], []
        original_freeze = command.freeze

        def freeze(root):
            events.append('freeze')
            return original_freeze(root)

        def history(root, ticker, interval):
            events.append(f'cache:{ticker}:{interval}')
            self.assertEqual(interval, '15m')
            return {'instrument': {'ticker': ticker, 'uid': 'synthetic-'+ticker, 'lot': 1},
                    'candles': [], 'coverage': {'bars': 0},
                    'sources': [{'file': ticker+'/synthetic.json', 'sha256': 'cache-fixture'}]}

        def examples(candles, ticker, lot):
            return {'control': [row(ticker=ticker)], 'main_hour': [row('2025-06-02', ticker)],
                    'evening_hour': [row('2024-06-03', ticker)]}, {'control': {'examples': 1}}

        def run(rows, progress=None):
            captured.extend(rows)
            return fake_folds()

        with TemporaryDirectory() as directory, override_settings(BASE_DIR=Path(directory)), \
                patch.object(command, 'freeze', side_effect=freeze), \
                patch.object(command, 'load_history', side_effect=history) as load, \
                patch.object(command.multi, 'build_intraday', side_effect=examples), \
                patch.object(study, 'run_folds', side_effect=run) as runs, \
                patch.object(study, 'source_hashes', return_value={'code': 'fixed'}), \
                patch.object(command.m, 'TICKERS', ('SBER', 'GAZP')), \
                patch('dashboard.market_provider.open_client') as broker:
            command.Command(stdout=StringIO()).handle()
            self.assertEqual(events, ['freeze', 'cache:SBER:15m', 'cache:GAZP:15m'])
            self.assertEqual(load.call_count, 2)
            runs.assert_called_once()
            self.assertEqual([item['ticker'] for item in captured], ['SBER', 'GAZP'])
            self.assertTrue(all(item['at'].startswith('2026-06-01') for item in captured))
            broker.assert_not_called()
            paths = list((Path(directory)/'dashboard'/'ai_models').glob('morning-temporal-v1-*.json'))
            self.assertEqual(len(paths), 1)
            artifact = json.loads(paths[0].read_text(encoding='utf-8'))
            self.assertFalse(artifact['entry_allowed'])
            self.assertEqual([fold['id'] for fold in artifact['folds']], [spec['id'] for spec in study.FOLDS])
            self.assertEqual(set(artifact['sources']), {'SBER', 'GAZP'})
            expected = artifact.pop('artifact_sha256')
            self.assertEqual(command.m.digest(artifact), expected)

    def test_cache_failure_keeps_registered_protocol_but_does_not_publish_or_fit(self):
        with TemporaryDirectory() as directory, override_settings(BASE_DIR=Path(directory)), \
                patch.object(study, 'source_hashes', return_value={'code': 'fixed'}), \
                patch.object(command, 'load_history', side_effect=ValueError('synthetic cache failure')), \
                patch.object(study, 'run_folds') as runs, \
                patch('dashboard.market_provider.open_client') as broker:
            with self.assertRaises(ValueError):
                command.Command(stdout=StringIO()).handle()
            self.assertTrue((Path(directory)/'data'/'morning-temporal-protocol-v1.json').exists())
            self.assertEqual(list(Path(directory).rglob('morning-temporal-v1-*.json')), [])
            runs.assert_not_called()
            broker.assert_not_called()
