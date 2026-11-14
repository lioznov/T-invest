"""One bounded collector for both channels and both analysis subscriptions."""
from django.core.management.base import CommandError
from .watch_vk import Command as WatchCommand
from dashboard.telegram_alerts import Store as TelegramStore
from dashboard.vk_alerts import Store as VKStore


class Command(WatchCommand):
    help='Сбор стакана и ленты для включённых уведомлений анализа ВК и Telegram. Только чтение.'

    def get_tickers(self):
        # The supervisor and its limits are the same as watch_vk/watch_telegram.
        configs=[VKStore().config(),TelegramStore().config()]
        tickers=list(dict.fromkeys(t for c in configs if c['enabled'] for t in c['tickers']))
        if not tickers:raise CommandError('Сначала включи канал анализа и выбери акции на сайте.')
        return tickers
