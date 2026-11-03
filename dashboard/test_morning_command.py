"""Time-gated fresh forecast checks with synthetic bars and a read-only fake SDK."""
import copy
import io
import json
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings

from . import morning_model as m
from .management.commands import forecast_morning as command
from .signals import Candle


NOW = datetime(2026, 10, 22, 17, 50, tzinfo=m.MSK)
INSTRUMENT = {'ticker': 'SBER', 'uid': 'synthetic-uid', 'lot': 10, 'currency': 'rub'}


def bars_before_cutoff():
    result = []
    start = m.clock(NOW.date()-timedelta(days=50), 10)
    cutoff = NOW.replace(minute=45)
    for day in range(51):
        for j in range(34):
            at = start+timedelta(days=day, minutes=15*j)
            if at+timedelta(minutes=15) <= cutoff:
                p = 100.+day*.02+j*.003
                result.append(Candle(at, p, p+.2, p-.2, p+.01, 1000+j))
    return result


def schedule():
    days = []
    for offset in (0, 1):
        day = NOW.date()+timedelta(days=offset)
        days.append(SimpleNamespace(date=m.clock(day, 0), is_trading_day=True,
                    start_time=m.clock(day, 10), end_time=m.clock(day, 23),
                    clearing_start_time=m.clock(day, 14), clearing_end_time=m.clock(day, 14, 30)))
    return SimpleNamespace(exchanges=[SimpleNamespace(exchange='MOEX', days=days)])


def artifact():
    return {'tickers': ['SBER'], 'instruments': {'SBER': dict(INSTRUMENT)},
            'artifact_sha256': 'a'*64,
            'model': {'center': [0.]*len(m.FEATURES), 'scale': [1.]*len(m.FEATURES),
                      'weights': [.3]+[0.]*len(m.FEATURES),
                      'interval_abs_pct': .7, 'train_mean_pct': .05}}


class MorningCutoffAndScheduleTests(SimpleTestCase):
    def test_window_is_monday_to_thursday_1745_inclusive_1800_exclusive(self):
        for day in (19, 20, 21, 22):
            for hour, minute in ((17, 45), (17, 59)):
                now = NOW.replace(day=day, hour=hour, minute=minute)
                self.assertEqual(command.cutoff(now), now.replace(hour=17, minute=45))
        for now in (NOW.replace(minute=44), NOW.replace(hour=18, minute=0),
                    NOW.replace(day=23), NOW.replace(day=24), NOW.replace(day=25)):
            with self.subTest(now=now), self.assertRaises(CommandError):
                command.cutoff(now)
        self.assertEqual(command.cutoff(NOW.astimezone(timezone.utc)), NOW.replace(minute=45))

    def test_schedule_proves_exact_current_entry_and_next_day_target(self):
        proof = command.schedule_check(schedule(), NOW.replace(minute=45))
        self.assertEqual(len(proof), 2)
        self.assertEqual(proof[0]['checked_time'], NOW.replace(hour=18, minute=0).isoformat())
        self.assertEqual(proof[1]['checked_time'],
                         (NOW+timedelta(days=1)).replace(hour=10, minute=0).isoformat())
        self.assertTrue(all(p['exchange'] == 'MOEX' and p['is_trading_day'] for p in proof))

    def test_closed_missing_or_ambiguous_schedule_is_rejected(self):
        for kind in ('closed_today', 'closed_tomorrow', 'missing', 'duplicate', 'wrong_exchange'):
            value = schedule()
            days = value.exchanges[0].days
            if kind == 'closed_today':
                days[0].is_trading_day = False
            elif kind == 'closed_tomorrow':
                days[1].is_trading_day = False
            elif kind == 'missing':
                days.pop()
            elif kind == 'duplicate':
                days.append(copy.deepcopy(days[0]))
            else:
                value.exchanges[0].exchange = 'OTHER'
            with self.subTest(kind=kind), self.assertRaises(CommandError):
                command.schedule_check(value, NOW.replace(minute=45))

    def test_entry_or_target_outside_session_and_during_clearing_is_rejected(self):
        for kind in ('late_start', 'early_end', 'clearing', 'naive_start', 'boolean_flag'):
            value = schedule()
            day = value.exchanges[0].days[0]
            if kind == 'late_start':
                day.start_time = NOW.replace(hour=19, minute=0)
            elif kind == 'early_end':
                day.end_time = NOW.replace(hour=18, minute=0)
            elif kind == 'clearing':
                day.clearing_start_time = NOW.replace(hour=17, minute=55)
                day.clearing_end_time = NOW.replace(hour=18, minute=10)
            elif kind == 'naive_start':
                day.start_time = day.start_time.replace(tzinfo=None)
            else:
                day.is_trading_day = 1
            with self.subTest(kind=kind), self.assertRaises(CommandError):
                command.schedule_check(value, NOW.replace(minute=45))


class MorningGenerateTests(SimpleTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.load = self.stack.enter_context(patch.object(command, 'load_artifact', return_value=artifact()))
        self.resolve = self.stack.enter_context(patch.object(command, 'resolve', return_value=dict(INSTRUMENT)))
        self.exchange = self.stack.enter_context(patch.object(command, 'exchange_candles', return_value=bars_before_cutoff()))
        self.schedule = schedule()
        self.status = SimpleNamespace(trading_status=SimpleNamespace(name='SECURITY_TRADING_STATUS_NORMAL_TRADING'),
                                      api_trade_available_flag=True)
        # No order/account/client methods exist: an accidental order call fails immediately.
        self.client = SimpleNamespace(
            instruments=SimpleNamespace(trading_schedules=Mock(return_value=self.schedule)),
            market_data=SimpleNamespace(get_trading_status=Mock(return_value=self.status)))
        self.open = self.stack.enter_context(patch.object(command, 'open_client', return_value=MagicMock()))
        self.open.return_value.__enter__.return_value = self.client
        self.telegram = self.stack.enter_context(patch('dashboard.telegram_transport.send',
                                                       side_effect=AssertionError('No Telegram in research forecasts')))

    def generate(self):
        return command.generate('SBER', NOW)

    def test_outside_window_and_missing_or_wrong_model_never_opens_broker(self):
        with self.assertRaises(CommandError):
            command.generate('SBER', NOW.replace(hour=18, minute=0))
        self.load.assert_not_called()
        self.load.return_value = None
        with self.assertRaises(CommandError):
            self.generate()
        self.load.return_value = artifact()
        with self.assertRaises(CommandError):
            command.generate('GAZP', NOW)
        self.open.assert_not_called()

    def test_fresh_forecast_is_bound_to_model_and_contains_no_future_outcome(self):
        trained, result, source = self.generate()
        self.assertEqual(trained['artifact_sha256'], 'a'*64)
        self.assertFalse(result['historical'])
        self.assertEqual(result['as_of'], NOW.replace(minute=45).isoformat())
        self.assertEqual(result['target_at'],
                         (NOW+timedelta(days=1)).replace(hour=10, minute=0).isoformat())
        self.assertNotIn('actual_price', result)
        self.assertNotIn('entry_price', result)
        self.assertEqual(source['candle_source'], 'CANDLE_SOURCE_EXCHANGE')
        self.assertEqual(source['instrument'], INSTRUMENT)
        self.assertEqual(len(source['schedule']), 2)
        self.assertTrue(all(datetime.fromisoformat(b['time'])+timedelta(minutes=15)
                            <= NOW.replace(minute=45) for b in source['bars']))
        self.telegram.assert_not_called()

    def test_fetch_is_bounded_by_cutoff_not_now_or_end_of_day(self):
        self.generate()
        call = self.exchange.call_args.args
        self.assertIs(call[0], self.client)
        self.assertEqual(call[1], 'synthetic-uid')
        self.assertEqual(call[2], m.clock(NOW.date()-timedelta(days=60), 0).astimezone(timezone.utc))
        self.assertEqual(call[3], NOW.replace(minute=45).astimezone(timezone.utc))
        self.assertEqual(call[5], 15)
        self.client.market_data.get_trading_status.assert_called_once_with(instrument_id='synthetic-uid')
        self.assertEqual(self.client.instruments.trading_schedules.call_args.kwargs['exchange'], 'MOEX')

    def test_uid_lot_ticker_currency_changes_block_before_market_reads(self):
        for key, changed in (('uid', 'changed'), ('lot', 20), ('ticker', 'GAZP'), ('currency', 'usd')):
            self.resolve.return_value = {**INSTRUMENT, key: changed}
            with self.subTest(key=key), self.assertRaises(CommandError):
                self.generate()
        self.exchange.assert_not_called()
        self.client.instruments.trading_schedules.assert_not_called()
        self.client.market_data.get_trading_status.assert_not_called()

    def test_closed_schedule_and_trading_unavailable_block_candle_fetch(self):
        self.schedule.exchanges[0].days[1].is_trading_day = False
        with self.assertRaises(CommandError):
            self.generate()
        self.schedule.exchanges[0].days[1].is_trading_day = True
        for kind in ('status', 'api'):
            self.status.trading_status.name = ('CLOSED' if kind == 'status'
                                              else 'SECURITY_TRADING_STATUS_NORMAL_TRADING')
            self.status.api_trade_available_flag = kind != 'api'
            with self.subTest(kind=kind), self.assertRaises(CommandError):
                self.generate()
        self.exchange.assert_not_called()

    def test_missing_current_cutoff_row_does_not_substitute_older_forecast(self):
        self.exchange.return_value = [b for b in self.exchange.return_value if b.time.date() < NOW.date()]
        with self.assertRaises(CommandError):
            self.generate()

    def test_incomplete_current_feature_day_does_not_forecast(self):
        at = NOW.replace(hour=17, minute=30)
        self.exchange.return_value = [b for b in self.exchange.return_value if b.time != at]
        with self.assertRaises(CommandError):
            self.generate()

    def test_future_or_out_of_request_source_bars_are_rejected(self):
        clean = self.exchange.return_value
        template = clean[-1]
        for at in (NOW.replace(minute=45), NOW.replace(hour=18, minute=0),
                   (NOW+timedelta(days=1)).replace(hour=10, minute=0),
                   m.clock(NOW.date()-timedelta(days=61), 10)):
            self.exchange.return_value = sorted(clean+[replace(template, time=at)], key=lambda b: b.time)
            with self.subTest(at=at), self.assertRaises(CommandError):
                self.generate()

    def test_future_row_outcome_is_defensively_rejected_even_with_valid_source(self):
        rows, quality = m.build_examples(self.exchange.return_value, 'SBER', 10)
        current = copy.deepcopy(next(r for r in rows if r['at'] == NOW.replace(minute=45).isoformat()))
        for field in ('entry_price', 'actual_price'):
            changed = {**current, field: 100.}
            with self.subTest(field=field), patch.object(m, 'build_examples', return_value=([changed], quality)):
                with self.assertRaises(CommandError):
                    self.generate()

    def test_success_command_saves_immutable_bound_input_and_reference_without_actions(self):
        with TemporaryDirectory() as directory, override_settings(BASE_DIR=Path(directory)), \
                patch.object(command, 'now_utc', side_effect=[NOW, NOW+timedelta(minutes=1)]), \
                patch.object(m, 'source_hashes', return_value={'synthetic-code': 'b'*64}):
            stdout = io.StringIO()
            call_command('forecast_morning', '--fetch', '--ticker', 'SBER', stdout=stdout)
            root = Path(directory)/'data'/'morning'
            reference = json.loads(next(root.glob('forecast-*.json')).read_text(encoding='utf-8'))
            source = json.loads((root/reference['input_file']).read_text(encoding='utf-8'))
            self.assertFalse(reference['entry_allowed'])
            self.assertEqual(reference['model_artifact_sha256'], 'a'*64)
            self.assertEqual(reference['source_sha256'], {'synthetic-code': 'b'*64})
            self.assertEqual(reference['input_sha256'], source['artifact_sha256'])
            for value in (reference, source):
                expected = m.digest({k: v for k, v in value.items() if k != 'artifact_sha256'})
                self.assertEqual(value['artifact_sha256'], expected)
            saved = reference['forecasts']['SBER']
            self.assertFalse(saved['historical'])
            self.assertNotIn('actual_price', saved)
            self.assertIn('эксперимент', stdout.getvalue())
            self.telegram.assert_not_called()


class MorningCommandSafetyTests(SimpleTestCase):
    def test_no_fetch_flag_never_reads_clock_model_or_broker(self):
        with patch.object(command, 'now_utc') as clock, patch.object(command, 'generate') as generate, \
                patch.object(command, 'open_client') as broker, patch.object(command, 'load_artifact') as model:
            with self.assertRaises(CommandError):
                call_command('forecast_morning', stdout=io.StringIO())
            clock.assert_not_called()
            generate.assert_not_called()
            broker.assert_not_called()
            model.assert_not_called()

    def test_outside_window_fetch_flag_still_cannot_open_broker(self):
        with patch.object(command, 'now_utc', return_value=NOW.replace(hour=18, minute=0)), \
                patch.object(command, 'generate') as generate, patch.object(command, 'open_client') as broker:
            with self.assertRaises(CommandError):
                call_command('forecast_morning', '--fetch', stdout=io.StringIO())
            generate.assert_not_called()
            broker.assert_not_called()

    def test_slow_calculation_or_wrong_cutoff_never_saves_reference(self):
        fresh = {'as_of': NOW.replace(minute=45).isoformat(), 'direction': 'uncertain'}
        for created, as_of in ((NOW.replace(hour=18, minute=0), fresh['as_of']),
                               (NOW, (NOW-timedelta(days=1)).replace(minute=45).isoformat())):
            with self.subTest(created=created, as_of=as_of), TemporaryDirectory() as directory, \
                    override_settings(BASE_DIR=Path(directory)), \
                    patch.object(command, 'now_utc', side_effect=[NOW, created]), \
                    patch.object(command, 'generate', return_value=(artifact(), {**fresh, 'as_of': as_of}, {})):
                with self.assertRaises(CommandError):
                    call_command('forecast_morning', '--fetch', stdout=io.StringIO())
                self.assertFalse((Path(directory)/'data'/'morning').exists())

    def test_unexpected_broker_failure_is_sanitized_without_partial_saved_result(self):
        with TemporaryDirectory() as directory, override_settings(BASE_DIR=Path(directory)), \
                patch.object(command, 'now_utc', return_value=NOW), \
                patch.object(command, 'generate', side_effect=RuntimeError('DO_NOT_EXPOSE_FAKE_SECRET')):
            with self.assertRaises(CommandError) as caught:
                call_command('forecast_morning', '--fetch', stdout=io.StringIO())
            self.assertNotIn('DO_NOT_EXPOSE_FAKE_SECRET', str(caught.exception))
            self.assertFalse((Path(directory)/'data'/'morning').exists())
