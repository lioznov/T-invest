from dataclasses import replace
from datetime import datetime,timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from django.test import SimpleTestCase
from django.core.management.base import CommandError
from .signals import Candle,MSK
from . import market_structure as structure
from .management.commands.study_structure import freeze, replay


def bars(values=None):
    values=values or [100,102,106,102,100,102,104,102,99,101,103,101]
    start=datetime(2026,10,13,10,tzinfo=MSK)
    return [Candle(start+timedelta(minutes=15*i),v,v+1,v-1,v,1000) for i,v in enumerate(values)]


class StructureTests(SimpleTestCase):
    def test_pivot_only_exists_after_two_right_bars(self):
        candles=bars()
        self.assertEqual(structure.scan(candles[:4])['pivots'],[])
        found=structure.scan(candles[:5])['pivots']
        self.assertEqual(len(found),1)
        self.assertEqual(found[0]['kind'],'high')
        self.assertEqual(found[0]['price'],107)
        self.assertEqual(found[0]['time'],candles[2].time.isoformat())
        self.assertEqual(found[0]['confirmed_at'],(candles[4].time+timedelta(minutes=15)).isoformat())
        self.assertIsNone(structure.scan(candles[:5])['rows'][4]['resistance'])

    def test_prefix_rows_pivots_and_events_do_not_repaint(self):
        candles=bars()
        earlier=structure.scan(candles[:9])
        extended=structure.scan(candles)
        self.assertEqual(earlier['rows'],extended['rows'][:9])
        self.assertEqual(earlier['pivots'],[p for p in extended['pivots'] if p['confirmed_index']<9])
        self.assertEqual(earlier['events'],[e for e in extended['events'] if e['time']<=candles[8].time.isoformat()])
        candles[-1]=replace(candles[-1],open=1000,high=1001,low=999,close=1000)
        self.assertEqual(structure.scan(candles)['rows'][:9],earlier['rows'])

    def test_strict_pivots_reject_plateau_gaps_zero_volume_and_overnight(self):
        candles=bars([100,104,104,104,100])
        self.assertEqual(structure.scan(candles)['pivots'],[])
        for change in ('gap','zero','overnight'):
            candles=bars()[:5]
            if change=='zero':
                candles[3]=replace(candles[3],volume=0)
            elif change=='gap':
                candles[4]=replace(candles[4],time=candles[4].time+timedelta(minutes=15))
            else:
                candles=[replace(c,time=c.time+timedelta(hours=13)) for c in candles]
            self.assertEqual(structure.scan(candles)['pivots'],[])

    def test_breakout_crosses_previously_known_level_not_its_own_wick(self):
        candles=bars([100,102,106,102,100,102,108,109])
        result=structure.scan(candles)
        self.assertTrue(result['rows'][6]['up'])
        self.assertFalse(result['rows'][7]['up'])
        event=next(e for e in result['events'] if e['key']=='breakout')
        self.assertEqual(event['level'],107)
        self.assertLess(event['level_known_at'],event['detected_at'])
        self.assertFalse(event['entry_allowed'])

    def test_bounce_and_breakdown_are_different_events(self):
        candles=bars([104,102,99,102,104,103,104,104,97])
        candles[6]=replace(candles[6],open=103,low=98.1,close=104,high=105)
        result=structure.scan(candles)
        self.assertTrue(result['rows'][6]['bounce'])
        self.assertTrue(result['rows'][8]['down'])
        self.assertFalse(result['rows'][8]['bounce'])

    def test_panel_rejects_unfinished_and_stale_current_events(self):
        candles=bars([100+(i%5) for i in range(30)])
        now=candles[-2].time+timedelta(minutes=16)
        a=structure.chart_panel({'candles':candles},now=now)
        b=structure.chart_panel({'candles':candles[:-1]},now=now)
        self.assertEqual(a,b)
        self.assertFalse(a['entry_allowed'])
        self.assertFalse(a['source_confirmed'])
        stale=structure.chart_panel({'candles':candles},now=now+timedelta(days=1))
        self.assertEqual(stale['status'],'stale')
        self.assertIsNone(stale['latest_event'])
        self.assertTrue(stale['pivots'])
        self.assertEqual(structure.chart_panel({'candles':candles[::-1]},now=now)['status'],'invalid')

    def test_lines_start_at_confirmation_not_original_extreme(self):
        candles=bars([100,104,101,99,100,103,100,98,99,102,100,97]+[101]*15)
        result=structure.chart_panel({'candles':candles},now=candles[-1].time+timedelta(minutes=16))
        self.assertTrue(result['trend_lines'])
        for line in result['trend_lines']:
            anchor=line['anchors'][-1]
            self.assertGreater(line['start_time'],anchor['time'])
            self.assertEqual(line['known_at'],anchor['confirmed_at'])
        self.assertTrue(result['levels'])

    def test_unknown_replay_is_retained_and_no_signals_are_not_profit(self):
        rows=[{'ticker':'SBER','at':'2026-10-13T07:30:00+00:00','y':None,'outcome':None,
               'structure_flags':{'pivot_breakout':True,'volume_breakout':False,'support_bounce':False}}]
        result=replay(rows,'pivot_breakout')
        self.assertEqual(result['base']['trades'],0)
        self.assertEqual(result['base']['unknown_trades'],1)

    def test_protocol_is_frozen_before_prices(self):
        with TemporaryDirectory() as d,patch('dashboard.management.commands.study_structure.source_hashes',return_value={'code':'a'}):
            freeze(Path(d))
            with patch('dashboard.management.commands.study_structure.source_hashes',return_value={'code':'changed'}):
                with self.assertRaises(CommandError):
                    freeze(Path(d))
