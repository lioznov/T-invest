"""Long-only, next-open, whole-lot tests of causal chart events.

The floor of an already confirmed pattern sets the stop, with a minimum distance
of 0.3%. Fees/slippage are not removed to create profit. No order API is used.
"""
from datetime import timedelta
from statistics import mean

from .price_patterns import NAMES
from .risk import position_size
from .signals import MSK


VARIANTS = {f"{key}_{suffix}": {"family": key, "volume": volume,
                               "name": f"{name} · {'объём ≥ 1,3' if volume else 'без фильтра объёма'}"}
            for key, name in NAMES.items() for suffix, volume in (("plain", False), ("volume", True))}


def simulate_pattern(candles, events, variant, first_day, last_day, lot):
    signals = events[variant["family"]]
    if len(signals) != len(candles):
        raise ValueError("Mismatched pattern events")
    position_size(100000, .5, 25, 100, 99, 102, lot)
    indices = [i for i, c in enumerate(candles) if i >= 100
               and first_day <= c.time.astimezone(MSK).date().isoformat() < last_day]
    cash = stress_cash = 100000.
    peak, drawdown, position, last_day_entered = cash, 0., None, None
    trades, curve = [], {}
    skipped = 0
    for i in indices:
        c, before = candles[i], candles[i-1]
        local = c.time.astimezone(MSK)
        minute = local.hour*60+local.minute
        adjacent = c.time-before.time == timedelta(minutes=15)
        suspicious = not .6 <= c.open/before.close <= 1.4
        signal = signals[i-1]
        eligible = signal is not None and (not variant["volume"] or signal["rvol"] >= 1.3)
        if (position is None and eligible and local.weekday() < 5 and 630 <= minute <= 1020
                and local.date() != last_day_entered):
            # Open-price checks are known at entry. Today's final volume is NOT.
            if (not adjacent or suspicious or c.open < signal["level"]-.1*signal["atr"]
                    or c.open > signal["price"]+signal["atr"]):
                skipped += 1
            else:
                distance = max(c.open-(signal["floor"]-.1*signal["atr"]), c.open*.003)
                stop, target = round(c.open-distance, 9), round(c.open+2*distance, 9)
                if stop > 0:
                    size = position_size(cash, .5, 25, round(c.open, 9), stop, target, lot)
                    shares = size["shares"]
                    if shares:
                        cost = shares*c.open*1.0005*1.0005
                        cash -= cost
                        stress_cash -= shares*c.open*1.001*1.001
                        position = {"time": c.time, "entry": c.open, "stop": stop, "target": target,
                                    "shares": shares, "cost": cost, "verified": True,
                                    "planned_loss": size["planned_loss"]}
                        last_day_entered = local.date()
        if position is not None:
            p = position
            if c.volume == 0 or suspicious or not adjacent:
                p["verified"] = False
            quote, reason = None, ""
            if c.open <= p["stop"]:
                quote, reason = c.open, "gap_stop"
            elif c.open >= p["target"]:
                quote, reason = p["target"], "gap_target"
            elif not adjacent:
                quote, reason = c.open, "missing_data"
            elif c.low <= p["stop"]:
                quote, reason = p["stop"], "stop"
            elif c.high >= p["target"]:
                quote, reason = p["target"], "target"
            elif (c.time+timedelta(minutes=15)-p["time"] >= timedelta(minutes=180)
                  or minute >= 1065 or i == indices[-1]):
                quote, reason = c.close, "window_end" if i == indices[-1] else "time"
            if quote is not None:
                proceeds = p["shares"]*quote*.9995*.9995
                pnl = proceeds-p["cost"]
                cash += proceeds
                stress_cash += p["shares"]*quote*.999*.999
                trades.append({"entry_time": p["time"].isoformat(), "exit_bar": c.time.isoformat(),
                               "entry_quote": p["entry"], "exit_quote": quote,
                               "stop": p["stop"], "target": p["target"], "shares": p["shares"],
                               "pnl": pnl, "return_pct": pnl/p["cost"]*100,
                               "planned_loss": p["planned_loss"], "reason": reason,
                               "verified_bars": p["verified"]})
                position = None
        marked = cash if position is None else cash+position["shares"]*c.close*.9995*.9995
        peak = max(peak, marked)
        drawdown = max(drawdown, (peak-marked)/peak)
        curve[local.date().isoformat()] = marked/100000
    wins = sum(max(0, t["pnl"]) for t in trades)
    losses = -sum(min(0, t["pnl"]) for t in trades)
    return {"status": "ok" if indices else "insufficient", "return_pct": (cash/100000-1)*100,
            "stress_return_pct": (stress_cash/100000-1)*100, "trades": len(trades),
            "unverified_trades": sum(not t["verified_bars"] for t in trades),
            "max_drawdown_pct": drawdown*100, "trade_log": trades,
            "daily_curve": [{"day": day, "equity": equity} for day, equity in curve.items()],
            "win_rate_pct": sum(t["pnl"] > 0 for t in trades)/len(trades)*100 if trades else None,
            "mean_trade_pct": mean(t["return_pct"] for t in trades) if trades else None,
            "profit_factor": wins/losses if losses else None, "skipped_entries": skipped}
