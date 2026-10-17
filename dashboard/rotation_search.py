"""Registered long-only selection among stocks, rather than trading every stock."""
from bisect import bisect_left
from dataclasses import dataclass
from datetime import date

from .risk import position_size
from .signals import MSK


VERSION = "rotation_search_v2"


@dataclass(frozen=True)
class Rotation:
    key: str
    style: str
    lookback: int
    schedule: str
    trend_filter: bool


ROTATIONS = tuple(Rotation(f"{style}_{n}_{schedule}_{int(trend)}", style, n, schedule, trend)
                  for style, n, schedule, trend in (
                      ("momentum", 20, "week", True), ("momentum", 60, "week", True),
                      ("momentum", 120, "week", True), ("momentum", 20, "month", True),
                      ("momentum", 60, "month", True), ("momentum", 120, "month", True),
                      ("reversal", 2, "week", False), ("reversal", 5, "week", False),
                      ("reversal", 2, "week", True), ("reversal", 5, "week", True),
                      ("reversal", 2, "month", False), ("reversal", 5, "month", False),
                      ("reversal", 2, "two_weeks", False), ("reversal", 5, "two_weeks", False),
                      ("reversal", 10, "two_weeks", False), ("reversal", 2, "three_weeks", False),
                      ("reversal", 5, "three_weeks", False), ("reversal", 10, "three_weeks", False)))


def schedule_bucket(day, schedule):
    if schedule == "week":
        return day.isocalendar()[:2]
    if schedule == "month":
        return day.year, day.month
    if schedule in ("two_weeks", "three_weeks"):
        # Fixed Monday anchor, independent of sample start or future sessions.
        width = 14 if schedule == "two_weeks" else 21
        return (day-date(2020, 1, 6)).days // width
    raise ValueError("Unknown rebalance schedule")


def choose_stock(prepared, day, rule):
    """Rank only closes whose daily bar dates strictly precede today's date."""
    ranked = []
    for ticker, data in prepared.items():
        bars, dates, rows = data["bars"], data["dates"], data["features"]
        i = bisect_left(dates, day)-1
        if i < max(100, rule.lookback) or (day-dates[i]).days > 4:
            continue
        c, f = bars[i], rows[i]
        if c.volume <= 0 or rule.trend_filter and c.close <= f["ema"][100]:
            continue
        performance = c.close/bars[i-rule.lookback].close-1
        if rule.style == "momentum" and performance <= 0:
            continue
        if rule.style == "reversal" and performance >= 0:
            continue
        ranked.append((performance if rule.style == "momentum" else -performance, ticker))
    return sorted(ranked, key=lambda x: (-x[0], x[1]))[0][1] if ranked else None


def simulate_rotation(prepared, rule, first_day, last_day):
    days = sorted({d for data in prepared.values() for d in data["dates"]
                   if first_day <= d.isoformat() < last_day and d.weekday() < 5})
    cash = stress_cash = 100000.
    fee = slip = .0005
    peak, dd, p, bucket = cash, 0., None, None
    trades, daily = [], []

    def close_position(out, day, reason):
        nonlocal cash, stress_cash, p
        proceeds = p["shares"]*out*(1-slip)*(1-fee)
        cash += proceeds
        stress_cash += p["shares"]*out*.999*.999
        pnl = proceeds-p["cost"]
        trades.append({"ticker": p["ticker"], "entry_day": p["day"].isoformat(),
                       "exit_day": day.isoformat(), "entry_quote": p["entry"], "exit_quote": out,
                       "shares": p["shares"], "pnl": pnl, "return_pct": pnl/p["cost"]*100,
                       "reason": reason, "verified_bars": p["verified"]})
        p = None

    for day in days:
        active = {ticker: data["by_date"].get(day) for ticker, data in prepared.items()}
        new_bucket = schedule_bucket(day, rule.schedule)
        rebalance = new_bucket != bucket
        wanted = choose_stock(prepared, day, rule) if rebalance else None
        bucket = new_bucket
        if p is not None:
            c = active[p["ticker"]]
            if c is None:
                p["verified"] = False
            else:
                data = prepared[p["ticker"]]
                i = bisect_left(data["dates"], day)-1
                before, f = data["bars"][i], data["features"][i]
                if not .6 <= c.open/before.close <= 1.4 or c.volume == 0:
                    p["verified"] = False
                # Trailing stop uses only the preceding closed day's close/ATR.
                p["stop"] = max(p["stop"], round(before.close-3*f["atr"], 9))
                if c.open <= p["stop"]:
                    close_position(c.open, day, "gap_stop")
                elif rebalance and wanted != p["ticker"]:
                    close_position(c.open, day, "rebalance_next_open")
        if p is None and rebalance and wanted is not None:
            data, c = prepared[wanted], active[wanted]
            if c is not None:
                i = bisect_left(data["dates"], day)-1
                before, f = data["bars"][i], data["features"][i]
                distance = max(3*f["atr"], c.open*.005)
                stop = round(c.open-distance, 9)
                if stop > 0 and .6 <= c.open/before.close <= 1.4:
                    size = position_size(cash, .5, 25, round(c.open, 9), stop,
                                         round(c.open+2*distance, 9), data["lot"])
                    shares = size["shares"]
                    if shares:
                        cost = shares*c.open*(1+slip)*(1+fee)
                        cash -= cost
                        stress_cash -= shares*c.open*1.001*1.001
                        p = {"ticker": wanted, "entry": c.open, "stop": stop, "shares": shares,
                             "cost": cost, "day": day, "verified": c.volume > 0,
                             "last_close": c.close}
        if p is not None:
            c = active[p["ticker"]]
            if c is not None:
                if c.low <= p["stop"]:
                    close_position(p["stop"], day, "stop")
                elif day == days[-1]:
                    close_position(c.close, day, "window_end")
                else:
                    p["last_close"] = c.close
            elif day == days[-1]:
                p["verified"] = False
                close_position(p["last_close"], day, "missing_end_quote")
        marked = cash if p is None else cash+p["shares"]*p["last_close"]*(1-slip)*(1-fee)
        peak = max(peak, marked)
        dd = max(dd, (peak-marked)/peak)
        daily.append({"day": day.isoformat(), "equity": marked/100000})
    return {"return_pct": (cash/100000-1)*100, "stress_return_pct": (stress_cash/100000-1)*100,
            "max_drawdown_pct": dd*100, "trades": len(trades), "trade_log": trades,
            "unverified_trades": sum(not t["verified_bars"] for t in trades), "daily_curve": daily}


def prepare(saved, bars, features):
    dates = [c.time.astimezone(MSK).date() for c in bars]
    if len(set(dates)) != len(dates):
        raise ValueError("Duplicate daily dates")
    return {"bars": bars, "dates": dates, "features": features,
            "lot": saved["instrument"]["lot"], "by_date": dict(zip(dates, bars))}
