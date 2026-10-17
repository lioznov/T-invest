"""Bounded point-in-time quote/tape archive; no signals and no order methods.

Only observed stream messages are saved. A sampled book is not a historical
order log, and indistinguishable anonymous trades are deliberately retained.
"""
import json
import math
import re
import sqlite3
import threading
import time
import uuid
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from pathlib import Path


ARCHIVE_VERSION = "microstructure_v1"
MAX_RECORDS = 100_000
MAX_BYTES = 100_000_000
BOOK_INTERVAL = timedelta(seconds=1)
TICKER_RE = re.compile(r"[A-Z0-9][A-Z0-9.\-]{0,14}\Z")


class ArchiveLimit(Exception):
    """Stop collecting; preserve all previously committed records."""


def validate_options(tickers, seconds):
    if isinstance(seconds, bool) or not isinstance(seconds, int) or not 1 <= seconds <= 3600:
        raise ValueError("Продолжительность должна быть целым числом от 1 до 3600 секунд.")
    if not isinstance(tickers, (list, tuple)) or not 1 <= len(tickers) <= 3:
        raise ValueError("Укажите от 1 до 3 тикеров акций TQBR.")
    symbols = []
    for ticker in tickers:
        if not isinstance(ticker, str) or not TICKER_RE.fullmatch(ticker.upper()):
            raise ValueError("Тикеры должны содержать только латинские буквы, цифры, точку или дефис.")
        symbol = ticker.upper()
        if symbol not in symbols:
            symbols.append(symbol)
    return symbols, seconds


def utc_timestamp(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if (not isinstance(value, datetime) or value.tzinfo is None
            or value.utcoffset() is None):
        raise ValueError("An aware timestamp is required")
    return value.astimezone(timezone.utc)


def stamp(value):
    return utc_timestamp(value).isoformat(timespec="microseconds")


def enum_name(value):
    name = getattr(value, "name", value)
    return name if isinstance(name, str) and re.fullmatch(r"[A-Z0-9_]{1,80}", name) else "UNKNOWN"


def optional_text(obj, names):
    for name in names:
        value = getattr(obj, name, None)
        if isinstance(value, str) and 0 < len(value) <= 128:
            return value
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
    return None


def price_number(value):
    if hasattr(value, "units") and hasattr(value, "nano"):
        value = value.units + value.nano / 1_000_000_000
    if (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value > 0):
        return float(value)
    return None


def lot_quantity(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 < value < 2**63 else None


class MicrostructureArchive:
    """Append-only market records with SQLite-enforced disk/row budgets.

    Received times are local UTC; exchange times are separate evidence. Quality
    flags prevent later consumers from silently treating bad clocks/order or
    inconsistent books as usable causal observations.
    """
    def __init__(self, path, *, max_records=MAX_RECORDS, max_bytes=MAX_BYTES):
        if (isinstance(max_records, bool) or not isinstance(max_records, int)
                or not 1 <= max_records <= MAX_RECORDS):
            raise ValueError("Invalid row budget")
        if (isinstance(max_bytes, bool) or not isinstance(max_bytes, int)
                or not 16_384 <= max_bytes <= MAX_BYTES):
            raise ValueError("Invalid disk budget")
        self.path = Path(path).resolve()
        self.max_records, self.max_bytes = max_records, max_bytes
        if self.path.exists() and self.path.stat().st_size > max_bytes:
            raise ArchiveLimit("Архив уже превышает лимит размера; сбор не начат, файл сохранён.")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=2)
        self.run_id = None
        self.received_sequence = 0
        self.last_received = None
        try:
            tables = {row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables - {"archive_meta", "collection_runs", "market_events", "sqlite_sequence"}:
                raise ValueError("Файл не является архивом микроструктуры; сбор не начат.")
            if "archive_meta" in tables:
                version = self.db.execute("SELECT value FROM archive_meta WHERE key='version'").fetchone()
                if version is not None and version[0] != ARCHIVE_VERSION:
                    raise ValueError("Неизвестная версия архива; сбор не начат.")
            self.db.execute("PRAGMA journal_mode=DELETE")
            self.db.execute("PRAGMA synchronous=FULL")
            page_size = self.db.execute("PRAGMA page_size").fetchone()[0]
            if self.db.execute("PRAGMA page_count").fetchone()[0] > max_bytes // page_size:
                raise ArchiveLimit("Архив достиг лимита страниц; сбор не начат.")
            self.db.execute(f"PRAGMA max_page_count={max_bytes // page_size}")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS archive_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS collection_runs (
                    id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
                    requested_seconds INTEGER NOT NULL, tickers TEXT NOT NULL,
                    status TEXT NOT NULL, summary TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS market_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                    kind TEXT NOT NULL, ticker TEXT NOT NULL, instrument_uid TEXT NOT NULL,
                    received_at TEXT NOT NULL, exchange_at TEXT, received_sequence INTEGER NOT NULL,
                    exchange_sequence TEXT, is_consistent INTEGER, source TEXT NOT NULL,
                    trade_id TEXT, exchange_date TEXT, causal_eligible INTEGER NOT NULL,
                    quality_flags TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS market_event_clock
                    ON market_events(instrument_uid, kind, exchange_at);
                CREATE INDEX IF NOT EXISTS market_event_received
                    ON market_events(instrument_uid, kind, id);
                CREATE UNIQUE INDEX IF NOT EXISTS known_trade_identity
                    ON market_events(instrument_uid, source, exchange_date, trade_id)
                    WHERE kind='trade' AND trade_id IS NOT NULL AND exchange_date IS NOT NULL;
            """)
            version = self.db.execute("SELECT value FROM archive_meta WHERE key='version'").fetchone()
            if version is not None and version[0] != ARCHIVE_VERSION:
                raise ValueError("Неизвестная версия архива; сбор не начат.")
            with self.db:
                self.db.execute("INSERT OR IGNORE INTO archive_meta VALUES ('version',?)", (ARCHIVE_VERSION,))
                count = self.db.execute("SELECT COUNT(*) FROM market_events").fetchone()[0]
                self.db.execute("INSERT OR REPLACE INTO archive_meta VALUES ('event_count',?)", (str(count),))
            if count >= max_records:
                raise ArchiveLimit("Архив достиг лимита 100000 рыночных записей; файл сохранён.")
            prior_received = self.db.execute("SELECT MAX(received_at) FROM market_events").fetchone()[0]
            self.last_received = utc_timestamp(prior_received) if prior_received is not None else None
        except sqlite3.DatabaseError as exc:
            self.db.close()
            if getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_FULL:
                raise ArchiveLimit("SQLite остановил запись по лимиту размера; файл сохранён.") from None
            raise
        except Exception:
            self.db.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.db.close()

    @property
    def count(self):
        return int(self.db.execute("SELECT value FROM archive_meta WHERE key='event_count'").fetchone()[0])

    def begin_run(self, tickers, seconds, received_at):
        symbols, seconds = validate_options(tickers, seconds)
        self.run_id = uuid.uuid4().hex
        try:
            with self.db:
                self.db.execute("INSERT INTO collection_runs VALUES (?,?,NULL,?,?,?,?)",
                                (self.run_id, stamp(received_at), seconds, json.dumps(symbols), "running", "{}"))
        except sqlite3.DatabaseError as exc:
            if getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_FULL:
                raise ArchiveLimit("SQLite достиг лимита размера; новый сеанс не начат.") from None
            raise
        return self.run_id

    def finish_run(self, status, stats, received_at):
        if status not in {"deadline", "once", "interrupted", "limit", "error", "stream_ended", "subscription_rejected"}:
            raise ValueError("Invalid run status")
        if self.run_id is not None:
            try:
                with self.db:
                    self.db.execute("UPDATE collection_runs SET finished_at=?, status=?, summary=? WHERE id=?",
                                    (stamp(received_at), status, json.dumps(stats, sort_keys=True), self.run_id))
            except sqlite3.DatabaseError as exc:
                if getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_FULL:
                    raise ArchiveLimit("Лимит размера: записи сохранены, сводка сеанса не дописана.") from None
                raise

    def append(self, kind, item, instrument, received_at):
        if kind not in {"book", "trade"} or self.run_id is None:
            raise ValueError("Begin a run before appending known event kinds")
        received = utc_timestamp(received_at)
        received_text = stamp(received)
        self.received_sequence += 1
        flags = []
        if self.last_received is not None and received < self.last_received:
            flags.append("local_clock_regressed")
        self.last_received = max(self.last_received, received) if self.last_received is not None else received
        exchange = None
        try:
            exchange = utc_timestamp(getattr(item, "time", None))
        except (TypeError, ValueError):
            flags.append("exchange_timestamp_missing_or_invalid")
        if exchange is not None and exchange > received:
            flags.append("exchange_timestamp_future")
        exchange_text = stamp(exchange) if exchange is not None else None
        uid, ticker = instrument["uid"], instrument["ticker"]
        sequence = optional_text(item, ("sequence", "sequence_number", "seq"))
        if sequence is None:
            flags.append("exchange_sequence_unavailable")
        trade_id = None
        consistent = None
        if kind == "book":
            source = enum_name(getattr(item, "order_book_type", None))
            raw_consistent = getattr(item, "is_consistent", None)
            consistent = int(raw_consistent) if isinstance(raw_consistent, bool) else None
            if consistent != 1:
                flags.append("book_inconsistent" if consistent == 0 else "book_consistency_unavailable")
            levels = {}
            for side in ("bids", "asks"):
                raw = getattr(item, side, None)
                if not isinstance(raw, (list, tuple)):
                    raw = []
                levels[side] = [[price_number(getattr(level, "price", None)),
                                 lot_quantity(getattr(level, "quantity", None))] for level in raw[:20]]
                if not levels[side] or any(None in row for row in levels[side]):
                    flags.append("book_levels_invalid")
            if "book_levels_invalid" not in flags:
                bids, asks = levels["bids"], levels["asks"]
                if (asks[0][0] < bids[0][0]
                        or any(a[0] <= b[0] for a, b in zip(bids, bids[1:]))
                        or any(a[0] >= b[0] for a, b in zip(asks, asks[1:]))):
                    flags.append("book_levels_unsorted_or_crossed")
            if source != "ORDERBOOK_TYPE_EXCHANGE":
                flags.append("source_not_confirmed_exchange")
            payload = {**levels, "depth_requested": 20,
                       "depth_reported": getattr(item, "depth", None) if isinstance(getattr(item, "depth", None), int) else None,
                       "quantity_unit": "lots", "price_unit": "per_share"}
        else:
            source = enum_name(getattr(item, "trade_source", None))
            trade_id = optional_text(item, ("trade_id",))
            if trade_id is None:
                flags.append("trade_id_unavailable_no_deduplication")
            direction = enum_name(getattr(item, "direction", None))
            price = price_number(getattr(item, "price", None))
            quantity = lot_quantity(getattr(item, "quantity", None))
            if direction not in {"TRADE_DIRECTION_BUY", "TRADE_DIRECTION_SELL"}:
                flags.append("trade_direction_unknown")
            if price is None or quantity is None:
                flags.append("trade_value_invalid")
            if source != "TRADE_SOURCE_EXCHANGE":
                flags.append("source_not_confirmed_exchange")
            payload = {"direction": direction, "price": price, "quantity": quantity,
                       "quantity_unit": "lots", "price_unit": "per_share", "trade_id": trade_id}
        payload["lot_size"] = lot_quantity(instrument.get("lot"))
        harmless = {"exchange_sequence_unavailable", "trade_id_unavailable_no_deduplication"}
        try:
            self.db.execute("BEGIN IMMEDIATE")
            if self.count >= self.max_records:
                raise ArchiveLimit("Достигнут лимит рыночных записей; сбор остановлен, архив сохранён.")
            if kind == "book":
                previous = self.db.execute("SELECT received_at FROM market_events WHERE kind='book' AND instrument_uid=? ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
                if previous is not None and received - utc_timestamp(previous[0]) < BOOK_INTERVAL:
                    self.db.rollback()
                    return "throttled"
            prior = self.db.execute("""SELECT MAX(exchange_at) FROM market_events
                WHERE kind=? AND instrument_uid=? AND exchange_at<=received_at""", (kind, uid)).fetchone()[0]
            if exchange is not None and prior is not None:
                if exchange < utc_timestamp(prior):
                    flags.append("exchange_timestamp_out_of_order")
                elif kind == "book" and exchange == utc_timestamp(prior):
                    flags.append("book_timestamp_repeated")
            flags = sorted(set(flags))
            causal_eligible = not any(flag not in harmless for flag in flags)
            inserted = self.db.execute("""INSERT OR IGNORE INTO market_events
                (run_id,kind,ticker,instrument_uid,received_at,exchange_at,received_sequence,
                 exchange_sequence,is_consistent,source,trade_id,exchange_date,causal_eligible,quality_flags,payload)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (self.run_id, kind, ticker, uid, received_text, exchange_text, self.received_sequence,
                 sequence, consistent, source, trade_id, exchange.date().isoformat() if exchange else None,
                 int(causal_eligible), json.dumps(flags), json.dumps(payload, allow_nan=False)))
            if inserted.rowcount:
                self.db.execute("UPDATE archive_meta SET value=CAST(value AS INTEGER)+1 WHERE key='event_count'")
            self.db.commit()
            return "stored" if inserted.rowcount else "duplicate_id"
        except sqlite3.DatabaseError as exc:
            self.db.rollback()
            if getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_FULL:
                raise ArchiveLimit("SQLite достиг лимита размера; предыдущие записи сохранены.") from None
            raise
        except Exception:
            self.db.rollback()
            raise


class StreamControl:
    """A monotonic whole-operation deadline and explicit gRPC cancellation."""
    def __init__(self, seconds, clock=time.monotonic):
        self.clock = clock
        self.deadline = clock() + seconds
        self.stop = threading.Event()
        self.call = None

    @property
    def remaining(self):
        return max(0.0, self.deadline - self.clock())

    def cancel(self):
        self.stop.set()
        if self.call is not None:
            try:
                self.call.cancel()
            except Exception:
                # The RPC deadline and Client context are independent fallbacks.
                # Never expose a raw transport exception or mask the run result.
                pass

    def details(self, original, maximum=None):
        Details = namedtuple("Details", "method timeout metadata credentials wait_for_ready compression")
        budget = min(self.remaining, maximum) if maximum is not None else self.remaining
        existing = getattr(original, "timeout", None)
        timeout = max(.001, min(budget, existing) if existing is not None else budget)
        return Details(original.method, timeout, original.metadata, original.credentials,
                       False, getattr(original, "compression", None))


def bounded_client(token, control):
    """Use the installed SDK, adding the timeout its stream method lacks."""
    import grpc
    from t_tech.invest import Client

    class Deadline(grpc.UnaryUnaryClientInterceptor, grpc.StreamStreamClientInterceptor):
        def intercept_unary_unary(self, continuation, details, request):
            return continuation(control.details(details, 25), request)

        def intercept_stream_stream(self, continuation, details, requests):
            call = continuation(control.details(details), requests)
            control.call = call
            if control.stop.is_set():
                call.cancel()
            return call

    return Client(token, interceptors=[Deadline()], app_name="vorby-pro-microstructure-readonly")


def subscription_requests(instruments, control):
    from t_tech.invest.schemas import (MarketDataRequest, OrderBookInstrument, OrderBookType,
                                     SubscribeOrderBookRequest, SubscribeTradesRequest,
                                     SubscriptionAction, TradeInstrument, TradeSourceType)
    yield MarketDataRequest(subscribe_order_book_request=SubscribeOrderBookRequest(
        subscription_action=SubscriptionAction.SUBSCRIPTION_ACTION_SUBSCRIBE,
        instruments=[OrderBookInstrument(instrument_id=row["uid"], depth=20,
                                       order_book_type=OrderBookType.ORDERBOOK_TYPE_EXCHANGE)
                     for row in instruments]))
    yield MarketDataRequest(subscribe_trades_request=SubscribeTradesRequest(
        subscription_action=SubscriptionAction.SUBSCRIPTION_ACTION_SUBSCRIBE,
        instruments=[TradeInstrument(instrument_id=row["uid"]) for row in instruments],
        trade_source=TradeSourceType.TRADE_SOURCE_EXCHANGE))
    # Keep the request side open without an unbounded sleep or busy loop.
    while control.remaining > 0 and not control.stop.wait(min(.25, control.remaining)):
        pass
