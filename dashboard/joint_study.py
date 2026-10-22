"""Three preregistered LONG quote-replay comparisons; never broker orders.

A one-minute breakout is NOT the site's fifteen-minute pattern strategy.
Every trigger is evaluated at receipt time, not backdated to candle open.
"""
from collections import deque
from datetime import timedelta
import math

from .microstructure_archive import utc_timestamp
from .pressure_study import prepare, top, midpoint, trade_valid, execution
from .stream_inputs import book_usable


VERSION = "joint_breakout_stream_v1"
VARIANTS = ("chart", "chart_flow", "chart_flow_book")
NAMES = {"chart": "Пробой на графике",
         "chart_flow": "Пробой + активные покупки",
         "chart_flow_book": "Пробой + покупки + поддержка стакана"}
RULES = {"previous_trials": 80, "new_comparisons": 3,
         "pattern": "positive-volume green closed 1m candle closes above preceding five highs; six consecutive minutes",
         "max_candle_receipt_delay_seconds": 10, "pattern_fresh_seconds": 10,
         "book_warmup_seconds": 30, "book_window_seconds": 20,
         "max_book_gap_seconds": 5, "buy_window_seconds": 30,
         "min_trades": 5, "buy_fraction_min": .60,
         "bid_fraction_min": .65, "near_bid_retention_min": .80,
         "persistence_seconds": 10, "spread_bps_max": 10,
         "hold_seconds": 180, "cooldown_seconds": 195,
         "fee_bps_each_side": 5, "slippage_bps_each_side": 5,
         "min_verified": 30, "min_distinct_days": 5,
         "max_segments": 96, "max_input_bytes": 150_000_000,
         "entry_allowed": False,
         "execution": "one lot at next observed ask after >=1s within 5s; bid exit after 180s within 10s; all inside same connection segment",
         "limitations": "anonymous sampled stream; no queue, orders, taxes or guaranteed fills; no return on account"}


def candle_valid(row):
    try:
        p = row["payload"]
        return bool(row["causal_eligible"] == 1 and p["source"] == "CANDLE_SOURCE_EXCHANGE"
                    and p["interval"] == "SUBSCRIPTION_INTERVAL_ONE_MINUTE"
                    and 0 <= (row["at"]-row["exchange_at"]-timedelta(minutes=1)).total_seconds() <= 10
                    and all(type(p[k]) in (int, float) and math.isfinite(p[k]) and p[k] > 0
                            for k in ("open", "high", "low", "close"))
                    and p["low"] <= min(p["open"], p["close"]) <= max(p["open"], p["close"]) <= p["high"]
                    and type(p["volume"]) is int and p["volume"] > 0)
    except (KeyError, TypeError, ValueError):
        return False


def breakout(candles):
    if len(candles) != 6:
        return False
    if any(b["exchange_at"]-a["exchange_at"] != timedelta(minutes=1)
           for a, b in zip(candles, list(candles)[1:])):
        return False
    p = candles[-1]["payload"]
    return p["close"] > p["open"] and p["close"] > max(r["payload"]["high"] for r in list(candles)[:-1])


def confirmations(books, trades, at):
    """Indicators describe aggregate observed activity, never distinct traders."""
    quantity = sum(r["payload"]["quantity"] for r in trades)
    buys = sum(r["payload"]["quantity"] for r in trades if r["payload"]["direction"] == "TRADE_DIRECTION_BUY")
    flow = len(trades) >= 5 and quantity > 0 and buys/quantity >= .60
    recent = [r for r in books if (at-r["at"]).total_seconds() <= 10]
    persistent = len(recent) >= 5 and (at-recent[0]["at"]).total_seconds() >= 9
    persistent = persistent and all(top(r)[0][1]/(top(r)[0][1]+top(r)[1][1]) >= .65 for r in recent)
    mid = midpoint(books[-1])
    depths = [sum(q for price, q in r["payload"]["bids"] if mid*.998 <= price <= mid) for r in books]
    retention = bool(max(depths, default=0) and depths[-1]/max(depths) >= .80)
    return {"chart": True, "chart_flow": flow, "chart_flow_book": flow and persistent and retention}


def normal_status(row, at):
    return bool(row and row["causal_eligible"] == 1 and 0 <= (at-row["at"]).total_seconds() <= 360
                and row["payload"].get("status") == "SECURITY_TRADING_STATUS_NORMAL_TRADING"
                and row["payload"].get("market_orders_available") is True)


def replay(signal, books, infos, signal_book):
    # Initialise exchange ordering with the actual signal book, not the first
    # future quote. Do not suppress unknown outcomes or cross reconnects.
    if books and (books[0]["exchange_at"] is None or books[0]["exchange_at"] <= signal_book["exchange_at"]):
        return {"status": "unverified", "reason": "exchange_clock_order"}
    result = execution(signal, books, 180)
    if result["status"] != "verified_quote_replay":
        return result
    end = utc_timestamp(result["exit_at"])
    states = iter(infos)
    state, pending = None, next(states, None)
    for book in books:
        if book["at"] > end:
            break
        # Status at the exact quote receipt time is conservatively not yet known.
        while pending and pending["at"] < book["at"]:
            state, pending = pending, next(states, None)
        if not normal_status(state, book["at"]):
            return {"status": "unverified", "reason": "trading_status_unknown_or_halted"}
    return result


def evaluate(rows, extras, ticker):
    """Past minute history may span connections; trade paths never do."""
    market = [prepare(r) for r in rows if r["ticker"] == ticker]
    chart = [prepare(r) for r in extras if r["ticker"] == ticker]
    # Within market table preserve id-order at tied timestamps. Auxiliary
    # status/candle ties are only visible after that timestamp's market records.
    events = sorted(market+chart, key=lambda r: (r["at"], int(r["kind"] not in ("book", "trade")), r["segment"], r["id"]))
    per_segment, statuses = {}, {}
    for r in market:
        if r["kind"] == "book":
            per_segment.setdefault(r["segment"], []).append(r)
    for r in chart:
        if r["kind"] == "info":
            statuses.setdefault(r["segment"], []).append(r)
    for group in statuses.values():
        group.sort(key=lambda r: (r["at"], r["id"]))
    positions = {(r["segment"], r["id"]): i for group in per_segment.values() for i, r in enumerate(group)}
    books, trades, candles = deque(), deque(), deque(maxlen=6)
    current_segment, started, blocked, status, signal_candle = None, None, None, None, None
    seen, used = {}, set()
    next_allowed = {key: None for key in VARIANTS}
    outcomes = {key: [] for key in VARIANTS}
    candidates = []
    diagnostics = {"breakout_candles": 0, "invalid_minute_messages": 0, "bad_books": 0,
                   "invalid_trades": 0, "book_gaps_or_clock_reversals": 0}
    for r in events:
        at, segment, kind = r["at"], r["segment"], r["kind"]
        if segment != current_segment:
            books.clear(); trades.clear()
            current_segment, started, blocked, status = segment, at, None, None
        if kind == "candle":
            if r["payload"].get("interval") != "SUBSCRIPTION_INTERVAL_ONE_MINUTE":
                continue
            identity = r["exchange_at"]
            if not candle_valid(r):
                diagnostics["invalid_minute_messages"] += 1
                candles.clear(); signal_candle = None
                continue
            if identity in seen:
                if seen[identity] != r["payload"]:
                    candles.clear(); signal_candle = None
                continue
            seen[identity] = r["payload"]
            if candles and identity <= candles[-1]["exchange_at"]:
                candles.clear(); signal_candle = None
                continue
            candles.append(r)
            signal_candle = r if breakout(candles) else None
            diagnostics["breakout_candles"] += signal_candle is not None
            continue
        if kind == "info":
            status = r
            continue
        while trades and trades[0]["at"] < at-timedelta(seconds=30):
            trades.popleft()
        while books and books[0]["at"] < at-timedelta(seconds=20):
            books.popleft()
        if kind == "trade":
            if trade_valid(r):
                trades.append(r)
            else:
                diagnostics["invalid_trades"] += 1
                blocked = at+timedelta(seconds=30)
            continue
        if kind != "book":
            continue
        if not book_usable(r, at, max_age=0):
            diagnostics["bad_books"] += 1
            books.clear(); trades.clear(); started = at
            blocked = at+timedelta(seconds=30)
            continue
        if books and ((at-books[-1]["at"]).total_seconds() > 5 or r["exchange_at"] <= books[-1]["exchange_at"]):
            diagnostics["book_gaps_or_clock_reversals"] += 1
            books.clear(); trades.clear(); started = at
        books.append(r)
        if (not signal_candle or not 0 <= (at-signal_candle["at"]).total_seconds() <= 10
                or signal_candle["exchange_at"] in used or (at-started).total_seconds() < 30
                or len(books) < 10 or (at-books[0]["at"]).total_seconds() < 18
                or (blocked and at <= blocked) or not normal_status(status, at)):
            continue
        bid, ask = top(r)
        if (ask[0]-bid[0])/midpoint(r)*10000 > 10:
            continue
        used.add(signal_candle["exchange_at"])
        filters = confirmations(books, trades, at)
        candidate = {"ticker": ticker, "signal_at": at.isoformat(), "segment": segment,
                     "candle_at": signal_candle["exchange_at"].isoformat(), "filters": filters}
        candidates.append(candidate)
        future = per_segment[segment][positions[(segment, r["id"])]+1:]
        # One shared quote path per candidate enables a genuinely matched
        # comparison of selected versus rejected candidates, not cherry-picking.
        outcome = replay(at, future, statuses.get(segment, []), r)
        candidate["outcome"] = outcome
        for key in VARIANTS:
            if filters[key] and (next_allowed[key] is None or at >= next_allowed[key]):
                next_allowed[key] = at+timedelta(seconds=195)
                outcomes[key].append({k: v for k, v in candidate.items() if k not in ("filters", "outcome")} | outcome)
    return {"outcomes": outcomes, "candidates": candidates, "diagnostics": diagnostics}


def summarize(values):
    verified = [r for r in values if r["status"] == "verified_quote_replay"]
    days = sorted({utc_timestamp(r["signal_at"]).date().isoformat() for r in verified})
    mean = lambda key: sum(r[key] for r in verified)/len(verified) if verified else None
    return {"signals": len(values), "verified": len(verified), "unverified": len(values)-len(verified),
            "distinct_days": len(days), "mean_trade_return_pct": mean("return_pct"),
            "mean_stress_trade_return_pct": mean("stress_return_pct"),
            "mean_gross_quote_return_pct": mean("gross_quote_return_pct"),
            "profitable_quotes": sum(r["return_pct"] > 0 for r in verified),
            "sufficient_sample": len(verified) >= 30 and len(days) >= 5,
            "entry_allowed": False}


def matched(candidates, key):
    def values(selected):
        return [c["outcome"] | {"signal_at": c["signal_at"]} for c in candidates if c["filters"][key] is selected]
    return {"selected": summarize(values(True)), "rejected": summarize(values(False)),
            "note": "All chart candidates, same quote paths; overlapping diagnostic scenarios, not a portfolio."}
