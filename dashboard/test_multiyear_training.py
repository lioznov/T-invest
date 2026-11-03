"""Protocol and orchestration checks: tiny synthetic rows, no broker/model fit."""
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from django.core.management.base import CommandError
from django.test import SimpleTestCase,override_settings
from . import multiyear_model as study
from .management.commands import train_multiyear_morning as command


def example(year):
    return {'at':f'{year}-06-03T17:45:00+03:00','label_end':f'{year}-06-04T10:15:00+03:00',
            'ticker':'SBER','x':[0.]*18,'y':.1}


class MultiyearTrainingTests(SimpleTestCase):
    def test_fixed_protocol_cannot_be_overwritten_after_source_change(self):
        with TemporaryDirectory() as temp,patch.object(study,'source_hashes',return_value={'a':'b'}):
            directory=Path(temp)
            digest=command.freeze(directory)
            path=directory/'multiyear-morning-protocol-v1.json'
            original=path.read_bytes()
            self.assertEqual(command.freeze(directory),digest)
            with patch.object(study,'source_hashes',return_value={'a':'changed'}):
                with self.assertRaises(CommandError):command.freeze(directory)
            self.assertEqual(path.read_bytes(),original)

    def test_fit_only_receives_old_training_and_2024_calibration(self):
        rows=[example(year) for year in (2023,2024,2025,2026)]
        with patch.object(command.m,'fit',return_value={'fixed':True}) as fit:
            seen=[]
            model,report=command.make_report(rows,lambda values,model:seen.append(values) or {'samples':len(values)})
        self.assertEqual(fit.call_args.args,([rows[0]],[rows[1]]))
        self.assertEqual(seen,[[rows[2]],[rows[3]]])
        self.assertEqual(report['periods']['test']['start'],'2025-01-01')
        self.assertEqual(report['periods']['later']['end_exclusive'],'2026-10-23')

    def test_all_four_results_are_saved_without_network_or_model_promotion(self):
        coverage={'bars':1,'first_actual':'2023-06-03T00:00:00+00:00',
                  'last_actual':'2023-06-03T00:00:00+00:00','requested_from':'2000-01-01T00:00:00+00:00',
                  'to':'2026-10-23T00:00:00+00:00'}
        data={'instrument':{'uid':'synthetic','ticker':'SBER','lot':1},'candles':[],
              'coverage':coverage,'sources':[{'sha256':'fake'}]}
        rows=[example(year) for year in (2023,2024,2025,2026)]
        fake_report={'periods':{key:{'samples':1,'mae_bps':1,'baseline_mae_bps':1,
            'direction_accuracy_pct':50,'paper':{'trades':0,'net_return_pct':None}}
            for key in ('test','later')}}
        with TemporaryDirectory() as temp,override_settings(BASE_DIR=Path(temp)), \
                patch.object(command.m,'TICKERS',('SBER',)), \
                patch.object(study,'source_hashes',return_value={'code':'fixed'}), \
                patch.object(command,'load_history',return_value=data) as loader, \
                patch.object(study,'build_intraday',return_value=({v['id']:rows for v in study.VARIANTS},{})), \
                patch.object(command.daily,'build_examples',return_value=(rows,{})), \
                patch.object(command,'make_report',return_value=({'weights':[1]},fake_report)), \
                patch('dashboard.market_provider.open_client',side_effect=AssertionError('No network')):
            def check_protocol(*args):
                self.assertTrue((Path(temp)/'data'/'multiyear-morning-protocol-v1.json').exists())
                return data
            loader.side_effect=check_protocol
            command.Command(stdout=io.StringIO()).handle()
            path=next((Path(temp)/'dashboard'/'ai_models').glob('multiyear-morning-v1-*.json'))
            artifact=json.loads(path.read_text(encoding='utf-8'))
            self.assertFalse(artifact['entry_allowed'])
            self.assertEqual([v['id'] for v in artifact['variants']],['daily_full','control','main_hour','evening_hour'])
            self.assertEqual(artifact['artifact_sha256'],study.digest({k:v for k,v in artifact.items() if k!='artifact_sha256'}))
            self.assertEqual(loader.call_count,2)
