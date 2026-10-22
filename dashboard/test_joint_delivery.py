import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from django.test import Client, override_settings
from django.core.management.base import CommandError
from .joint_study import VERSION, VARIANTS, RULES, summarize, matched
from .joint_research import load_joint_research
from .management.commands.study_joint import freeze, hashes, snapshot
from .test_stream_analysis import NOW


class JointDeliveryTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root/"data"
        self.data.mkdir()
        self.settings = override_settings(BASE_DIR=self.root)
        self.settings.enable()
        self.addCleanup(self.settings.disable)

    def save(self, positive=False):
        path, protocol = freeze(self.data, "fixture")
        input_path = self.data/"joint-stream-input-v1-replay.json"
        input_path.write_text('{}', encoding='utf-8')
        quote = {"ticker": "SBER", "signal_at": NOW.isoformat(), "status": "verified_quote_replay",
                 "return_pct": 1., "stress_return_pct": .8, "gross_quote_return_pct": 1.2}
        outcomes = {k: [quote] if positive else [] for k in VARIANTS}
        candidates = [{"ticker": "SBER", "signal_at": NOW.isoformat(), "filters": {k: True for k in VARIANTS}, "outcome": quote}] if positive else []
        report = {"version": VERSION, "phase": "replay", "entry_allowed": False, "session": "fixture",
                  "protocol_sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "source_sha256": hashes(),
                  "input_file": input_path.name, "input_sha256": hashlib.sha256(input_path.read_bytes()).hexdigest(),
                  "outcomes": outcomes, "candidates": candidates, "at": NOW.isoformat(),
                  "tickers": protocol["tickers"], "coverage": {},
                  "summaries": {k: summarize(v) for k, v in outcomes.items()},
                  "matched": {k: matched(candidates, k) for k in VARIANTS[1:]}}
        report_path = self.data/"joint-stream-study-replay-fixture.json"
        report_path.write_text(json.dumps(report), encoding='utf-8')
        return report_path, report

    def test_no_file_not_zero_profit(self):
        self.assertEqual(load_joint_research("SBER")["status"], "insufficient")

    def test_empty_results_and_unknown_stock_are_not_zero_profit(self):
        self.save()
        result = load_joint_research("SBER")
        self.assertEqual(result["status"], "ok")
        self.assertIsNone(result["variants"][0]["stock"]["mean_trade_return_pct"])
        self.assertEqual(load_joint_research("TATN")["variants"], [])

    def test_positive_report_still_never_allows_long(self):
        self.save(positive=True)
        result = load_joint_research("SBER")
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["entry_allowed"])
        self.assertFalse(result["variants"][0]["group"]["sufficient_sample"])

    def test_input_tamper_is_rejected(self):
        self.save()
        (self.data/"joint-stream-input-v1-replay.json").write_text('{"tampered":true}', encoding='utf-8')
        self.assertEqual(load_joint_research("SBER")["status"], "insufficient")

    def test_boolean_return_and_fabricated_summary_are_rejected(self):
        path, saved = self.save(positive=True)
        saved["outcomes"]["chart"][0]["return_pct"] = True
        path.write_text(json.dumps(saved), encoding='utf-8')
        self.assertEqual(load_joint_research("SBER")["status"], "insufficient")

    def test_source_or_frozen_rules_cannot_be_changed(self):
        path, protocol = freeze(self.data, "fixture")
        protocol = json.loads(json.dumps(protocol))
        protocol["rules"]["hold_seconds"] = 60
        path.write_text(json.dumps(protocol), encoding='utf-8')
        with self.assertRaises(CommandError):
            freeze(self.data, "fixture")

    def test_real_registered_sources_stay_frozen(self):
        directory = Path(__file__).resolve().parents[1]/"data"
        protocol_path = directory/"joint-stream-protocol-v1.json"
        if protocol_path.exists():
            protocol = json.loads(protocol_path.read_text(encoding='utf-8'))
            self.assertEqual(protocol["source_sha256"], hashes())
            self.assertEqual(protocol["rules"], RULES)

    def test_future_empty_snapshot_does_not_fabricate_prices(self):
        session = self.data/"watch"/"fixture"
        session.mkdir(parents=True)
        _, protocol = freeze(self.data, "fixture")
        self.assertFalse(snapshot(session.resolve(), protocol, "forward")["segments"])

    def test_demo_does_not_include_market_joint_report(self):
        response = Client().get('/api/pattern-research/?ticker=SBER&mode=demo')
        self.assertNotIn("joint_factors", response.json())

    def test_live_delivers_joint_section_without_broker_calls(self):
        self.save()
        with patch('dashboard.pattern_research.load_pattern_research', return_value={'status':'insufficient'}):
            response = Client().get('/api/pattern-research/?ticker=SBER&mode=live')
        self.assertEqual(response.json()["joint_factors"]["status"], "ok")
        self.assertFalse(response.json()["joint_factors"]["entry_allowed"])

    def save_forward(self):
        _, report = self.save()
        protocol_path = self.data/"joint-stream-protocol-v1.json"
        protocol = json.loads(protocol_path.read_text(encoding='utf-8'))
        inputs = {'phase':'forward', 'version':VERSION, 'protocol_sha256':report['protocol_sha256'],
                  'window_start':protocol['forward_start'], 'window_end':protocol['forward_end'], 'segments':[]}
        input_path = self.data/'joint-forward-fixture.json'
        input_path.write_text(json.dumps(inputs), encoding='utf-8')
        report.update(phase='forward', input_file=input_path.name, forward_complete=False,
                      input_sha256=hashlib.sha256(input_path.read_bytes()).hexdigest(),
                      at=(datetime.fromisoformat(protocol['forward_start'])+timedelta(hours=1)).isoformat())
        path = self.data/'joint-stream-study-forward-fixture.json'
        path.write_text(json.dumps(report), encoding='utf-8')
        return path, report, input_path, inputs

    def test_future_table_separate_and_no_trade_not_zero(self):
        self.save_forward()
        result = load_joint_research('SBER')
        self.assertEqual(result['new_observations']['status'], 'ok')
        self.assertFalse(result['new_observations']['window_complete'])
        self.assertIsNone(result['new_observations']['variants'][0]['group']['mean_trade_return_pct'])
        self.assertFalse(result['new_observations']['entry_allowed'])

    def test_replay_cannot_be_relabelled_as_future(self):
        path, report, input_path, inputs = self.save_forward()
        inputs['phase'] = 'replay'
        input_path.write_text(json.dumps(inputs), encoding='utf-8')
        report['input_sha256'] = hashlib.sha256(input_path.read_bytes()).hexdigest()
        path.write_text(json.dumps(report), encoding='utf-8')
        self.assertEqual(load_joint_research('SBER','forward')['status'], 'insufficient')

    def test_past_records_cannot_be_used_for_future_report(self):
        path, report, input_path, inputs = self.save_forward()
        inputs['segments'] = [{'rows':[{'received_at':NOW.isoformat()}], 'extras':[]}]
        input_path.write_text(json.dumps(inputs), encoding='utf-8')
        report['input_sha256'] = hashlib.sha256(input_path.read_bytes()).hexdigest()
        path.write_text(json.dumps(report), encoding='utf-8')
        self.assertEqual(load_joint_research('SBER','forward')['status'], 'insufficient')

    def test_premature_completed_window_label_rejected(self):
        path, report, _, _ = self.save_forward()
        report['forward_complete'] = True
        path.write_text(json.dumps(report), encoding='utf-8')
        self.assertEqual(load_joint_research('SBER','forward')['status'], 'insufficient')
