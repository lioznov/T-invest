"""Meaningful guard checks for the read-only research explanation layer."""
import copy
import json
import unittest
from datetime import datetime, timedelta, timezone

from .trading_research_helper import summarize_research, RULE_NAMES

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)


def signal():
    return {'status': 'ok', 'mode': 'live', 'source': 'Т-Банк · TQBR',
        'analysis': {'updated_at': NOW.isoformat(), 'data_time': (NOW-timedelta(seconds=2)).isoformat(),
            'decision': 'LONG', 'score': 100, 'blockers': [],
            'data_quality': {'action_blocked': False, 'book_consistency_confirmed': True},
            'rules': [{'key': key, 'pass': True} for key in RULE_NAMES],
            'plan': {'entry': 100., 'stop': 99., 'target': 102.},
            'neural': {'status': 'ok', 'score': 99., 'expected_net_pct': .3,
                       'valid_until': (NOW+timedelta(seconds=10)).isoformat()}}}


class ResearchHelperTests(unittest.TestCase):
    def test_missing_or_unconfirmed_book_quality_cannot_look_verified(self):
        for quality in (None, {}, {'action_blocked': False},
                        {'action_blocked': False, 'book_consistency_confirmed': False}):
            p = signal(); p['analysis']['data_quality'] = quality
            result = summarize_research(p, now=NOW)
            self.assertEqual(result['action'], 'WAIT')
            self.assertIsNone(result['neural_review']['expected_net_pct'])

    def test_fresh_candidate_is_review_without_trade_authorization_or_probability(self):
        original = signal(); before = copy.deepcopy(original)
        result = summarize_research(original, now=NOW)
        self.assertEqual(result['action'], 'REVIEW')
        self.assertFalse(result['entry_allowed']); self.assertIsNone(result['confidence'])
        self.assertAlmostEqual(result['risk_review']['price_distance_risk_pct'], 1.)
        self.assertAlmostEqual(result['risk_review']['reward_risk_before_costs'], 2.)
        self.assertEqual(result['neural_review']['expected_net_pct'], .3)
        self.assertEqual(original, before)

    def test_demo_and_historical_never_display_live_plan_or_neural_estimate(self):
        for change in ({'mode': 'demo'}, {'historical': True}, {'mode': 'archive'}):
            p = signal(); p.update(change)
            result = summarize_research(p, now=NOW)
            self.assertEqual(result['action'], 'WAIT')
            self.assertIsNone(result['risk_review']['entry'])
            self.assertIsNone(result['neural_review']['expected_net_pct'])

    def test_stale_or_future_or_missing_timestamps_block_review(self):
        for field, value in (('updated_at', NOW-timedelta(seconds=31)),
                             ('data_time', NOW-timedelta(seconds=46)),
                             ('updated_at', NOW+timedelta(seconds=1)),
                             ('data_time', None), ('updated_at', '2026-11-03T12:00:00')):
            p = signal(); p['analysis'][field] = value.isoformat() if isinstance(value, datetime) else value
            result = summarize_research(p, now=NOW)
            self.assertEqual(result['action'], 'WAIT')
            self.assertIsNone(result['risk_review']['stop'])

    def test_wrong_source_blockers_and_data_gaps_do_not_leak_raw_errors(self):
        p = signal(); p['source'] = 'Синтетические учебные данные'
        self.assertEqual(summarize_research(p, now=NOW)['action'], 'WAIT')
        for change in ({'blockers': ['secret-token-and-broker-error']},
                       {'data_quality': {'action_blocked': True}}):
            p = signal(); p['analysis'].update(change)
            result = summarize_research(p, now=NOW)
            self.assertEqual(result['action'], 'WAIT')
            self.assertNotIn('secret-token-and-broker-error', json.dumps(result))

    def test_failed_missing_or_duplicate_rules_cannot_be_promoted_by_score_100(self):
        for rules in ([{'key': key, 'pass': key != 'flow'} for key in RULE_NAMES],
                      [{'key': 'trend', 'pass': True}],
                      [{'key': key, 'pass': True} for key in RULE_NAMES]+[{'key': 'flow', 'pass': True}]):
            p = signal(); p['analysis']['rules'] = rules
            self.assertEqual(summarize_research(p, now=NOW)['action'], 'WAIT')

    def test_invalid_risk_plan_fails_even_if_all_rules_pass(self):
        for entry, stop, target in ((100., 101., 102.), (100., 99., 98.),
                                    (float('nan'), 99., 102.), (100., True, 102.)):
            p = signal(); p['analysis']['plan'] = {'entry': entry, 'stop': stop, 'target': target}
            result = summarize_research(p, now=NOW)
            self.assertEqual(result['action'], 'WAIT')
            self.assertIsNone(result['risk_review']['entry'])

    def test_expired_or_nonfinite_neural_output_is_not_explained_as_fresh(self):
        for change in ({'valid_until': (NOW-timedelta(seconds=1)).isoformat()},
                       {'expected_net_pct': float('inf')}, {'expected_net_pct': True}):
            p = signal(); p['analysis']['neural'].update(change)
            r = summarize_research(p, now=NOW)
            self.assertIsNone(r['neural_review']['expected_net_pct'])
            self.assertIsNone(r['confidence'])

    def test_unknown_paper_outcome_hides_a_positive_known_subset_return(self):
        report = {'status': 'ok', 'summary': {'closed': 40, 'unverified': 1, 'open': 0, 'pending': 0,
                                             'net_return_pct': 30.}}
        r = summarize_research(signal(), now=NOW, paper_report=report)
        self.assertEqual(r['research_review']['paper']['state'], 'incomplete')
        self.assertIsNone(r['research_review']['paper']['closed_sample_return_pct'])
        self.assertEqual(r['research_review']['paper']['unknown'], 1)

    def test_zero_trades_or_pending_position_is_not_profit_evidence(self):
        for closed, pending in ((0, 0), (1, 1)):
            report = {'status': 'ok', 'summary': {'closed': closed, 'unverified': 0, 'open': 0,
                                                 'pending': pending, 'net_return_pct': 5.}}
            r = summarize_research(signal(), now=NOW, paper_report=report)
            self.assertIsNone(r['research_review']['paper']['closed_sample_return_pct'])

    def test_available_model_and_forward_pairs_remain_separate_from_real_income(self):
        report = {'status': 'ok', 'summary': {'closed': 4, 'unverified': 0, 'open': 0,
                                             'pending': 0, 'net_return_pct': 1.2}}
        r = summarize_research(signal(), now=NOW, paper_report=report,
            forward_report={'status': 'ok', 'buy_candidates': {'samples': 7, 'average_pct': .9}},
            model_report={'status': 'ok', 'paper_ready': False, 'entry_allowed': True})
        self.assertEqual(r['research_review']['forward_candidate_pairs'], 7)
        self.assertEqual(r['research_review']['paper']['closed_sample_return_pct'], 1.2)
        self.assertFalse(r['entry_allowed'])
        self.assertIn('не пройден', r['research_review']['historical_model_note'])

    def test_malformed_inputs_are_json_safe_and_naive_review_time_is_rejected(self):
        r = summarize_research(None, now=NOW, model_report=[], paper_report=[], forward_report=[])
        json.dumps(r, allow_nan=False)
        self.assertEqual(r['data_review']['state'], 'unavailable')
        with self.assertRaises(ValueError):
            summarize_research(signal(), now=NOW.replace(tzinfo=None))

    def test_unhashable_rule_key_and_overflowing_prices_fail_closed(self):
        p = signal(); p['analysis']['rules'] = [{'key': [], 'pass': True}]
        self.assertEqual(summarize_research(p, now=NOW)['action'], 'WAIT')
        p = signal(); p['analysis']['plan']['entry'] = 10**1000
        result = summarize_research(p, now=NOW)
        self.assertEqual(result['action'], 'WAIT')
        json.dumps(result, allow_nan=False)

    def test_unavailable_forward_report_is_not_a_zero_observation_claim(self):
        unavailable = summarize_research(signal(), now=NOW)['research_review']
        empty = summarize_research(signal(), now=NOW,
            forward_report={'status': 'ok', 'buy_candidates': {'samples': 0}})['research_review']
        self.assertIsNone(unavailable['forward_candidate_pairs'])
        self.assertIn('не получен', unavailable['forward_note'])
        self.assertEqual(empty['forward_candidate_pairs'], 0)
        self.assertIn('пока нет', empty['forward_note'])
