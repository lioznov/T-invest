"""One manual, read-only, time-gated forecast. Never trades or sends Telegram."""
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from dashboard import morning_model as m
from dashboard.morning_service import load_artifact
from dashboard.market_provider import open_client, resolve, error_message
from dashboard.market_stream_provider import exchange_candles
from .train_morning_forecast import immutable


def now_utc():
    return datetime.now(timezone.utc)


def cutoff(now):
    local = now.astimezone(m.MSK)
    at = m.clock(local.date(), 17, 45)
    if local.weekday() > 3 or not at <= local < m.clock(local.date(), 18):
        raise CommandError('Новый прогноз доступен только пн–чт с17:45 до18:00 МСК. Старый не выдаётся за свежий.')
    return at


def schedule_check(response, at):
    """Check the current published schedule; never invent a historical calendar."""
    checks = (m.clock(at.date(), 18), m.clock(at.date()+timedelta(days=1), 10))
    proof = []
    for point in checks:
        candidates = [day for exchange in response.exchanges if exchange.exchange == 'MOEX'
                      for day in exchange.days if day.date.date() == point.date()]
        if len(candidates) != 1:
            raise CommandError('Нет однозначного расписания MOEX на вход/утреннюю цель. Прогноз не сохранён.')
        day = candidates[0]
        start, end = day.start_time, day.end_time
        clearing_start, clearing_end = day.clearing_start_time, day.clearing_end_time
        if (day.is_trading_day is not True or not start or not end or start.tzinfo is None or end.tzinfo is None
                or not start <= point < end
                or clearing_start and clearing_end and clearing_start <= point < clearing_end):
            raise CommandError('Вход или утренняя цель вне подтверждённой торговой сессии. Прогноз не сохранён.')
        proof.append({'date':point.date().isoformat(), 'exchange':'MOEX', 'is_trading_day':True,
                      'start':start.isoformat(), 'end':end.isoformat(), 'checked_time':point.isoformat()})
    return proof


def generate(ticker, now):
    """Return a forecast from fresh closed exchange bars, with no outcome attached."""
    at = cutoff(now)
    artifact = load_artifact(now)
    if artifact is None:
        raise CommandError('Сначала обучи неизменённую модель: python manage.py train_morning_forecast')
    if ticker not in m.TICKERS or ticker not in artifact['tickers']:
        raise CommandError('Этот тикер не входил в обучение отдельной модели.')
    from t_tech.invest.schemas import CandleInterval
    with open_client() as client:
        instrument = resolve(client, ticker)
        expected = artifact['instruments'][ticker]
        if any(instrument[key] != expected[key] for key in ('ticker', 'uid', 'lot', 'currency')):
            raise CommandError('Инструмент/лот изменён относительно обучения. Нужна отдельная новая проверка.')
        schedules = client.instruments.trading_schedules(exchange='MOEX', from_=now,
            to=m.clock(at.date()+timedelta(days=2), 0).astimezone(timezone.utc))
        schedule = schedule_check(schedules, at)
        status = client.market_data.get_trading_status(instrument_id=instrument['uid'])
        if status.trading_status.name != 'SECURITY_TRADING_STATUS_NORMAL_TRADING' or not status.api_trade_available_flag:
            raise CommandError('Текущий торговый статус акции не подтверждён; прогноз не сохранён.')
        start = m.clock(at.date()-timedelta(days=60), 0).astimezone(timezone.utc)
        bars = exchange_candles(client, instrument['uid'], start, at.astimezone(timezone.utc),
                                CandleInterval.CANDLE_INTERVAL_15_MIN, 15)
    m.validate(bars)
    if any(not start <= b.time or b.time+timedelta(minutes=15) > at for b in bars):
        raise CommandError('Свечи за границами зафиксированного окна; будущие данные запрещены.')
    rows, quality = m.build_examples(bars, ticker, instrument['lot'])
    row = next((r for r in rows if r['at'] == at.isoformat()), None)
    if row is None:
        raise CommandError('Недостаточно завершённых свечей до17:45 либо нет20 полных предыдущих дней.')
    # The fetch ends at cutoff; later entry and target may never be known here.
    if row['actual_price'] is not None or row['entry_price'] is not None:
        raise CommandError('Обнаружены будущие относительно прогноза данные; расчёт заблокирован.')
    result = m.forecast(artifact['model'], row)
    result['historical'] = False
    result.pop('actual_price', None)
    source = {'source':'T-Bank SDK GetCandles', 'candle_source':'CANDLE_SOURCE_EXCHANGE',
              'from_utc':start.isoformat(), 'to_utc_exclusive':at.astimezone(timezone.utc).isoformat(),
              'instrument':instrument, 'schedule':schedule, 'quality':quality,
              'bars':[{**asdict(b), 'time':b.time.isoformat()} for b in bars]}
    return artifact, result, source


class Command(BaseCommand):
    help = 'Save one fresh next-morning research forecast, read-only; manual window Mon–Thu17:45–18:00MSK.'

    def add_arguments(self, parser):
        parser.add_argument('--ticker', default='SBER', choices=m.TICKERS)
        parser.add_argument('--fetch', action='store_true', help='Explicitly allow read-only T-Bank API requests.')

    def handle(self, *args, **options):
        if not options['fetch']:
            raise CommandError('Для свежих данных явно добавь --fetch. Обучение и сделки не запускаются.')
        now = now_utc()
        cutoff(now)  # Reject outside the window before any model/broker work.
        try:
            artifact, result, source = generate(options['ticker'], now)
            created = now_utc()
            if cutoff(created).isoformat() != result['as_of']:
                raise CommandError('Расчёт не завершился до18:00 МСК; просроченный прогноз не сохраняется.')
            root = Path(settings.BASE_DIR)/'data'/'morning'; root.mkdir(parents=True, exist_ok=True)
            stamp = created.strftime('%Y%m%d-%H%M%S-%f')+'-'+options['ticker']
            source_name = 'input-'+stamp+'.json'
            source['artifact_sha256'] = m.digest(source)
            immutable(root/source_name, source)
            reference = {'version':m.VERSION, 'entry_allowed':False, 'created_at':created.isoformat(),
                         'model_artifact_sha256':artifact['artifact_sha256'],
                         'source_sha256':m.source_hashes(), 'forecasts':{options['ticker']:result},
                         'input_file':source_name, 'input_sha256':source['artifact_sha256']}
            reference['artifact_sha256'] = m.digest(reference)
            immutable(root/('forecast-'+stamp+'.json'), reference)
        except CommandError:
            raise
        except Exception as exc:
            raise CommandError(error_message(exc)) from None
        self.stdout.write(f'{options["ticker"]}: прогноз сохранён до следующего10:00 МСК; направление {result["direction"]}. '
                          'Это эксперимент, не поручение купить/продать. Биржевых заявок и сообщений боту нет.')
