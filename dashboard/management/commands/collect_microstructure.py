"""Explicit, bounded, read-only collection; independent of browser and signals."""
import logging
import os
import sqlite3
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from dashboard.market_provider import MarketUnavailable, error_message, resolve
from dashboard.microstructure_archive import (ArchiveLimit, MicrostructureArchive,
                                              StreamControl, bounded_client, enum_name,
                                              subscription_requests, validate_options)


STOP_LABELS = {
    "deadline": "Истёк заданный срок",
    "once": "Получена первая рыночная запись",
    "interrupted": "Остановлено пользователем",
    "limit": "Достигнут лимит архива; старые записи сохранены",
    "error": "Сбор остановлен из-за ошибки",
    "stream_ended": "Сервер завершил поток",
    "subscription_rejected": "Сервер отклонил подписку",
}


def subscription_statuses(message):
    for field, entries in (("subscribe_order_book_response", "order_book_subscriptions"),
                           ("subscribe_trades_response", "trade_subscriptions")):
        response = getattr(message, field, None)
        subscriptions = getattr(response, entries, None)
        if isinstance(subscriptions, (list, tuple)):
            for subscription in subscriptions:
                yield enum_name(getattr(subscription, "subscription_status", None))


def consume_stream(client, archive, instruments, control, *, once=False,
                   now=lambda: datetime.now(timezone.utc), progress=None):
    """No decisions are produced; cancel/close the SDK iterator on every exit."""
    known = {row["uid"]: row for row in instruments}
    stats = Counter(books=0, trades=0, throttled=0, duplicate_ids=0,
                    ignored=0, acknowledgements=0, flagged_records=0)
    stream = None
    timer = threading.Timer(control.remaining, control.cancel)
    timer.daemon = True
    timer.start()
    status, safe_error = "stream_ended", None
    next_progress = time.monotonic() + 30
    try:
        stream = client.market_data_stream.market_data_stream(subscription_requests(instruments, control))
        for message in stream:
            received_at = now()
            if control.remaining <= 0 or control.stop.is_set():
                status = "deadline"
                break
            for subscription_status in subscription_statuses(message):
                if subscription_status == "SUBSCRIPTION_STATUS_SUCCESS":
                    stats["acknowledgements"] += 1
                else:
                    status = "subscription_rejected"
                    break
            if status == "subscription_rejected":
                break
            stored = False
            for kind, field, count in (("book", "orderbook", "books"), ("trade", "trade", "trades")):
                item = getattr(message, field, None)
                uid = getattr(item, "instrument_uid", None)
                if not isinstance(uid, str):
                    continue
                if uid not in known:
                    stats["ignored"] += 1
                    continue
                outcome = archive.append(kind, item, known[uid], received_at)
                if outcome == "stored":
                    stats[count] += 1
                    stored = True
                elif outcome == "throttled":
                    stats["throttled"] += 1
                elif outcome == "duplicate_id":
                    stats["duplicate_ids"] += 1
                if archive.count >= archive.max_records:
                    status = "limit"
                    break
            if status == "limit":
                break
            if once and stored:
                status = "once"
                break
            if progress is not None and time.monotonic() >= next_progress:
                progress(stats)
                next_progress = time.monotonic() + 30
        if status == "stream_ended" and (control.remaining <= 0 or control.stop.is_set()):
            status = "deadline"
    except KeyboardInterrupt:
        status = "interrupted"
    except ArchiveLimit:
        status = "limit"
    except Exception as exc:
        if control.remaining <= 0 or control.stop.is_set():
            status = "deadline"
        else:
            status = "error"
            safe_error = error_message(exc)
    finally:
        control.cancel()
        timer.cancel()
        if stream is not None:
            close = getattr(stream, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    stats["iterator_close_failed"] += 1
                    if safe_error is None:
                        safe_error = "Итератор потока не завершился штатно; отмена RPC и закрытие канала всё равно запрошены."
    # Count only newly stored records; SQL never deletes/relabels old messages.
    stats["flagged_records"] = archive.db.execute(
        "SELECT COUNT(*) FROM market_events WHERE run_id=? AND causal_eligible=0", (archive.run_id,)
    ).fetchone()[0]
    return status, dict(stats), safe_error


class Command(BaseCommand):
    help = "Collect a bounded T-Bank exchange book/tape stream into a separate archive; never place orders."

    def add_arguments(self, parser):
        parser.add_argument("--tickers", nargs="+", default=["SBER"], help="1–3 TQBR shares")
        parser.add_argument("--seconds", type=int, default=900, help="Whole-operation limit: 1–3600 seconds")
        parser.add_argument("--once", action="store_true", help="Stop after first stored message or at most 10 seconds")

    def handle(self, *args, **options):
        try:
            tickers, seconds = validate_options(options["tickers"], options["seconds"])
        except ValueError as exc:
            raise CommandError(str(exc)) from None
        if options["once"]:
            seconds = min(seconds, 10)
        token = os.getenv("INVEST_TOKEN", "").strip()
        if not token:
            raise CommandError("Не настроен INVEST_TOKEN. Нужен токен для чтения рыночных данных; сделки не отправляются.")
        control = StreamControl(seconds)
        path = Path(settings.BASE_DIR) / "data" / "microstructure.sqlite3"
        self.stdout.write(f"Только чтение: {', '.join(tickers)} · TQBR/EXCHANGE · максимум {seconds} сек. Остановить: Ctrl+C.")
        # SDK errors are handled below without raw exception/metadata output.
        sdk_logger = logging.getLogger("t_tech.invest")
        prior_log_level = sdk_logger.level
        sdk_logger.setLevel(logging.CRITICAL)
        stats, status = {}, "error"
        try:
            with MicrostructureArchive(path) as archive:
                archive.begin_run(tickers, seconds, datetime.now(timezone.utc))
                try:
                    with bounded_client(token, control) as client:
                        instruments = []
                        for ticker in tickers:
                            if control.remaining <= 0:
                                status = "deadline"
                                break
                            # Exact TQBR/share identity is checked by the SDK adapter.
                            instruments.append(resolve(client, ticker))
                        else:
                            status, stats, safe_error = consume_stream(
                                client, archive, instruments, control, once=options["once"],
                                progress=lambda row: self.stdout.write(
                                    f"Записано: стаканов {row['books']}, сделок {row['trades']}; сбор продолжается."))
                            if safe_error:
                                self.stderr.write(safe_error)
                except KeyboardInterrupt:
                    status = "interrupted"
                except Exception as exc:
                    status = "deadline" if control.remaining <= 0 else "error"
                    if status == "error":
                        self.stderr.write(error_message(exc))
                finally:
                    control.cancel()
                    try:
                        archive.finish_run(status, stats, datetime.now(timezone.utc))
                    except ArchiveLimit as exc:
                        status = "limit"
                        self.stderr.write(str(exc))
                self.stdout.write(f"STOP: {STOP_LABELS[status]}. Записано в сеансе: стаканов {stats.get('books', 0)}, сделок {stats.get('trades', 0)}.")
                self.stdout.write(f"Подтверждений подписки: {stats.get('acknowledgements', 0)}; не пригодных для причинной проверки записей: {stats.get('flagged_records', 0)}.")
                self.stdout.write(f"Записей в архиве: {archive.count}; размер: {path.stat().st_size} байт. Архив: data/microstructure.sqlite3.")
                self.stdout.write("Это неполные наблюдения, не доказательство прибыльности и не история заявок отдельных трейдеров. LONG/EXIT сайта не изменены.")
        except ArchiveLimit as exc:
            self.stdout.write("STOP: " + str(exc))
        except (OSError, sqlite3.Error):
            raise CommandError("Не удалось открыть или записать отдельный архив. Предыдущие записи не удалялись.") from None
        except (ImportError, MarketUnavailable):
            raise CommandError("Проверьте установленный t-tech-investments и доступность рыночных данных. Токен и ошибки SDK не выводятся.") from None
        except ValueError:
            raise CommandError("Сбор не начат: неверный формат или версия отдельного архива.") from None
        finally:
            control.cancel()
            sdk_logger.setLevel(prior_log_level)
