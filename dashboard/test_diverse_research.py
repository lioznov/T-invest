from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from django.core.management.base import CommandError
from django.test import override_settings

from dashboard import diverse_research as d, portfolio_replay
from dashboard.signals import Candle, MSK
from dashboard.management.commands import study_diverse_long as command


def bars(days=12):
    result = []
    day = datetime(2026, 7, 1, 10, tzinfo=MSK)
    while days:
        if day.weekday() < 5:
            for j in range(34):
                p = 100+len(result)*.005
                result.append(Candle(day+timedelta(minutes=15*j), p, p+.12, p-.08, p+.03, 100+j))
            days -= 1
        day += timedelta(days=1)
    return result


def fake_result(value=1., trades=10, unknown=0):
    return {'trades': trades, 'unknown_trades': unknown, 'return_pct': None if unknown else value}


class DiverseResearchTests(TestCase):
    def test_registry_is_bounded_long_only(self):
        self.assertEqual(len(d.VARIANTS), 24)
        self.assertEqual({v['hold'] for v in d.VARIANTS.values()}, {4,8,16})
        self.assertFalse(d.RULES['entry_allowed'])

    def test_features_are_prefix_causal(self):
        cs = bars()
        prefix = d.feature_rows(cs[:320])
        self.assertEqual(prefix, d.feature_rows(cs)[:320])

    def test_future_mutation_does_not_change_past(self):
        cs = bars()
        changed = cs[:320]+[Candle(c.time,c.open*2,c.high*2,c.low*2,c.close*2,c.volume*3) for c in cs[320:]]
        self.assertEqual(d.feature_rows(cs)[:320], d.feature_rows(changed)[:320])

    def test_seasonal_volume_excludes_current_and_future(self):
        cs = bars(7)
        i = 34*5+1
        c = cs[i]
        cs[i] = Candle(c.time,c.open,c.high,c.low,c.close,1010)
        fs = d.feature_rows(cs)
        self.assertIsNone(fs[34*4+1]['seasonal_volume'])
        self.assertEqual(fs[i]['seasonal_volume'],10)

    def test_missing_open_cannot_fabricate_gap(self):
        cs = [c for c in bars() if c.time.astimezone(MSK).hour != 10 or c.time.minute != 0]
        self.assertFalse(any(f['flags']['gap_repair'] for f in d.feature_rows(cs)))

    def test_invalid_prices_rejected(self):
        c = bars(1)[0]
        with self.assertRaises(ValueError):
            d.feature_rows([Candle(c.time,100,99,98,100,1)])

    def test_peer_excludes_self_and_needs_four_others(self):
        cs = bars()
        universe = {}
        for n in range(5):
            fs = d.feature_rows(cs)
            for f in fs:
                f['return4'] = n*.01
            universe[str(n)] = (cs,fs)
        d.add_peer_context(universe)
        self.assertEqual(universe['0'][1][110]['peer_return'],.025)
        universe.pop('4')
        for _,fs in universe.values():
            for f in fs:
                f['peer_return'] = None
        d.add_peer_context(universe)
        self.assertIsNone(universe['0'][1][110]['peer_return'])

    def test_peer_matches_exact_time_not_last_seen(self):
        cs = bars()
        u = {str(n):(cs, d.feature_rows(cs)) for n in range(5)}
        target = 34*8+10
        other_cs, other_fs = u['4']
        u['4'] = (other_cs[:target]+other_cs[target+1:], other_fs[:target]+other_fs[target+1:])
        d.add_peer_context(u)
        self.assertIsNone(u['0'][1][target]['peer_return'])
        self.assertIsNotNone(u['0'][1][target+1]['peer_return'])

    def selected_rows(self, hold=4, missing=False):
        cs = bars()
        fs = d.feature_rows(cs)
        i = 34*8+5
        fs[i]['flags']['compression'] = True
        if missing:
            cs = cs[:i+2]+cs[i+3:]
            fs = fs[:i+2]+fs[i+3:]
        return list(d.replay_rows(cs,fs,'SBER',10,f'compression_{hold}')), cs, i

    def test_horizons_next_open_and_full_interval(self):
        for hold in (4,8,16):
            rows,cs,i = self.selected_rows(hold)
            self.assertEqual(len(rows),1)
            row = rows[0]
            self.assertEqual(row['entry'],cs[i+1].open)
            self.assertEqual(datetime.fromisoformat(row['at']),cs[i+1].time)
            self.assertEqual(datetime.fromisoformat(row['label_end'])-datetime.fromisoformat(row['at']),timedelta(minutes=hold*15))
            self.assertIsNotNone(row['outcome'])

    def test_unknown_path_not_silently_removed(self):
        rows,_,_ = self.selected_rows(missing=True)
        self.assertIsNone(rows[0]['outcome'])
        result = portfolio_replay.simulate(rows)
        self.assertEqual(result['unknown_trades'],1)
        self.assertIsNone(result['return_pct'])

    def test_cost_stress_reduces_result(self):
        rows,_,_ = self.selected_rows()
        values = [portfolio_replay.simulate(rows,m)['return_pct'] for m in (0,1,2)]
        self.assertGreater(values[0],values[1])
        self.assertGreater(values[1],values[2])

    def test_entry_window_prevents_overnight_horizons(self):
        cs = bars()
        fs = d.feature_rows(cs)
        for f in fs:
            f['flags']['compression'] = True
        for r in d.replay_rows(cs,fs,'SBER',10,'compression_16'):
            start = datetime.fromisoformat(r['at']).astimezone(MSK)
            end = datetime.fromisoformat(r['label_end']).astimezone(MSK)
            self.assertEqual(start.date(),end.date())
            self.assertLessEqual(end.hour*60+end.minute,1110)

    def test_zero_volume_future_is_unknown(self):
        rows,cs,i = self.selected_rows()
        fs = d.feature_rows(cs)
        fs[i]['flags']['compression'] = True
        c = cs[i+2]
        cs[i+2] = Candle(c.time,c.open,c.high,c.low,c.close,0)
        self.assertIsNone(list(d.replay_rows(cs,fs,'SBER',10,'compression_4'))[0]['outcome'])

    def test_gap_stop_uses_open_not_better_stop_price(self):
        _,cs,i = self.selected_rows()
        fs = d.feature_rows(cs)
        fs[i]['flags']['compression'] = True
        c = cs[i+2]
        cs[i+2] = Candle(c.time,98,99,97,98,100)
        row = list(d.replay_rows(cs,fs,'SBER',10,'compression_4'))[0]
        self.assertEqual(row['outcome']['exit'],98)
        self.assertEqual(row['outcome']['reason'],'gap_stop')

    def test_missing_next_entry_is_retained_unknown(self):
        _,cs,i = self.selected_rows()
        fs = d.feature_rows(cs)
        fs[i]['flags']['compression'] = True
        cs,fs = cs[:i+1]+cs[i+2:],fs[:i+1]+fs[i+2:]
        row = list(d.replay_rows(cs,fs,'SBER',10,'compression_4'))[0]
        self.assertIsNone(row['entry'])
        self.assertIsNone(row['outcome'])

    def test_period_purges_full_label_horizon(self):
        r = {'at':'2026-09-30T20:00:00+00:00','label_end':'2026-09-30T22:00:00+00:00'}
        self.assertFalse(d.in_period(r,('2026-09-01','2026-10-01')))

    def test_selection_cannot_approve_zero_trades(self):
        results = {'a':{p:{k:fake_result(1,0) for k in ('base','double_cost')} for p in d.PERIODS}}
        self.assertIsNone(d.choose(results)['audit_candidate'])

    def test_screen_requires_all_folds_under_stress(self):
        results = {'a':{p:{k:fake_result() for k in ('base','double_cost')} for p in d.PERIODS}}
        self.assertEqual(d.choose(results)['passed'],['a'])
        results['a']['summer']['double_cost']['return_pct'] = -.1
        selection = d.choose(results)
        self.assertEqual(selection['passed'],[])
        self.assertEqual(selection['audit_candidate'],'a')
        self.assertFalse(selection['entry_allowed'])

    def test_unknown_result_disqualifies_candidate(self):
        results = {'a':{p:{k:fake_result(100,100,1) for k in ('base','double_cost')} for p in d.PERIODS}}
        self.assertIsNone(d.choose(results)['audit_candidate'])

    def test_worst_fold_ranks_before_total_profit(self):
        results = {name:{p:{k:fake_result(value) for k in ('base','double_cost')} for p in d.PERIODS}
                   for name,value in (('a',1),('b',2))}
        results['b']['summer']['double_cost']['return_pct'] = .1
        self.assertEqual(d.choose(results)['audit_candidate'],'a')

    def test_lock_refuses_overwrite(self):
        with TemporaryDirectory() as temp:
            path = Path(temp)/'lock.json'
            sha = command.lock(path,{'x':1})
            self.assertEqual(command.lock(path,{'x':1}),sha)
            with self.assertRaises(CommandError):
                command.lock(path,{'x':2})
            self.assertEqual(json.loads(path.read_text())['spec'],{'x':1})

    def test_selection_written_before_any_fresh_access(self):
        old = {'candles':[], 'lot':1, 'saved':{'instrument':{}}, 'source':{},
               'quality':{'exchange_source_verified':True,'exchange_requested':True}}
        with TemporaryDirectory() as temp, override_settings(BASE_DIR=temp):
            def fresh(*args):
                self.assertTrue((Path(temp)/'data'/'diverse-long-selection-v1.json').exists())
                raise CommandError('intentional stop before network')
            with patch.object(command,'acquire',return_value=deepcopy(old)), \
                 patch.object(command,'extension',return_value=([],{})), \
                 patch.object(command,'load_extension',return_value=([],{})), \
                 patch.object(command,'universe',return_value={}), \
                 patch.object(command,'evaluate',return_value={'base':fake_result()}), \
                 patch.object(command.research,'choose',return_value={'audit_candidate':'compression_4','passed':[]}), \
                 patch.object(command,'fresh_input',side_effect=fresh):
                with self.assertRaisesMessage(CommandError,'intentional stop'):
                    command.Command().handle(fetch=True,discovery_only=False)

    def assertRaisesMessage(self, exception, text):
        return self.assertRaisesRegex(exception,text)
