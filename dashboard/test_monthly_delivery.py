import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from django.test import SimpleTestCase, Client, override_settings
from .monthly_momentum import VERSION, RULES, VARIANTS, TICKERS, PERIODS, SCENARIOS
from .monthly_research import load_monthly_research
from .management.commands import audit_monthly_calendar


class MonthlyDeliveryTests(SimpleTestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root/'data'
        self.data.mkdir()
        self.override = override_settings(BASE_DIR=self.root)
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.hashes = patch('dashboard.monthly_research.hashes', return_value={'source':'test'})
        self.hashes.start()
        self.addCleanup(self.hashes.stop)

    def write(self, name, value):
        raw = json.dumps(value).encode()
        (self.data/name).write_bytes(raw)
        return hashlib.sha256(raw).hexdigest()

    def fixture(self):
        inputs = {}
        for ticker in TICKERS:
            name = f'range-input-{ticker}-20261003.json'
            inputs[ticker] = {'file':name, 'sha256':self.write(name,{})}
        signature = self.write('monthly-momentum-protocol-v1.json', {'spec':{'rules':RULES,'source_sha256':{'source':'test'},'tickers':list(TICKERS)}})
        metrics = {'trades':1,'return_pct':1.,'max_drawdown_pct':1.,'unknown_trades':0,'verified':True}
        periods = {p:{s:metrics.copy() for s in SCENARIOS} for p in PERIODS}
        results = {r.key:periods for r in VARIANTS}
        original_sha = self.write('monthly-momentum-study-fixture.json', {'version':VERSION,'source_sha256':{'source':'test'},
            'protocol_sha256':signature,'inputs':inputs,'results':results,'entry_allowed':False,'simulations':259,'rolling_summary':{}})
        source_hash = hashlib.sha256(Path(audit_monthly_calendar.__file__).read_bytes()).hexdigest()
        audit_protocol = self.write('monthly-calendar-review-protocol-v1.json', {'spec':{'report_sha256':original_sha,
            'report_file':'monthly-momentum-study-fixture.json','source_sha256':source_hash,'closed_days':list(audit_monthly_calendar.CLOSED)}})
        audit = {'version':'monthly_calendar_review_v1','source_sha256':source_hash,'protocol_sha256':audit_protocol,
                 'original_report':'monthly-momentum-study-fixture.json','original_sha256':original_sha,
                 'summaries':[{'key':r.key,'periods':periods} for r in VARIANTS], 'entry_allowed':False,
                 'at':'2026-10-07T14:00:00+00:00','bootstrap':{}}
        self.write('monthly-calendar-review-20261007-fixture.json', audit)
        return audit

    def test_missing_report_and_demo_never_fabricate_returns(self):
        self.assertEqual(load_monthly_research()['status'],'insufficient')
        self.assertNotIn('monthly_momentum',Client().get('/api/pattern-research/?ticker=SBER&mode=demo').json())

    def test_positive_report_does_not_approve_trading(self):
        self.fixture()
        result = load_monthly_research()
        self.assertEqual(result['status'],'ok')
        self.assertEqual(len(result['variants']),12)
        self.assertFalse(result['entry_allowed'])
        self.assertEqual(result['passed'],0)  # one trade is insufficient

    def test_modified_source_is_rejected(self):
        self.fixture()
        self.write('range-input-SBER-20261003.json',{'changed':True})
        self.assertEqual(load_monthly_research()['status'],'insufficient')

    def test_calendar_review_cannot_change_profit(self):
        audit = self.fixture()
        audit['summaries'][0]['periods']['full']['base']['return_pct'] = 100
        self.write('monthly-calendar-review-20261007-fixture.json',audit)
        self.assertEqual(load_monthly_research()['status'],'insufficient')

    def test_path_escape_is_rejected(self):
        audit = self.fixture()
        audit['original_report']='../outside.json'
        self.write('monthly-calendar-review-20261007-fixture.json',audit)
        self.assertEqual(load_monthly_research()['status'],'insufficient')
