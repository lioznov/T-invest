import json
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from django.test import SimpleTestCase, override_settings

from .market_provider import demo_snapshot
from .signal_service import archive
from .signals import analyse


class ResearchArchiveTests(SimpleTestCase):
    def test_frozen_plan_version_and_quotes_are_saved_atomically_in_compact_archive(self):
        snapshot = demo_snapshot('SBER')
        analysis = analyse(snapshot)
        analysis['plan'] = {'entry': 300, 'stop': 297, 'target': 306, 'risk_per_share': 3}
        with tempfile.TemporaryDirectory() as directory, override_settings(BASE_DIR=Path(directory)):
            archive('SBER', snapshot, analysis)
            archive('SBER', snapshot, analysis)
            with closing(sqlite3.connect(Path(directory) / 'data' / 'market.sqlite3')) as db:
                compact_rows = db.execute('SELECT payload FROM paper_observations').fetchall()
                self.assertEqual(len(compact_rows), 1)
                payload = json.loads(compact_rows[0][0])
                self.assertEqual(payload['strategy_version'], 'six_rules_v1')
                self.assertEqual(payload['plan']['stop'], 297)
                self.assertEqual(payload['instrument']['lot'], 10)
                self.assertEqual(len(payload['book']['bids']), 1)
                self.assertIn('time', payload['book'])
                self.assertNotIn('trades', payload)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM observations').fetchone()[0], 1)
