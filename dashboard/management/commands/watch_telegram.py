"""Collect a saved unlimited watchlist in bounded groups; no trading orders."""
import logging
import os
from pathlib import Path
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from dashboard.telegram_alerts import Store
from dashboard.wide_watch import supervise


class Command(BaseCommand):
    help = 'Collect saved Telegram watchlist in groups of 50, rotating every 15 minutes. Read-only.'

    def add_arguments(self, parser):
        parser.add_argument('--seconds', type=int, default=28800)
        parser.add_argument('--max-mb', type=int, default=1000)

    def handle(self, *args, **options):
        token = os.getenv('INVEST_TOKEN', '').strip()
        if not token:
            raise CommandError('Для чтения нужен INVEST_TOKEN.')
        tickers = Store().config()['tickers']
        logger = logging.getLogger('t_tech.invest'); level = logger.level
        logger.setLevel(logging.CRITICAL)
        self.stdout.write(f'Список: {len(tickers)} акций. Группы до 50, смена раз в 15 минут. Без заявок и шорта. Ctrl+C — остановка.')
        try:
            session, status = supervise(Path(settings.BASE_DIR)/'data'/'watch', token, tickers,
                seconds=options['seconds'], max_bytes=options['max_mb']*1_000_000, progress=self.stdout.write)
            self.stdout.write(f'STOP: {status}. Архив: {session.name}. Уведомления включаются отдельно на сайте.')
        except ValueError as exc:
            raise CommandError(str(exc)) from None
        finally:
            logger.setLevel(level)
