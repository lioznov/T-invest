"""Read-only segmented stream supervision, with visible gaps and closed candles.

No order calls, trader identities, synthetic gap fills, or automatic deletion.
Separate sessions preserve the older bounded archive unchanged.
"""
import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from .management.commands.collect_microstructure import consume_stream
from .microstructure_archive import (ArchiveLimit, MicrostructureArchive, StreamControl,
                                    bounded_client, enum_name, price_number, stamp,
                                    subscription_requests, validate_options)
from .market_provider import error_message, resolve


SEGMENT_BYTES = 20_000_000
SIDE_BYTES = 4_000_000
RESERVE_BYTES = SEGMENT_BYTES + SIDE_BYTES + 2_000_000
ACK_FIELDS = (("subscribe_candles_response", "candles_subscriptions"),
              ("subscribe_info_response", "info_subscriptions"))


def options(tickers, seconds, segment_seconds, max_bytes):
    tickers, _ = validate_options(tickers, 1)
    for number, minimum, maximum in ((seconds, 1, 86400), (segment_seconds, 5, 3600),
                                     (max_bytes, RESERVE_BYTES, 2_000_000_000)):
        if isinstance(number, bool) or not isinstance(number, int) or not minimum <= number <= maximum:
            raise ValueError("Неверные лимиты: сеанс 1–86400 сек., сегмент 5–3600 сек., архив 26–2000 МБ.")
    return tickers


@contextmanager
def exclusive_watch(root):
    root.mkdir(parents=True, exist_ok=True)
    lock = root/"active.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise ValueError("Уже есть active.lock: второй сбор не запущен. После аварии сначала проверьте процесс, затем уберите только этот lock.") from None
    try:
        with os.fdopen(fd, "w", encoding="ascii") as stream:
            stream.write(str(os.getpid()))
        yield
    finally:
        lock.unlink(missing_ok=True)


def disk_usage(root):
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file())


class Journal:
    """Bounded session status; separate from price files, readable by website."""
    def __init__(self, path, tickers, seconds):
        self.db = sqlite3.connect(path, timeout=2, check_same_thread=False)
        self.lock = threading.Lock()
        self.db.execute("PRAGMA max_page_count=244")  # ~1 MB, no unlimited event log
        self.db.executescript("""
            CREATE TABLE health (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL);
            CREATE TABLE segments (id INTEGER PRIMARY KEY, started_at TEXT, ended_at TEXT,
                status TEXT, stats TEXT, book_file TEXT, extra_file TEXT);
        """)
        self.state = {"version": "continuous_watch_v1", "tickers": tickers, "requested_seconds": seconds,
                      "started_at": stamp(datetime.now(timezone.utc)), "status": "starting",
                      "heartbeat_at": None, "last_message_at": None, "segments": 0, "reconnects": 0,
                      "books": 0, "trades": 0, "candles": 0, "info": 0, "gaps_possible": True,
                      "entry_allowed": False}
        self.change()

    def change(self, **changes):
        with self.lock, self.db:
            self.state.update(changes)
            self.state["heartbeat_at"] = stamp(datetime.now(timezone.utc))
            self.db.execute("INSERT OR REPLACE INTO health VALUES (1,?)",
                            (json.dumps(self.state, allow_nan=False),))

    def segment(self, number, start, status, stats, book_file, extra_file):
        with self.lock, self.db:
            self.db.execute("INSERT INTO segments VALUES (?,?,?,?,?,?,?)",
                            (number, start, stamp(datetime.now(timezone.utc)), status,
                             json.dumps(stats), book_file, extra_file))

    def close(self):
        self.db.close()


class ExtraArchive:
    """Append raw closed-candle/status observations, not reconstructed history."""
    def __init__(self, path):
        self.db = sqlite3.connect(path, timeout=2)
        self.db.execute(f"PRAGMA max_page_count={SIDE_BYTES//4096}")
        self.db.execute("""CREATE TABLE events (id INTEGER PRIMARY KEY, kind TEXT, ticker TEXT,
            received_at TEXT, exchange_at TEXT, causal_eligible INTEGER, flags TEXT, payload TEXT)""")
        self.db.commit()
        self.counts = {"candles": 0, "info": 0, "flagged_extra": 0}

    def append(self, kind, item, instrument, received, acked):
        flags = []
        exchange = getattr(item, "time", None)
        if not isinstance(exchange, datetime) or exchange.tzinfo is None:
            exchange = None
            flags.append("exchange_clock_unavailable")
        if kind == "candle":
            interval = enum_name(getattr(item, "interval", None))
            minutes = {"SUBSCRIPTION_INTERVAL_ONE_MINUTE": 1, "SUBSCRIPTION_INTERVAL_FIFTEEN_MINUTES": 15}.get(interval)
            source = enum_name(getattr(item, "candle_source_type", None))
            prices = {key: price_number(getattr(item, key, None)) for key in ("open", "high", "low", "close")}
            volume = getattr(item, "volume", None)
            if any(value is None for value in prices.values()):
                flags.append("invalid_prices")
            elif not prices["low"] <= min(prices["open"], prices["close"]) <= max(prices["open"], prices["close"]) <= prices["high"]:
                flags.append("invalid_ohlc")
            if isinstance(volume, bool) or not isinstance(volume, int) or volume < 0:
                volume = None
                flags.append("invalid_volume")
            if minutes is None or exchange is None or exchange+timedelta(minutes=minutes) > received:
                flags.append("not_known_closed")
            if source != "CANDLE_SOURCE_EXCHANGE":
                flags.append("source_not_exchange")
            if (instrument["uid"], interval) not in acked:
                flags.append("closed_exchange_subscription_not_confirmed")
            payload = {**prices, "volume": volume, "interval": interval, "source": source,
                       "volume_buy": getattr(item, "volume_buy", None), "volume_sell": getattr(item, "volume_sell", None),
                       "lot_size": instrument["lot"], "waiting_close_requested": True}
            self.counts["candles"] += 1
        else:
            payload = {"status": enum_name(getattr(item, "trading_status", None)),
                       "market_orders_available": getattr(item, "market_order_available_flag", None),
                       "limit_orders_available": getattr(item, "limit_order_available_flag", None)}
            self.counts["info"] += 1
        if exchange is not None and exchange > received:
            flags.append("future_exchange_clock")
        try:
            with self.db:
                self.db.execute("INSERT INTO events VALUES (NULL,?,?,?,?,?,?,?)",
                                (kind, instrument["ticker"], stamp(received), stamp(exchange) if exchange else None,
                                 int(not flags), json.dumps(flags), json.dumps(payload, allow_nan=False)))
        except sqlite3.DatabaseError as exc:
            if getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_FULL:
                raise ArchiveLimit("Достигнут лимит отдельного архива свечей/статусов.") from None
            raise
        self.counts["flagged_extra"] += bool(flags)

    def close(self):
        self.db.close()


def requests(instruments, control):
    from t_tech.invest.schemas import (MarketDataRequest, SubscribeCandlesRequest, CandleInstrument,
                                      CandleSource, SubscriptionInterval, SubscriptionAction,
                                      SubscribeInfoRequest, InfoInstrument)
    base = subscription_requests(instruments, control)
    yield next(base)
    yield next(base)
    yield MarketDataRequest(subscribe_candles_request=SubscribeCandlesRequest(
        subscription_action=SubscriptionAction.SUBSCRIPTION_ACTION_SUBSCRIBE, waiting_close=True,
        candle_source_type=CandleSource.CANDLE_SOURCE_EXCHANGE,
        instruments=[CandleInstrument(instrument_id=row["uid"], interval=interval)
                     for row in instruments for interval in (SubscriptionInterval.SUBSCRIPTION_INTERVAL_ONE_MINUTE,
                                                             SubscriptionInterval.SUBSCRIPTION_INTERVAL_FIFTEEN_MINUTES)]))
    yield MarketDataRequest(subscribe_info_request=SubscribeInfoRequest(
        subscription_action=SubscriptionAction.SUBSCRIPTION_ACTION_SUBSCRIBE,
        instruments=[InfoInstrument(instrument_id=row["uid"]) for row in instruments]))
    yield from base


class StreamAdapter:
    """Add chart/status collection to the tested bounded book/tape consumer."""
    def __init__(self, client, instruments, control, extra, journal):
        self.client, self.instruments, self.control = client, instruments, control
        self.extra, self.journal = extra, journal
        self.market_data_stream = SimpleNamespace(market_data_stream=self.stream)
        self.acked = set()
        self.last_message = time.monotonic()
        self.last_health_write = 0.

    def stream(self, unused):
        known = {row["uid"]: row for row in self.instruments}
        source = self.client.market_data_stream.market_data_stream(requests(self.instruments, self.control))
        try:
            for message in source:
                self.last_message = time.monotonic()
                received = datetime.now(timezone.utc)
                if self.last_message-self.last_health_write >= 2:
                    self.journal.change(last_message_at=stamp(received), status="collecting")
                    self.last_health_write = self.last_message
                for field, entries in ACK_FIELDS:
                    for subscription in getattr(getattr(message, field, None), entries, []) or []:
                        if enum_name(subscription.subscription_status) != "SUBSCRIPTION_STATUS_SUCCESS":
                            raise ValueError("Candle/info subscription rejected")
                        if field == "subscribe_candles_response":
                            if (getattr(subscription, "waiting_close", None) is True
                                    and enum_name(getattr(subscription, "candle_source_type", None)) == "CANDLE_SOURCE_EXCHANGE"):
                                self.acked.add((subscription.instrument_uid, enum_name(subscription.interval)))
                for kind, field in (("candle", "candle"), ("info", "trading_status")):
                    item = getattr(message, field, None)
                    uid = getattr(item, "instrument_uid", None)
                    if uid in known:
                        self.extra.append(kind, item, known[uid], received, self.acked)
                yield message
        finally:
            close = getattr(source, "close", None)
            if callable(close):
                close()


def supervise(root, token, tickers, seconds=28800, segment_seconds=300, max_bytes=1_000_000_000,
              progress=lambda text: None, analysis_worker=None):
    tickers = options(tickers, seconds, segment_seconds, max_bytes)
    root = Path(root).resolve()
    with exclusive_watch(root):
        session = root/(datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")+"-"+uuid.uuid4().hex[:8])
        session.mkdir()
        journal = Journal(session/"health.sqlite3", tickers, seconds)
        end = time.monotonic()+seconds
        instruments, failures, number, status = None, 0, 0, "deadline"
        try:
            if analysis_worker is not None:
                analysis_worker.start(session)
                journal.change(signal_worker_running=analysis_worker.alive)
            while time.monotonic() < end:
                if number >= 1000 or disk_usage(root)+RESERVE_BYTES > max_bytes:
                    status = "disk_limit"
                    break
                number += 1
                control = StreamControl(min(segment_seconds, max(.01, end-time.monotonic())))
                book_file, extra_file = f"{number:04d}-book.sqlite3", f"{number:04d}-chart.sqlite3"
                start = stamp(datetime.now(timezone.utc))
                base_totals = {key: journal.state[key] for key in ("books", "trades", "candles", "info")}
                journal.change(status="connecting", segments=number)
                outcome, stats, idle = "error", {}, threading.Event()
                pulse_stop = threading.Event()
                extra = ExtraArchive(session/extra_file)
                try:
                    with MicrostructureArchive(session/book_file, max_bytes=SEGMENT_BYTES) as archive:
                        archive.begin_run(tickers, max(1, int(control.remaining)), datetime.now(timezone.utc))
                        try:
                            with bounded_client(token, control) as client:
                                if instruments is None:
                                    instruments = [resolve(client, ticker) for ticker in tickers]
                                adapter = StreamAdapter(client, instruments, control, extra, journal)

                                def heartbeat():
                                    while not pulse_stop.wait(5):
                                        try:
                                            journal.change(signal_worker_running=bool(analysis_worker and analysis_worker.alive))
                                        except sqlite3.Error:
                                            idle.set()
                                            control.cancel()
                                            return
                                        if time.monotonic()-adapter.last_message > 90:
                                            idle.set()
                                            control.cancel()
                                            return

                                pulse = threading.Thread(target=heartbeat, daemon=True)
                                pulse.start()
                                try:
                                    outcome, stats, safe_error = consume_stream(adapter, archive, instruments, control,
                                        progress=lambda row: journal.change(**{
                                            key: base_totals[key]+(extra.counts.get(key, 0) if key in ("candles", "info") else row.get(key, 0))
                                            for key in base_totals}))
                                    if safe_error:
                                        progress(safe_error)
                                finally:
                                    pulse_stop.set()
                                    pulse.join(timeout=6)
                                if idle.is_set():
                                    outcome = "idle_timeout"
                        except KeyboardInterrupt:
                            outcome = "interrupted"
                        except ArchiveLimit:
                            outcome = "limit"
                        except Exception as exc:
                            progress(error_message(exc))
                        finally:
                            control.cancel()
                            stats.update(extra.counts)
                            if outcome == "idle_timeout":
                                stats["idle_timeout"] = True
                            archive.finish_run("error" if outcome == "idle_timeout" else outcome,
                                               stats, datetime.now(timezone.utc))
                finally:
                    extra.close()
                journal.segment(number, start, outcome, stats, book_file, extra_file)
                totals = {key: base_totals[key]+stats.get(key, 0) for key in base_totals}
                journal.change(**totals)
                progress(f"Сегмент {number}: {outcome}; стаканов {totals['books']}, сделок {totals['trades']}, свечей {totals['candles']}.")
                if outcome in ("interrupted", "subscription_rejected"):
                    status = outcome
                    break
                # Rotations/reconnections are explicit boundaries, NEVER uninterrupted history.
                journal.change(status="reconnecting", reconnects=number)
                failures = failures+1 if outcome in ("error", "stream_ended", "idle_timeout") else 0
                if failures >= 8:
                    status = "retries_exhausted"
                    break
                if failures:
                    time.sleep(min(30, 2**failures, max(0, end-time.monotonic())))
        except KeyboardInterrupt:
            status = "interrupted"
        except (OSError, sqlite3.Error, ArchiveLimit):
            status = "archive_error"
        finally:
            try:
                if analysis_worker is not None:
                    analysis_worker.stop()
            finally:
                journal.change(status=status, signal_worker_running=False, finished_at=stamp(datetime.now(timezone.utc)))
                journal.close()
        return session, status


def load_health(root, now=None):
    """Read-only status, never a trading authorization or proof of complete tape."""
    now = now or datetime.now(timezone.utc)
    files = sorted(Path(root).glob("*/health.sqlite3"),
                   key=lambda p: p.stat().st_mtime_ns, reverse=True)[:1]
    if not files:
        return {"status": "not_started", "message": "Фоновый сбор не запускался.", "entry_allowed": False}
    try:
        if not files[0].resolve().is_relative_to(Path(root).resolve()) or files[0].stat().st_size > 2_000_000:
            raise ValueError("Invalid health file")
        with closing(sqlite3.connect(files[0].resolve().as_uri()+"?mode=ro", uri=True, timeout=.2)) as db:
            payload = json.loads(db.execute("SELECT payload FROM health WHERE id=1").fetchone()[0])
        age = (now-datetime.fromisoformat(payload["heartbeat_at"])).total_seconds()
        active = payload["status"] in ("starting", "connecting", "collecting", "reconnecting")
        payload["alive"] = bool(active and 0 <= age <= 20)
        if active and not payload["alive"]:
            payload["status"] = "stale"
        payload["entry_allowed"] = False
        payload["message"] = ("Сбор работает отдельно от браузера. " if payload["alive"] else "Работа сборщика сейчас не подтверждена. ")+"Границы соединений записываются; пропуски не дорисовываются."
        return payload
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
        return {"status": "unavailable", "message": "Статус сбора недоступен.", "entry_allowed": False}
