"""Explicit, rate-limited multi-year EXCHANGE download; resume without overwrite."""
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from dashboard import morning_model as m
from dashboard import multiyear_inputs as inputs
from dashboard.market_provider import error_message


def diagnostic(exc):
    """Only an exception class and allowlisted RPC status; never SDK details."""
    cls = type(exc).__name__
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,79}', cls):
        cls = 'Exception'
    try:
        code = getattr(exc, 'code', None)
        code = code() if callable(code) else code
        name = getattr(code, 'name', None)
    except Exception:
        name = None
    known = {'OK', 'CANCELLED', 'UNKNOWN', 'INVALID_ARGUMENT', 'DEADLINE_EXCEEDED', 'NOT_FOUND',
             'ALREADY_EXISTS', 'PERMISSION_DENIED', 'RESOURCE_EXHAUSTED', 'FAILED_PRECONDITION',
             'ABORTED', 'OUT_OF_RANGE', 'UNIMPLEMENTED', 'INTERNAL', 'UNAVAILABLE', 'DATA_LOSS',
             'UNAUTHENTICATED'}
    return f'{cls}/{name if name in known else "NO_RPC_CODE"}'


class Command(BaseCommand):
    help = 'Validate cached multi-year history; --fetch explicitly permits read-only broker downloads. Never orders or Telegram.'

    def add_arguments(self, parser):
        parser.add_argument('--fetch', action='store_true')
        parser.add_argument('--daily', action='store_true')
        parser.add_argument('--intraday', action='store_true')
        parser.add_argument('--workers', type=int, default=3)
        parser.add_argument('--ticker', action='append', choices=m.TICKERS)

    def handle(self, *args, **options):
        workers = options['workers']
        if not 1 <= workers <= 3:
            raise CommandError('--workers должен быть от1 до3; общий лимит запросов сохраняется.')
        intervals = tuple(interval for interval, selected in
                          (('day', options['daily']), ('15m', options['intraday'])) if selected)
        if not intervals:
            intervals = ('day', '15m')
        tickers = tuple(dict.fromkeys(options.get('ticker') or m.TICKERS))
        root = Path(settings.BASE_DIR)/'data'/'multiyear'
        log = logging.getLogger('t_tech.invest')
        previous = log.level
        log.setLevel(logging.CRITICAL)
        failures = {}
        output_lock = inputs.threading.Lock()

        def progress(message):
            with output_lock:
                self.stdout.write(message)
                self.stdout.flush()

        try:
            # Metadata and daily history first; long intraday work starts only afterwards.
            phases = ((),)+tuple((interval,) for interval in intervals)
            for phase in phases:
                phase_name = ','.join(phase) or 'metadata'
                progress(f'Этап {phase_name}: {len(tickers)-len(failures)} акций; EXCHANGE, конец {inputs.END.isoformat()}.')
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = {pool.submit(inputs.collect_stock, root, ticker, phase,
                                           fetch=options['fetch'], progress=progress): ticker
                               for ticker in tickers if ticker not in failures}
                    for future in as_completed(futures):
                        ticker = futures[future]
                        try:
                            future.result()
                        except Exception as exc:
                            # Only our static validation messages may be printed verbatim.
                            if isinstance(exc, (ValueError, KeyError, TypeError, OSError, CommandError)):
                                message = 'Кэш отсутствует, повреждён или не прошёл проверку; существующие файлы не перезаписаны.'
                            else:
                                message = error_message(exc)
                            failures[ticker] = message
                            progress(f'{ticker} {phase_name} [{diagnostic(exc)}]: {message}')
        finally:
            log.setLevel(previous)
        if failures:
            raise CommandError('Не завершены: '+', '.join(sorted(failures))+'. Можно повторить команду с --fetch; исправные блоки сохраняются.')
        self.stdout.write('Все выбранные блоки проверены. История не означает прибыльность; торговля и Telegram не включены.')
