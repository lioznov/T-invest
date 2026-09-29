"""Keep a small read-only watchlist observed while the browser is closed."""
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

from django.core.management.base import BaseCommand, CommandError

from dashboard.market_provider import error_message, live_snapshot
from dashboard.signal_service import archive
from dashboard.signals import analyse


class Command(BaseCommand):
    help = 'Archive real T-Bank signal observations in the foreground; no orders are placed.'

    def add_arguments(self, parser):
        parser.add_argument('--tickers', nargs='+', default=['SBER', 'GAZP', 'LKOH'])
        parser.add_argument('--interval', type=int, default=30, help='Seconds between rounds, 30–40')
        parser.add_argument('--once', action='store_true', help='Fetch one round and exit')

    def handle(self, *args, **options):
        symbols = list(dict.fromkeys(t.upper() for t in options['tickers']))
        if not 1 <= len(symbols) <= 5 or any(
                not re.fullmatch(r'[A-Z0-9][A-Z0-9.\-]{0,14}', symbol) for symbol in symbols):
            raise CommandError('Укажите от 1 до 5 биржевых тикеров латиницей.')
        interval = options['interval']
        if not 30 <= interval <= 40:
            raise CommandError('Интервал должен быть от 30 до 40 секунд для проверки устойчивости стакана.')
        previous = {}
        last_state = {}
        self.stdout.write('Наблюдаем: ' + ', '.join(symbols) + '. Остановить: Ctrl+C.')

        def fetch(ticker):
            try:
                return ticker, live_snapshot(ticker, previous.get(ticker)), None
            except Exception as exc:
                return ticker, None, error_message(exc)

        try:
            with ThreadPoolExecutor(max_workers=min(3, len(symbols))) as pool:
                while True:
                    started = time.monotonic()
                    fetched = 0
                    trading = 0
                    for ticker, snapshot, error in pool.map(fetch, symbols):
                        if error:
                            state = ('error', error)
                        else:
                            fetched += 1
                            trading += bool(snapshot.get('trading'))
                            analysis = analyse(snapshot)
                            try:
                                archive(ticker, snapshot, analysis)
                            except (OSError, sqlite3.Error):
                                state = ('error', 'Не удалось сохранить локальное наблюдение.')
                            else:
                                previous[ticker] = snapshot
                                state = (analysis['decision'], f"{analysis['score']}/100 покупки")
                        if state != last_state.get(ticker) or options['once']:
                            self.stdout.write(f"{ticker}: {state[0]} · {state[1]}")
                            last_state[ticker] = state
                    if options['once']:
                        break
                    pause = interval if trading else 300 if fetched else 60
                    remaining = pause - (time.monotonic() - started)
                    if remaining > 0:
                        time.sleep(remaining)
        except KeyboardInterrupt:
            self.stdout.write('Наблюдение остановлено.')
