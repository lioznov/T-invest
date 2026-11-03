from unittest.mock import patch
from django.test import SimpleTestCase


class ResearchHelperDeliveryTests(SimpleTestCase):
    def test_optional_report_failure_does_not_break_explanation(self):
        import sqlite3
        from .signal_views import _helper_evidence
        _helper_evidence.cache_clear()
        with patch('dashboard.neural_service.model_status',side_effect=OSError('private details')), \
             patch('dashboard.signal_views.load_paper_journal',side_effect=sqlite3.OperationalError('private db')), \
             patch('dashboard.signal_views.load_review',return_value={'status':'unavailable'}):
            self.assertEqual(_helper_evidence('SBER',0),({'status':'unavailable'},)*3)
        _helper_evidence.cache_clear()

    @patch('dashboard.signal_views._helper_evidence')
    @patch('dashboard.signal_views.get_signal',return_value={'status':'loading','mode':'demo'})
    def test_demo_gets_explanation_without_broker_or_evidence_requests(self,get_signal,evidence):
        result=self.client.get('/api/signals/?ticker=SBER&mode=demo')
        helper=result.json()['research_helper']
        self.assertEqual(helper['data_review']['state'],'demo')
        self.assertFalse(helper['entry_allowed']);self.assertIsNone(helper['confidence'])
        evidence.assert_not_called()
        self.assertEqual(result['Cache-Control'],'no-store')

    @patch('dashboard.signal_views._helper_evidence')
    @patch('dashboard.signal_views.get_signal',return_value={'status':'error','mode':'live','message':'private broker details'})
    def test_error_explanation_never_copies_raw_broker_text(self,get_signal,evidence):
        helper=self.client.get('/api/signals/?ticker=SBER&mode=live').json()['research_helper']
        self.assertEqual(helper['action'],'WAIT')
        self.assertNotIn('private broker details',str(helper))
        evidence.assert_not_called()
