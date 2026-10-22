"""Bounded receiver-only latency experiment, not a trading data source.

No disk writes while receiving. Public quote fingerprints match identical
observed books; unpaired records are not used to infer a causal improvement.
"""
import hashlib
import json
import math
from collections import Counter
from datetime import datetime, timezone

from .microstructure_archive import enum_name, price_number, utc_timestamp, stamp


VERSION = "receiver_latency_probe_v1"
MAX_OBSERVATIONS = 20000


def fingerprint(bids, asks):
    return hashlib.sha256(json.dumps({"bids": bids, "asks": asks}, sort_keys=True,
                                    allow_nan=False).encode("utf-8")).hexdigest()


def statistics(values):
    values = sorted(values)
    n = len(values)
    quantile = lambda p: values[min(n-1, max(0, math.ceil(p*n)-1))] if n else None
    return {"count": n, "median_seconds": quantile(.5), "p95_seconds": quantile(.95),
            "max_seconds": max(values) if n else None, "over_5s": sum(v > 5 for v in values),
            "negative_clock_difference": sum(v < 0 for v in values)}


class ReceiverProbe:
    def __init__(self, instruments):
        self.known = {r["uid"]: r["ticker"] for r in instruments}
        self.observations = []
        self.counts = Counter()

    def observe(self, message, received):
        if len(self.observations) >= MAX_OBSERVATIONS:
            return False
        for kind, field in (("book", "orderbook"), ("trade", "trade")):
            item = getattr(message, field, None)
            uid = getattr(item, "instrument_uid", None)
            if uid not in self.known:
                continue
            self.counts[kind] += 1
            try:
                exchange = utc_timestamp(getattr(item, "time", None))
                lag = (utc_timestamp(received)-exchange).total_seconds()
                record = {"ticker": self.known[uid], "kind": kind, "received_at": stamp(received),
                          "exchange_at": stamp(exchange), "latency_seconds": lag}
                if kind == "book":
                    record["source"] = enum_name(getattr(item, "order_book_type", None))
                    record["is_consistent"] = getattr(item, "is_consistent", None) is True
                    levels = {side: [[price_number(level.price), getattr(level, "quantity", None)]
                                     for level in getattr(item, side, [])[:20]] for side in ("bids", "asks")}
                    record["fingerprint"] = fingerprint(**levels)
                else:
                    record["source"] = enum_name(getattr(item, "trade_source", None))
                self.observations.append(record)
            except (ValueError, TypeError, AttributeError):
                self.counts["invalid_clock_or_payload"] += 1
        return len(self.observations) < MAX_OBSERVATIONS

    def summary(self):
        return {"received": dict(self.counts), "latencies": {
            kind: statistics([r["latency_seconds"] for r in self.observations if r["kind"] == kind])
            for kind in ("book", "trade")}}


def compare(probe, archived):
    direct = {}
    for r in probe:
        if r["kind"] == "book" and r.get("is_consistent") is True and r.get("source") == "ORDERBOOK_TYPE_EXCHANGE":
            key = (r["ticker"], utc_timestamp(r["exchange_at"]), r["fingerprint"])
            # Retain first receipt; duplicate quote packets are not independent.
            direct.setdefault(key, r)
    paired, used = [], set()
    for r in archived:
        if r["kind"] != "book" or r["source"] != "ORDERBOOK_TYPE_EXCHANGE" or r["is_consistent"] != 1:
            continue
        try:
            p = json.loads(r["payload"]) if isinstance(r["payload"], str) else r["payload"]
            key = (r["ticker"], utc_timestamp(r["exchange_at"]), fingerprint(p["bids"], p["asks"]))
            if key in used or key not in direct:
                continue
            used.add(key)
            lag = (utc_timestamp(r["received_at"])-key[1]).total_seconds()
            paired.append({"ticker": r["ticker"], "exchange_at": stamp(key[1]),
                           "probe_seconds": direct[key]["latency_seconds"], "archive_seconds": lag,
                           "archive_minus_probe_seconds": lag-direct[key]["latency_seconds"]})
        except (ValueError, TypeError, KeyError):
            continue
    return {"matched_books": len(paired),
            "probe": statistics([r["probe_seconds"] for r in paired]),
            "archive": statistics([r["archive_seconds"] for r in paired]),
            "archive_minus_probe": statistics([r["archive_minus_probe_seconds"] for r in paired]),
            "pairs": paired,
            "note": "Same source timestamp and levels; concurrent receivers, not randomised runs. Difference does not isolate disk, queue, network or server."}
