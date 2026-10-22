"""Optional foreground worker for deployments without runserver."""
from django.core.management.base import BaseCommand
from dashboard.telegram_alerts import worker_loop


class Command(BaseCommand):
    help = 'Send opted-in live six-rule candidates to the radar Telegram bot. No orders.'

    def handle(self, *args, **options):
        self.stdout.write('Telegram: используем сохранённые настройки сайта. Остановить: Ctrl+C.')
        try:
            worker_loop()
        except KeyboardInterrupt:
            self.stdout.write('Наблюдение Telegram остановлено.')
