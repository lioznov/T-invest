import logging
import os
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from dashboard.continuous_watch import options as validate_watch, supervise
from dashboard.signal_worker import SignalWorker


class Command(BaseCommand):
    help = "Read-only continuous book/tape/closed 1m+15m candles; segmented archive and bounded reconnects."

    def add_arguments(self, parser):
        parser.add_argument("--tickers", nargs="+", default=["SBER", "GAZP", "LKOH"])
        parser.add_argument("--seconds", type=int, default=28800)
        parser.add_argument("--segment-seconds", type=int, default=300)
        parser.add_argument("--max-mb", type=int, default=1000)
        parser.add_argument("--signals", action="store_true", help="Also run existing read-only signal analysis every 30s, independently of browser.")

    def handle(self, *args, **options):
        token = os.getenv("INVEST_TOKEN", "").strip()
        if not token:
            raise CommandError("Нужен INVEST_TOKEN для чтения данных. Торговые поручения не отправляются.")
        logger = logging.getLogger("t_tech.invest")
        level = logger.level
        logger.setLevel(logging.CRITICAL)
        self.stdout.write("Только наблюдение, без сделок/шорта/бота. Остановить: Ctrl+C. Сон компьютера прервёт сбор.")
        try:
            tickers = validate_watch(options["tickers"], options["seconds"], options["segment_seconds"], options["max_mb"]*1_000_000)
            worker = SignalWorker(tickers, options["seconds"], settings.BASE_DIR) if options["signals"] else None
            session, status = supervise(Path(settings.BASE_DIR)/"data"/"watch", token,
                tickers, options["seconds"], options["segment_seconds"],
                options["max_mb"]*1_000_000, progress=self.stdout.write, analysis_worker=worker)
            self.stdout.write(f"STOP: {status}. Архив сохранён: {session.name}. Прибыльность не подтверждена.")
        except ValueError as exc:
            raise CommandError(str(exc)) from None
        finally:
            logger.setLevel(level)
