"""Read committed EXCHANGE observations only; never bridge a bad book silently."""
import json
import math
import sqlite3
from contextlib import closing
from datetime import timedelta
from pathlib import Path

from .continuous_watch import load_health
from .market_provider import MarketUnavailable
from .microstructure_archive import stamp, utc_timestamp


def read_rows(path, table, query, args=()):
    if not path.is_file() or path.stat().st_size > 25_000_000:
        raise ValueError("Invalid stream file")
    with closing(sqlite3.connect(path.resolve().as_uri()+"?mode=ro", uri=True, timeout=.5)) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(query, args)]


def valid_levels(payload):
    bids, asks = payload.get("bids"), payload.get("asks")
    def valid(side):
        return (isinstance(side, list) and 1 <= len(side) <= 20 and all(
            isinstance(row, list) and len(row) == 2 and type(row[0]) in (int, float)
            and math.isfinite(row[0]) and row[0] > 0 and type(row[1]) is int and row[1] > 0
            for row in side))
    return bool(valid(bids) and valid(asks) and bids[0][0] <= asks[0][0]
                and all(a[0] > b[0] for a, b in zip(bids, bids[1:]))
                and all(a[0] < b[0] for a, b in zip(asks, asks[1:])))


def book_usable(row, now, max_age=45):
    try:
        received, exchange = utc_timestamp(row["received_at"]), utc_timestamp(row["exchange_at"])
        payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
        return bool(row["causal_eligible"] == 1 and row["is_consistent"] == 1
                    and row["source"] == "ORDERBOOK_TYPE_EXCHANGE"
                    and 0 <= (now-received).total_seconds() <= max_age
                    and 0 <= (received-exchange).total_seconds() <= 5 and valid_levels(payload))
    except (ValueError, TypeError, KeyError, AttributeError):
        return False


def stream_window(root, ticker, instrument, now):
    root = Path(root).resolve()
    health = load_health(root, now)
    if not health.get("alive") or ticker not in health.get("tickers", []):
        raise MarketUnavailable("Нет работающего потокового наблюдения этой акции. Непроверенный стакан не используется для входа.")
    paths = sorted(root.glob("*/health.sqlite3"), key=lambda p: p.stat().st_mtime_ns, reverse=True)
    session = paths[0].parent.resolve()
    if not session.is_relative_to(root):
        raise ValueError("Stream path escapes archive")
    books, trades, infos, flagged_trades, boundaries = [], [], [], 0, []
    files = sorted(session.glob("*-book.sqlite3"), reverse=True)[:4]
    for path in reversed(files):
        if not path.resolve().is_relative_to(session):
            raise ValueError("Invalid segment path")
        rows = read_rows(path, "market_events", """SELECT * FROM market_events WHERE ticker=?
            AND received_at>=? AND received_at<=? ORDER BY id LIMIT 20001""",
            (ticker, stamp(now-timedelta(seconds=320)), stamp(now)))
        if len(rows) > 20000:
            raise MarketUnavailable("Поток слишком плотный для текущего лимита чтения; неполная лента не оценивается.")
        per_file_books = []
        for row in rows:
            if row["instrument_uid"] != instrument["uid"]:
                raise ValueError("Ticker identity changed")
            row["payload"] = json.loads(row["payload"])
            if row["payload"].get("lot_size") != instrument["lot"]:
                raise ValueError("Lot identity changed")
            if row["kind"] == "book":
                books.append(row)
                per_file_books.append(row)
            elif row["kind"] == "trade":
                try:
                    exchange = utc_timestamp(row["exchange_at"])
                    latency = (utc_timestamp(row["received_at"])-exchange).total_seconds()
                except (ValueError, TypeError):
                    flagged_trades += 1
                    continue
                if not (now-timedelta(minutes=5) <= exchange <= now):
                    continue
                if (row["causal_eligible"] != 1 or row["source"] != "TRADE_SOURCE_EXCHANGE"
                        or not 0 <= latency <= 5):
                    flagged_trades += 1
                    continue
                p = row["payload"]
                if (p["direction"] not in ("TRADE_DIRECTION_BUY", "TRADE_DIRECTION_SELL")
                        or type(p["quantity"]) is not int or p["quantity"] <= 0
                        or type(p["price"]) not in (int, float) or not math.isfinite(p["price"]) or p["price"] <= 0):
                    flagged_trades += 1
                    continue
                trades.append({"time": utc_timestamp(row["exchange_at"]), "quantity": p["quantity"],
                               "price": p["price"], "side": "buy" if p["direction"] == "TRADE_DIRECTION_BUY" else "sell"})
        if per_file_books:
            boundaries.append(path.name)
        chart = path.with_name(path.name.replace("-book.sqlite3", "-chart.sqlite3"))
        if not chart.resolve().is_relative_to(session):
            raise ValueError("Invalid chart path")
        infos.extend(read_rows(chart, "events", """SELECT * FROM events WHERE ticker=? AND kind='info'
            AND received_at<=? ORDER BY id DESC LIMIT 1""", (ticker, stamp(now))))
    books.sort(key=lambda r: (r["received_at"], r["id"]))
    if not books or not book_usable(books[-1], now):
        raise MarketUnavailable("Последний потоковый стакан устарел, несогласован или имеет непроверенный источник. Оценка входа заблокирована.")
    if not infos:
        raise MarketUnavailable("Нет подтверждённого потокового статуса торгов.")
    info = max(infos, key=lambda r: r["received_at"])
    if info["causal_eligible"] != 1 or not 0 <= (now-utc_timestamp(info["received_at"])).total_seconds() <= 360:
        raise MarketUnavailable("Потоковый статус торгов устарел или непроверен.")
    state = json.loads(info["payload"])
    recent_books = [r for r in books if utc_timestamp(r["received_at"]) >= now-timedelta(seconds=120)]
    gaps = [(utc_timestamp(b["received_at"])-utc_timestamp(a["received_at"])).total_seconds()
            for a, b in zip(books, books[1:])
            if utc_timestamp(b["received_at"]) >= now-timedelta(minutes=5)]
    clean = [r for r in recent_books if book_usable(r, utc_timestamp(r["received_at"]), max_age=0)]
    converted = [{"time": utc_timestamp(r["exchange_at"]), "received_at": utc_timestamp(r["received_at"]),
                  "is_consistent": True, "source": r["source"],
                  "bids": r["payload"]["bids"], "asks": r["payload"]["asks"]} for r in clean]
    quality = {"version": "stream_exchange_v1", "book_consistency_confirmed": True,
               "observed_only": True, "segments_in_window": len(boundaries), "flagged_trades": flagged_trades,
               "max_book_observation_gap_seconds": max(gaps, default=0),
               "warning": "Снимки и обезличенные сделки, не полный журнал заявок. Малые пропуски и потеря сообщений без sequence не исключены."}
    # Refuse to hide a corrupt or out-of-order book between valid observations.
    quality["action_blocked"] = bool(flagged_trades or max(gaps, default=0) > 5
                                    or len(clean) != len(recent_books)
                                    or any(b["time"] <= a["time"] for a, b in zip(converted, converted[1:])))
    quality["blocked_reason"] = "В недавнем потоке есть пропуск более 5 секунд, плохой стакан или непроверенные сделки; вход и выход по алгоритму заблокированы." if quality["action_blocked"] else None
    return {"books": converted, "trades": sorted(trades, key=lambda t: t["time"]),
            "trading": state["status"] == "SECURITY_TRADING_STATUS_NORMAL_TRADING" and state.get("market_orders_available") is True,
            "trading_status": state["status"], "data_quality": quality}
