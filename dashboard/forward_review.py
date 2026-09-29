"""Forward-only review of real, locally archived signal observations."""
import json
import math
import sqlite3
from bisect import bisect_left
from datetime import datetime, timedelta
from pathlib import Path

from django.conf import settings

from .signals import MSK

HORIZON = timedelta(hours=1)
MAX_DELAY = timedelta(minutes=10)
GROUPS = (("0–49", 0, 50), ("50–79", 50, 80), ("80–99", 80, 100), ("100", 100, 101))


def _prices(payload):
    book = payload.get("book") or {}
    bids, asks = book.get("bids") or [], book.get("asks") or []
    if not bids or not asks:
        return None
    bid, ask = bids[0][0], asks[0][0]
    if (not isinstance(bid, (int, float)) or not isinstance(ask, (int, float))
            or not math.isfinite(bid) or not math.isfinite(ask) or bid <= 0 or ask < bid):
        return None
    return bid, ask


def review_observations(rows, fee_bps=5, slippage_bps=5):
    """One entry per ticker/hour; use only a later observed quote, never future-filled candles."""
    valid = []
    for at, raw_payload in rows:
        try:
            payload = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
            prices = _prices(payload)
            timestamp = datetime.fromisoformat(at)
            if payload.get("review_ready") is True and prices and timestamp.tzinfo:
                valid.append((timestamp, payload, prices))
        except (TypeError, ValueError, KeyError, IndexError):
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
        at, payload, (bid, ask) = sample
        index = bisect_left(times, at + HORIZON)
        if index >= len(valid) or valid[index][0] > at + HORIZON + MAX_DELAY:
            return None
        later_at, _, (later_bid, later_ask) = valid[index]
        entry_cost = ask * (1 + slip) * (1 + fee)
        exit_value = later_bid * (1 - slip) * (1 - fee)
        return {"at": at.isoformat(), "later_at": later_at.isoformat(),
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
    return {"status": "ok", "observations": len(valid), "sampled_hours": len(hourly),
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
        return review_observations([], fee_bps, slippage_bps)
    try:
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2) as db:
            compact = db.execute("""
                SELECT at,
                       json_extract(payload, '$.decision'),
                       json_extract(payload, '$.score'),
                       json_extract(payload, '$.book.bids[0][0]'),
                       json_extract(payload, '$.book.asks[0][0]')
                  FROM observations
                 WHERE ticker=? AND json_extract(payload, '$.review_ready')=1
                 ORDER BY at
            """, (ticker,)).fetchall()
    except sqlite3.Error:
        return {"status": "error", "message": "Не удалось прочитать локальную историю наблюдений."}
    rows = [(at, {"review_ready": True, "decision": decision, "score": score,
                  "book": {"bids": [[bid, 1]], "asks": [[ask, 1]]}})
            for at, decision, score, bid, ask in compact]
    return review_observations(rows, fee_bps, slippage_bps)
