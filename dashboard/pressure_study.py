"""Four fixed exploratory LONG hypotheses on observed, segmented streams.

Quote replay is not a broker execution simulation or a profitability guarantee.
Signals see received-time history only; bad/absent future quotes are unverified.
"""
from collections import deque
from datetime import timedelta
import math

from .microstructure_archive import utc_timestamp
from .stream_inputs import book_usable


VERSION = "pressure_stream_v1"
VARIANTS = {f"{family}_{horizon}s": {"family": family, "horizon_seconds": horizon}
            for family in ("persistent_bid", "positive_ofi") for horizon in (60, 180)}
RULES = {"previous_trials": 65, "spread_bps_max": 10, "warmup_seconds": 30,
         "book_window_seconds": 20, "min_books": 10, "max_gap_seconds": 5,
         "buy_window_seconds": 30, "min_trades": 5, "buy_fraction_min": .60,
         "bid_fraction_min": .65, "bid_persistence_seconds": 10,
         "near_bid_retention_min": .80, "ofi_normalised_min": .50,
         "entry_delay_seconds": 1, "entry_deadline_seconds": 5,
         "exit_wait_max_seconds": 10, "cooldown_extra_seconds": 15,
         "fee_bps_each_side": 5, "slippage_bps_each_side": 5,
         "stress_multiplier": 2, "max_segments": 32,
         "min_distinct_days": 3, "min_verified_per_variant": 30,
         "execution": "one lot at next observed ask / later bid; top depth checked; no short, leverage, stops or orders",
         "scope": "within one segment only; observations are sampled, anonymous and potentially incomplete",
         "approval": "always false: exploratory replay, no independent holdout"}


def top(book):
    p = book["payload"]
    return p["bids"][0], p["asks"][0]


def midpoint(book):
    bid, ask = top(book)
    return (bid[0]+ask[0])/2


def ofi(previous, current):
    """Cont-style top-of-book observed change, NOT hidden trader activity."""
    (bp, bq), (ap, aq) = top(previous)
    (bn, bqn), (an, aqn) = top(current)
    return ((bqn if bn >= bp else 0)-(bq if bn <= bp else 0)
            -(aqn if an <= ap else 0)+(aq if an >= ap else 0))


def triggers(books, trades, at):
    books = list(books)
    if len(books) < RULES["min_books"] or (at-books[0]["at"]).total_seconds() < 18:
        return set()
    if any((b["at"]-a["at"]).total_seconds() > 5 for a, b in zip(books, books[1:])):
        return set()
    if any(b["exchange_at"] <= a["exchange_at"] for a, b in zip(books, books[1:])):
        return set()
    if len(trades) < 5:
        return set()
    total = sum(r["payload"]["quantity"] for r in trades)
    buys = sum(r["payload"]["quantity"] for r in trades if r["payload"]["direction"] == "TRADE_DIRECTION_BUY")
    if not total or buys/total < .60:
        return set()
    current = books[-1]
    bid, ask = top(current)
    mid = midpoint(current)
    if (ask[0]-bid[0])/mid*10000 > 10:
        return set()
    persistent = [r for r in books if (at-r["at"]).total_seconds() <= 10]
    dominance = len(persistent) >= 5 and (at-persistent[0]["at"]).total_seconds() >= 9
    dominance = dominance and all(top(r)[0][1]/(top(r)[0][1]+top(r)[1][1]) >= .65 for r in persistent)
    depths = [sum(q for p, q in r["payload"]["bids"] if mid*.998 <= p <= mid) for r in books]
    retention = bool(max(depths) and depths[-1]/max(depths) >= .80)
    average_depth = sum(top(r)[0][1]+top(r)[1][1] for r in books)/len(books)
    flow = sum(ofi(a, b) for a, b in zip(books, books[1:]))/average_depth
    hits = set()
    if dominance and retention:
        hits.add("persistent_bid")
    if flow >= .50 and mid > midpoint(books[0]):
        hits.add("positive_ofi")
    return hits


def prepare(row):
    import json
    result = dict(row)
    result["at"] = utc_timestamp(row["received_at"])
    result["exchange_at"] = utc_timestamp(row["exchange_at"]) if row.get("exchange_at") else None
    result["payload"] = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
    return result


def trade_valid(row):
    p = row["payload"]
    try:
        return bool(row["causal_eligible"] == 1 and row["source"] == "TRADE_SOURCE_EXCHANGE"
                    and 0 <= (row["at"]-row["exchange_at"]).total_seconds() <= 5
                    and p["direction"] in ("TRADE_DIRECTION_BUY", "TRADE_DIRECTION_SELL")
                    and type(p["quantity"]) is int and p["quantity"] > 0
                    and type(p["price"]) in (int, float) and math.isfinite(p["price"]) and p["price"] > 0)
    except (TypeError, ValueError, KeyError):
        return False


def execution(signal, future, horizon):
    """Never remove a bad observation to manufacture an executable path."""
    entry = None
    deadline = signal+timedelta(seconds=5)
    previous = signal
    exchange_previous = None
    for row in future:
        at = row["at"]
        if not book_usable(row, at, max_age=0) or (at-previous).total_seconds() > 5:
            return {"status": "unverified", "reason": "bad_book_or_gap"}
        if exchange_previous is not None and row["exchange_at"] <= exchange_previous:
            return {"status": "unverified", "reason": "exchange_clock_order"}
        previous, exchange_previous = at, row["exchange_at"]
        if entry is None:
            if at > deadline:
                return {"status": "unverified", "reason": "no_timely_entry"}
            if at >= signal+timedelta(seconds=1):
                entry = row
            continue
        due = entry["at"]+timedelta(seconds=horizon)
        if at < due:
            continue
        if at > due+timedelta(seconds=10):
            return {"status": "unverified", "reason": "no_timely_exit"}
        ask, bid = top(entry)[1][0], top(row)[0][0]
        def net(multiplier):
            fee, slip = .0005*multiplier, .0005*multiplier
            return (bid*(1-slip)*(1-fee)/(ask*(1+slip)*(1+fee))-1)*100
        return {"status": "verified_quote_replay", "entry_at": entry["at"].isoformat(),
                "exit_at": at.isoformat(), "entry_ask": ask, "exit_bid": bid,
                "return_pct": net(1), "stress_return_pct": net(2),
                "gross_quote_return_pct": (bid/ask-1)*100}
    return {"status": "unverified", "reason": "segment_ended"}


def evaluate_segment(rows, infos, ticker, segment):
    rows = [prepare(r) for r in rows if r["ticker"] == ticker]
    infos = [prepare(r) for r in infos if r["ticker"] == ticker]
    # Status can only become known at its receiver time; ties are conservative.
    events = sorted([(r["at"], 0, r) for r in rows]+[(r["at"], 1, r) for r in infos], key=lambda r: (r[0], r[1], r[2]["id"]))
    all_books = [r for r in rows if r["kind"] == "book"]
    book_positions = {r["id"]: i for i, r in enumerate(all_books)}
    books, trades, status = deque(), deque(), None
    started, blocked_until = None, None
    next_allowed = {key: None for key in VARIANTS}
    output = {key: [] for key in VARIANTS}
    for at, _, row in events:
        started = started or at
        while books and books[0]["at"] < at-timedelta(seconds=20):
            books.popleft()
        while trades and trades[0]["at"] < at-timedelta(seconds=30):
            trades.popleft()
        if row["kind"] == "info":
            status = row
            continue
        if row["kind"] == "trade":
            if trade_valid(row):
                trades.append(row)
            else:
                blocked_until = at+timedelta(seconds=30)
            continue
        if row["kind"] != "book":
            continue
        if not book_usable(row, at, max_age=0):
            books.clear()
            blocked_until = at+timedelta(seconds=30)
            continue
        if books and (at-books[-1]["at"]).total_seconds() > 5:
            books.clear()
            trades.clear()
            started = at
        books.append(row)
        if ((at-started).total_seconds() < 30 or (blocked_until and at <= blocked_until)
                or status is None or status["causal_eligible"] != 1
                or (at-status["at"]).total_seconds() > 360
                or status["payload"].get("status") != "SECURITY_TRADING_STATUS_NORMAL_TRADING"
                or status["payload"].get("market_orders_available") is not True):
            continue
        families = triggers(books, trades, at)
        for key, variant in VARIANTS.items():
            if variant["family"] not in families or (next_allowed[key] and at < next_allowed[key]):
                continue
            horizon = variant["horizon_seconds"]
            # Cooldown fixed at signal time, even if later execution is unknown.
            next_allowed[key] = at+timedelta(seconds=horizon+15)
            result = execution(at, all_books[book_positions[row["id"]]+1:], horizon)
            output[key].append({"ticker": ticker, "segment": segment, "signal_at": at.isoformat(), **result})
    return output


def summary(outcomes):
    valid = [r for r in outcomes if r["status"] == "verified_quote_replay"]
    days = sorted({r["signal_at"][:10] for r in outcomes})
    average = lambda field: sum(r[field] for r in valid)/len(valid) if valid else None
    return {"signals": len(outcomes), "verified": len(valid), "unverified": len(outcomes)-len(valid),
            "distinct_days": len(days), "mean_trade_return_pct": average("return_pct"),
            "mean_stress_trade_return_pct": average("stress_return_pct"),
            "mean_gross_quote_return_pct": average("gross_quote_return_pct"),
            "profitable_quotes": sum(r["return_pct"] > 0 for r in valid),
            "sufficient_sample": len(days) >= 3 and len(valid) >= 30,
            "independent_holdout": False, "entry_allowed": False,
            "note": "Средняя доходность условной сделки, НЕ доходность счёта. Непроверенные исполнения не считаются нулевыми или прибыльными."}
