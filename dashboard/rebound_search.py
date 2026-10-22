"""Exploratory long-only recovery with prior-day price and volume confirmation.

The previous 42 hypotheses and their failed reserved-stock check remain recorded.
This is a new hypothesis, not an independent retry of the same holdout.
"""
from bisect import bisect_left
from dataclasses import dataclass
from statistics import mean

from .risk import position_size
from .rotation_search import schedule_bucket


VERSION = "confirmed_rebound_v1"
RESERVED = ("TATN", "CHMF", "PHOR")


@dataclass(frozen=True)
class Rebound:
    key: str
    lookback: int
    schedule: str
    trend_filter: bool


RULES = tuple(Rebound(f"rebound_{n}_{schedule}_{int(trend)}", n, schedule, trend)
              for n in (2, 5) for schedule in ("day", "week") for trend in (False, True))


def choose_stock(prepared, day, rule):
    ranked = []
    for ticker, data in prepared.items():
        bars, dates, rows = data["bars"], data["dates"], data["features"]
        i = bisect_left(dates, day)-1
        if i < 100 or (day-dates[i]).days > 4:
            continue
        c, f = bars[i], rows[i]
        average_volume = mean(b.volume for b in bars[i-20:i])
        decline = c.close/bars[i-rule.lookback].close-1
        if (decline >= 0 or average_volume <= 0 or c.volume < 1.5*average_volume
                or c.close <= c.open or c.close <= bars[i-1].close
                or rule.trend_filter and c.close <= f["ema"][100]):
            continue
        ranked.append((decline, ticker))
    return min(ranked)[1] if ranked else None


def simulate_rebound(prepared, rule, first_day, last_day):
    """Confirmed prior close -> next open; exits on recovery, stop or 10 held bars."""
    days = sorted({d for data in prepared.values() for d in data["dates"]
                   if first_day <= d.isoformat() < last_day and d.weekday() < 5})
    cash = stress_cash = 100000.
    peak, drawdown, position, bucket = cash, 0., None, None
    trades, curve = [], []

    def close_position(quote, day, reason):
        nonlocal cash, stress_cash, position
        p = position
        proceeds = p["shares"]*quote*.9995*.9995
        cash += proceeds
        stress_cash += p["shares"]*quote*.999*.999
        trades.append({"ticker": p["ticker"], "entry_day": p["day"].isoformat(),
                       "exit_day": day.isoformat(), "entry_quote": p["entry"],
                       "exit_quote": quote, "shares": p["shares"],
                       "pnl": proceeds-p["cost"], "reason": reason,
                       "verified_bars": p["verified"]})
        position = None

    for day in days:
        new_bucket = day if rule.schedule == "day" else schedule_bucket(day, rule.schedule)
        rebalance = new_bucket != bucket
        wanted = choose_stock(prepared, day, rule) if rebalance else None
        bucket = new_bucket
        exited_today = False
        if position is not None:
            data = prepared[position["ticker"]]
            c = data["by_date"].get(day)
            if c is None:
                position["verified"] = False
            else:
                i = bisect_left(data["dates"], day)-1
                before, f = data["bars"][i], data["features"][i]
                position["held_bars"] += 1
                if c.volume == 0 or not .6 <= c.open/before.close <= 1.4:
                    position["verified"] = False
                if c.open <= position["stop"]:
                    close_position(c.open, day, "gap_stop")
                elif before.close >= f["mean20"] or position["held_bars"] >= 10:
                    close_position(c.open, day, "recovery_next_open" if before.close >= f["mean20"] else "time_next_open")
                elif rebalance and wanted != position["ticker"]:
                    close_position(c.open, day, "rebalance_next_open")
                exited_today = position is None
        # Do not re-enter on an open at which the old position just exited.
        if position is None and rebalance and wanted is not None and not exited_today:
            data = prepared[wanted]
            c = data["by_date"].get(day)
            if c is not None:
                i = bisect_left(data["dates"], day)-1
                before, f = data["bars"][i], data["features"][i]
                distance = max(3*f["atr"], .005*c.open)
                stop = round(c.open-distance, 9)
                if stop > 0 and .6 <= c.open/before.close <= 1.4:
                    size = position_size(cash, .5, 25, round(c.open, 9), stop,
                                         round(c.open+2*distance, 9), data["lot"])
                    shares = size["shares"]
                    if shares:
                        cost = shares*c.open*1.0005*1.0005
                        cash -= cost
                        stress_cash -= shares*c.open*1.001*1.001
                        position = {"ticker": wanted, "day": day, "entry": c.open,
                                    "stop": stop, "shares": shares, "cost": cost,
                                    "last_close": c.close, "held_bars": 1, "verified": c.volume > 0}
        if position is not None:
            c = prepared[position["ticker"]]["by_date"].get(day)
            if c is not None:
                if c.low <= position["stop"]:
                    close_position(position["stop"], day, "stop")
                elif day == days[-1]:
                    close_position(c.close, day, "window_end")
                else:
                    position["last_close"] = c.close
            elif day == days[-1]:
                position["verified"] = False
                close_position(position["last_close"], day, "missing_end_quote")
        marked = cash if position is None else cash+position["shares"]*position["last_close"]*.9995*.9995
        peak = max(peak, marked)
        drawdown = max(drawdown, (peak-marked)/peak)
        curve.append({"day": day.isoformat(), "equity": marked/100000})
    return {"return_pct": (cash/100000-1)*100, "stress_return_pct": (stress_cash/100000-1)*100,
            "trades": len(trades), "trade_log": trades, "daily_curve": curve,
            "max_drawdown_pct": drawdown*100,
            "unverified_trades": sum(not t["verified_bars"] for t in trades)}
