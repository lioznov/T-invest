"""Wide-list collector. The earlier frozen 1–3-symbol research collector is unchanged.

Reuse its observation format and stream consumer; only scheduling/metadata differs.
No orders, automatic archive deletion, fabricated history or hidden subscription cap.
"""
import json
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .continuous_watch import (ExtraArchive, Journal, StreamAdapter, RESERVE_BYTES,
                               SEGMENT_BYTES, disk_usage, exclusive_watch)
from .management.commands.collect_microstructure import consume_stream
from .market_provider import resolve, error_message, MarketUnavailable
from .microstructure_archive import ArchiveLimit, MicrostructureArchive, StreamControl, bounded_client, stamp
from .watch_universe import normalize, collection_group, COLLECTION_BATCH, ROTATION_SECONDS


class WideArchive(MicrostructureArchive):
    def begin_run(self, tickers, seconds, received_at):
        # Same schema and timestamp rules; default legacy validation remains untouched.
        symbols = normalize(tickers)
        if len(symbols) > COLLECTION_BATCH or type(seconds) is not int or not 1 <= seconds <= 3600:
            raise ValueError('Превышен размер группы или срок сегмента.')
        self.run_id = uuid.uuid4().hex
        try:
            with self.db:
                self.db.execute('INSERT INTO collection_runs VALUES (?,?,NULL,?,?,?,?)',
                    (self.run_id, stamp(received_at), seconds, json.dumps(symbols), 'running', '{}'))
        except sqlite3.DatabaseError as exc:
            if getattr(exc, 'sqlite_errorcode', None) == sqlite3.SQLITE_FULL:
                raise ArchiveLimit('Достигнут лимит архива.') from None
            raise
        return self.run_id


def supervise(root, token, tickers, seconds=28800, segment_seconds=300, max_bytes=1_000_000_000,
              progress=lambda text: None):
    tickers = normalize(tickers)
    if (type(seconds) is not int or not 1 <= seconds <= 86400
            or type(segment_seconds) is not int or not 5 <= segment_seconds <= 3600
            or type(max_bytes) is not int or not RESERVE_BYTES <= max_bytes <= 2_000_000_000):
        raise ValueError('Неверные лимиты: сеанс 1–86400 сек., сегмент 5–3600 сек., архив 26–2000 МБ.')
    root = Path(root).resolve()
    with exclusive_watch(root):
        session = root/(datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')+'-wide-'+uuid.uuid4().hex[:8])
        session.mkdir()
        selected, index, count = collection_group(tickers, 0)
        journal = Journal(session/'health.sqlite3', selected, seconds)
        journal.change(version='wide_watch_v1', watchlist_count=len(tickers), batch=index+1,
                       batch_count=count, rotation_seconds=ROTATION_SECONDS, unsupported_tickers=[])
        started = time.monotonic()
        end = started+seconds
        cache, unsupported = {}, set()
        failures, number, status = 0, 0, 'deadline'
        try:
            while time.monotonic() < end:
                if number >= 1000 or disk_usage(root)+RESERVE_BYTES > max_bytes:
                    status = 'disk_limit'
                    break
                elapsed = time.monotonic()-started
                available = [ticker for ticker in tickers if ticker not in unsupported]
                if not available:
                    status = 'no_supported'
                    break
                selected, index, count = collection_group(available, elapsed)
                until_rotation = ROTATION_SECONDS-elapsed % ROTATION_SECONDS if count > 1 else segment_seconds
                control = StreamControl(min(segment_seconds, until_rotation, max(.01, end-time.monotonic())))
                number += 1
                book_file, extra_file = f'{number:04d}-book.sqlite3', f'{number:04d}-chart.sqlite3'
                start = stamp(datetime.now(timezone.utc))
                totals = {key: journal.state[key] for key in ('books', 'trades', 'candles', 'info')}
                journal.change(status='connecting', segments=number, tickers=selected, batch=index+1, batch_count=count)
                unsupported_before = len(unsupported)
                outcome, stats, idle, pulse_stop = 'error', {}, threading.Event(), threading.Event()
                extra = ExtraArchive(session/extra_file)
                try:
                    with WideArchive(session/book_file, max_bytes=SEGMENT_BYTES) as archive:
                        archive.begin_run(selected, max(1, int(control.remaining)), datetime.now(timezone.utc))
                        try:
                            with bounded_client(token, control) as client:
                                instruments = []
                                for ticker in selected:
                                    if ticker in unsupported:
                                        continue
                                    if ticker not in cache:
                                        try:
                                            cache[ticker] = resolve(client, ticker)
                                        except MarketUnavailable:
                                            # An unknown TQBR symbol must not disable the other 49.
                                            unsupported.add(ticker)
                                            progress(f'{ticker}: акция TQBR не найдена; пропускаем до перезапуска.')
                                            continue
                                    instruments.append(cache[ticker])
                                journal.change(tickers=[i['ticker'] for i in instruments],
                                               unsupported_tickers=sorted(unsupported))
                                if not instruments:
                                    raise MarketUnavailable('В текущей группе нет доступных акций TQBR.')
                                adapter = StreamAdapter(client, instruments, control, extra, journal)

                                def heartbeat():
                                    while not pulse_stop.wait(5):
                                        try:
                                            journal.change()
                                        except sqlite3.Error:
                                            idle.set(); control.cancel(); return
                                        if time.monotonic()-adapter.last_message > 90:
                                            idle.set(); control.cancel(); return

                                pulse = threading.Thread(target=heartbeat, daemon=True)
                                pulse.start()
                                try:
                                    outcome, stats, error = consume_stream(adapter, archive, instruments, control,
                                        progress=lambda row: journal.change(**{
                                            key: totals[key]+(extra.counts.get(key, 0) if key in ('candles', 'info') else row.get(key, 0))
                                            for key in totals}))
                                    if error:
                                        progress(error)
                                finally:
                                    pulse_stop.set(); pulse.join(timeout=6)
                                if idle.is_set():
                                    outcome = 'error'
                        except KeyboardInterrupt:
                            outcome = 'interrupted'
                        except ArchiveLimit:
                            outcome = 'limit'
                        except Exception as exc:
                            progress(error_message(exc))
                        finally:
                            control.cancel()
                            stats.update(extra.counts)
                            archive.finish_run(outcome, stats, datetime.now(timezone.utc))
                finally:
                    extra.close()
                journal.segment(number, start, outcome, stats, book_file, extra_file)
                journal.change(**{key: totals[key]+stats.get(key, 0) for key in totals})
                progress(f'Группа {index+1}/{count}, сегмент {number}: {outcome}. Всего в списке {len(tickers)} акций.')
                if outcome in ('interrupted', 'subscription_rejected'):
                    status = outcome
                    break
                failures = failures+1 if outcome in ('error', 'stream_ended') and len(unsupported) == unsupported_before else 0
                if failures >= 8:
                    status = 'retries_exhausted'
                    break
                journal.change(status='reconnecting', reconnects=number)
                if failures:
                    time.sleep(min(30, 2**failures, max(0, end-time.monotonic())))
        except KeyboardInterrupt:
            status = 'interrupted'
        except (OSError, sqlite3.Error, ArchiveLimit):
            status = 'archive_error'
        finally:
            journal.change(status=status, signal_worker_running=False, finished_at=stamp(datetime.now(timezone.utc)))
            journal.close()
        return session, status
