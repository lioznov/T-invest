"""Forward-only review of real, locally archived signal observations."""
import json
import math
import sqlite3
from bisect import bisect_left
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

from django.conf import settings

from .signals import MSK

HORIZON = timedelta(hours=1)
MAX_DELAY = timedelta(minutes=10)
MAX_BOOK_AGE = timedelta(seconds=45)
STRATEGY_VERSION = "six_rules_v1"
GROUPS = (("0–49", 0, 50), ("50–79", 50, 80), ("80–99", 80, 100), ("100", 100, 101))


def _prices(payload):
    book = payload.get("book") or {}
    if not isinstance(book, dict):
        return None
    bids, asks = book.get("bids") or [], book.get("asks") or []
    if not bids or not asks:
        return None
    bid, ask = bids[0][0], asks[0][0]
    if (not isinstance(bid, (int, float)) or not isinstance(ask, (int, float))
            or isinstance(bid, bool) or isinstance(ask, bool)
            or not math.isfinite(bid) or not math.isfinite(ask) or bid <= 0 or ask < bid):
        return None
    return bid, ask


def review_observations(rows, fee_bps=5, slippage_bps=5, provider_version=None):
    """Review only the current registered rules and fresh, timestamped quotes.

    A later *recording* is not sufficient: its actual book timestamp must reach
    the one-hour horizon. Legacy decisions cannot validate the current rules.
    """
    valid = []
    excluded_legacy = 0
    excluded_quotes = 0
    for at, raw_payload in rows:
        try:
            payload = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
            if not isinstance(payload, dict):
                continue
            if (payload.get("strategy_version") != STRATEGY_VERSION
                    or (provider_version and payload.get("provider_version") != provider_version)):
                excluded_legacy += 1
                continue
            prices = _prices(payload)
            timestamp = datetime.fromisoformat(at)
            if payload.get("review_ready") is not True or payload.get("data_quality", {}).get("action_blocked"):
                continue
            book_at = datetime.fromisoformat(payload["book"]["time"])
            if (prices and timestamp.tzinfo and timestamp.utcoffset() is not None
                    and book_at.tzinfo and book_at.utcoffset() is not None
                    and timedelta(0) <= timestamp - book_at <= MAX_BOOK_AGE):
                valid.append((timestamp, payload, prices, book_at))
            else:
                excluded_quotes += 1
        except (TypeError, ValueError, KeyError, IndexError):
            excluded_quotes += 1
            continue
    valid.sort(key=lambda row: row[0])
    times = [row[0] for row in valid]
    hourly = []
    events = []
    seen_hours = set()
    previous_decision = None
    previous_at = None
    for row in valid:
        hour = row[0].astimezone(MSK).replace(minute=0, second=0, microsecond=0)
        if hour not in seen_hours:
            seen_hours.add(hour)
            hourly.append(row)
        decision = row[1].get("decision")
        if decision in ("LONG", "EXIT") and (decision != previous_decision or
                                             previous_at is None or row[0] - previous_at > MAX_DELAY):
            events.append(row)
        previous_decision, previous_at = decision, row[0]

    fee, slip = fee_bps / 10000, slippage_bps / 10000
    def pair(sample):
        at, payload, (bid, ask), book_at = sample
        horizon = at + HORIZON
        index = bisect_left(times, horizon)
        while index < len(valid) and valid[index][0] <= horizon + MAX_DELAY:
            later_at, _, (later_bid, later_ask), later_book_at = valid[index]
            if later_book_at >= horizon and later_book_at > book_at:
                break
            index += 1
        else:
            return None
        entry_cost = ask * (1 + slip) * (1 + fee)
        exit_value = later_bid * (1 - slip) * (1 - fee)
        return {"at": at.isoformat(), "later_at": later_at.isoformat(),
                "book_at": book_at.isoformat(), "later_book_at": later_book_at.isoformat(),
                "decision": payload.get("decision"), "score": payload.get("score"),
                "net_buy_pct": (exit_value / entry_cost - 1) * 100,
                "price_change_pct": (((later_bid + later_ask) / (bid + ask)) - 1) * 100}

    outcomes = [result for sample in hourly if (result := pair(sample)) is not None]
    event_outcomes = [result for sample in events if (result := pair(sample)) is not None]

    def summary(items, metric, success):
        values = [row[metric] for row in items]
        return {"samples": len(values),
                "average_pct": sum(values) / len(values) if values else None,
                "positive_or_correct": sum(success(value) for value in values) if values else 0}

    bands = []
    for name, low, high in GROUPS:
        items = [row for row in outcomes if isinstance(row["score"], int) and low <= row["score"] < high]
        bands.append({"name": name, **summary(items, "net_buy_pct", lambda value: value > 0)})
    candidates = [row for row in event_outcomes if row["decision"] == "LONG"]
    exits = [row for row in event_outcomes if row["decision"] == "EXIT"]
    return {"status": "ok", "strategy_version": STRATEGY_VERSION, "provider_version": provider_version,
            "excluded_legacy_observations": excluded_legacy,
            "excluded_invalid_book_observations": excluded_quotes,
            "observations": len(valid), "sampled_hours": len(hourly),
            "completed": len(outcomes), "without_pair": len(hourly) - len(outcomes),
            "candidate_events": sum(row[1].get("decision") == "LONG" for row in events),
            "exit_events": sum(row[1].get("decision") == "EXIT" for row in events),
            "horizon_minutes": 60, "fee_bps": fee_bps, "slippage_bps": slippage_bps,
            "bands": bands,
            "buy_candidates": summary(candidates, "net_buy_pct", lambda value: value > 0),
            "exit_warnings": summary(exits, "price_change_pct", lambda value: value < 0),
            "first_at": valid[0][0].isoformat() if valid else None,
            "last_at": valid[-1][0].isoformat() if valid else None}


def load_review(ticker, fee_bps=5, slippage_bps=5):
    path = Path(settings.BASE_DIR) / "data" / "market.sqlite3"
    if not path.is_file():
        return review_observations([], fee_bps, slippage_bps, provider_version="stream_exchange_v1")
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
            compact = db.execute("""
                SELECT at,
                       json_extract(payload, '$.strategy_version'),
                       json_extract(payload, '$.review_ready'),
                       json_extract(payload, '$.decision'),
                       json_extract(payload, '$.score'),
                       json_extract(payload, '$.book.time'),
                       json_extract(payload, '$.book.bids[0][0]'),
                       json_extract(payload, '$.book.asks[0][0]'),
                       json_extract(payload, '$.provider_version'),
                       json_extract(payload, '$.data_quality.action_blocked')
                  FROM observations
                 WHERE ticker=?
                 ORDER BY at
            """, (ticker,)).fetchall()
    except sqlite3.Error:
        return {"status": "error", "message": "Не удалось прочитать локальную историю наблюдений."}
    rows = [(at, {"strategy_version": version, "review_ready": ready == 1,
                  "decision": decision, "score": score, "provider_version": provider,
                  "data_quality": {"action_blocked": blocked == 1},
                  "book": {"time": book_at, "bids": [[bid, 1]], "asks": [[ask, 1]]}})
            for at, version, ready, decision, score, book_at, bid, ask, provider, blocked in compact]
    return review_observations(rows, fee_bps, slippage_bps, provider_version="stream_exchange_v1")
