"""Registered long-only strategy search; no broker order calls or live rule changes."""
from dataclasses import asdict, dataclass
from datetime import timedelta
from math import ceil, isfinite, sqrt
from statistics import mean

from .risk import position_size
from .signals import Candle, MSK, ema


VERSION = "registered_search_v2"
DEVELOPMENT = ("2023-10-02", "2026-07-01")
VALIDATION = ("2026-07-01", "2026-09-01")
DISCOVERY_TICKERS = ("SBER", "GAZP", "LKOH", "NVTK", "GMKN", "ROSN", "YDEX")
RESERVE_TICKERS = ("MTSS", "SNGS", "MAGN")


@dataclass(frozen=True)
class Strategy:
    key: str
    family: str
    timeframe: str
    lookback: int
    threshold: float
    trend_filter: bool
    stop_atr: float
    hold_bars: int


STRATEGIES = tuple(
    Strategy(f"{tf}_{family}_{lookback}_{str(threshold).replace('.', '_')}_{int(trend)}",
             family, tf, lookback, threshold, trend, stop, hold)
    for tf, entries in (
        ("hour", (("ema", 10, 50, False, 3, 40), ("ema", 20, 100, False, 3, 80),
                  ("channel", 20, 10, False, 3, 40), ("channel", 55, 20, False, 3, 80),
                  ("bollinger", 20, 2, False, 3, 16), ("bollinger", 20, 2, True, 3, 16),
                  ("bollinger", 20, 2.5, False, 3, 16), ("bollinger", 20, 2.5, True, 3, 16),
                  ("rsi", 2, 5, False, 3, 8), ("rsi", 2, 5, True, 3, 8),
                  ("rsi", 2, 10, False, 3, 8), ("rsi", 2, 10, True, 3, 8))),
        ("day", (("ema", 5, 20, False, 3, 30), ("ema", 10, 50, False, 3, 60),
                 ("ema", 20, 100, False, 3, 90), ("channel", 20, 10, False, 3, 60),
                 ("channel", 55, 20, False, 3, 90), ("bollinger", 20, 2, False, 3, 5),
                 ("bollinger", 20, 2, True, 3, 5), ("bollinger", 20, 2.5, False, 3, 5),
                 ("bollinger", 20, 2.5, True, 3, 5), ("rsi", 2, 5, False, 3, 5),
                 ("rsi", 2, 5, True, 3, 5), ("rsi", 2, 10, True, 3, 5))),
    ) for family, lookback, threshold, trend, stop, hold in entries
)
REGISTRY = {s.key: s for s in STRATEGIES}


def validate_bars(bars):
    previous = None
    for c in bars:
        prices = (c.open, c.high, c.low, c.close)
        if (c.time.tzinfo is None or previous is not None and c.time <= previous
                or not all(isfinite(p) and p > 0 for p in prices)
                or c.low > min(c.open, c.close) or c.high < max(c.open, c.close)
                or c.low > c.high or not isfinite(c.volume) or c.volume < 0):
            raise ValueError("Invalid, duplicate or unordered OHLC data")
        previous = c.time


def feature_rows(bars):
    """Causal arithmetic averages, population bands and Wilder RSI(2)."""
    validate_bars(bars)
    closes = [c.close for c in bars]
    averages = {p: ema(closes, p) for p in (5, 10, 20, 50, 100)}
    gains, losses, ranges, result = 0., 0., [], []
    for i, c in enumerate(bars):
        previous = bars[i-1].close if i else c.open
        change = c.close-previous
        gains = (gains+max(change, 0))/2
        losses = (losses+max(-change, 0))/2
        rsi = 100*gains/(gains+losses) if gains+losses else 50.
        ranges.append(max(c.high-c.low, abs(c.high-previous), abs(c.low-previous)))
        window = closes[max(0, i-19):i+1]
        center = mean(window)
        sigma = sqrt(mean((p-center)**2 for p in window))
        result.append({"ema": {p: values[i] for p, values in averages.items()},
                       "mean20": center, "sigma20": sigma, "rsi2": rsi,
                       "atr": mean(ranges[-14:]),
                       "channel20": max((x.high for x in bars[max(0, i-20):i]), default=c.high),
                       "channel55": max((x.high for x in bars[max(0, i-55):i]), default=c.high),
                       "floor10": min((x.low for x in bars[max(0, i-10):i]), default=c.low),
                       "floor20": min((x.low for x in bars[max(0, i-20):i]), default=c.low)})
    return result


def entry_signal(c, f, s):
    if s.trend_filter and c.close <= f["ema"][100]:
        return False
    if s.family == "ema":
        return f["ema"][s.lookback] > f["ema"][int(s.threshold)] and c.close > f["ema"][s.lookback]
    if s.family == "channel":
        return c.close > f[f"channel{s.lookback}"]
    if s.family == "bollinger":
        return f["sigma20"] > 0 and c.close < f["mean20"]-s.threshold*f["sigma20"]
    if s.family == "rsi":
        return f["rsi2"] < s.threshold
    raise ValueError("Unknown family")


def exit_signal(c, f, s):
    if s.family == "ema":
        return f["ema"][s.lookback] <= f["ema"][int(s.threshold)] or c.close < f["ema"][int(s.threshold)]
    if s.family == "channel":
        return c.close < f[f"floor{int(s.threshold)}"]
    if s.family == "bollinger":
        return c.close >= f["mean20"]
    if s.family == "rsi":
        return f["rsi2"] > 70
    raise ValueError("Unknown family")


def simulate(bars, rows, strategy, first_day, last_day, lot=1,
             *, capital=100000, risk_pct=.5, allocation_pct=25, fee_bps=5, slip_bps=5):
    """Signal exits execute next open. Fixed protective stop, no future-volume veto.

    Swing positions deliberately remain overnight; gap stops use next open. A
    missing hourly bar inside the registered core session or a zero-volume held
    bar invalidates evidence. Missing daily sessions cannot be diagnosed without
    an exchange calendar, and are explicitly an unmodeled limitation.
    """
    if len(rows) != len(bars):
        raise ValueError("Mismatched features")
    position_size(capital, risk_pct, allocation_pct, 100, 99, 102, lot, fee_bps, slip_bps)
    fee, slip = fee_bps/10000, slip_bps/10000
    stress_fee, stress_slip = 2*fee, 2*slip
    indices = [i for i, c in enumerate(bars) if i >= 100
               and first_day <= c.time.astimezone(MSK).date().isoformat() < last_day]
    if not indices:
        return {"status": "insufficient", "trades": 0, "return_pct": 0., "stress_return_pct": 0.,
                "trade_log": [], "daily_curve": [], "max_drawdown_pct": 0., "unverified_trades": 0}
    cash = stress_cash = float(capital)
    peak, dd, p, last_entry_day = cash, 0., None, None
    trades, daily = [], {}
    for i in indices:
        c, previous, f = bars[i], bars[i-1], rows[i-1]
        local = c.time.astimezone(MSK)
        day = local.date().isoformat()
        # Gross jumps may be splits or other corporate actions. Do not reinterpret
        # them as a fortunate price move or quietly delete their resulting loss.
        suspicious_gap = not .6 <= c.open/previous.close <= 1.4
        missing = (strategy.timeframe == "hour" and local.date() == previous.time.astimezone(MSK).date()
                   and c.time-previous.time != timedelta(hours=1))
        if p is None and local.weekday() < 5 and day != last_entry_day and previous.volume > 0:
            if entry_signal(previous, f, strategy) and not suspicious_gap and not missing:
                distance = max(strategy.stop_atr*f["atr"], c.open*.005)
                stop = round(c.open-distance, 9)
                if stop > 0:
                    size = position_size(cash, risk_pct, allocation_pct, round(c.open, 9), stop,
                                         round(c.open+distance*2, 9), lot, fee_bps, slip_bps)
                    shares = size["shares"]
                    if shares:
                        cost = shares*c.open*(1+slip)*(1+fee)
                        stress_cost = shares*c.open*(1+stress_slip)*(1+stress_fee)
                        cash -= cost
                        stress_cash -= stress_cost
                        p = {"entry": c.open, "stop": stop, "index": i, "time": c.time,
                             "shares": shares, "cost": cost, "stress_cost": stress_cost,
                             "verified": True, "planned_loss": size["planned_loss"]}
                        last_entry_day = day
        if p is not None:
            if suspicious_gap or missing or c.volume == 0:
                p["verified"] = False
            out, reason = None, ""
            if c.open <= p["stop"]:
                out, reason = c.open, "gap_stop"
            elif i > p["index"] and exit_signal(previous, f, strategy):
                out, reason = c.open, "signal_next_open"
            elif c.low <= p["stop"]:
                out, reason = p["stop"], "stop"
            elif i-p["index"] >= strategy.hold_bars-1 or i == indices[-1]:
                out, reason = c.close, "window_end" if i == indices[-1] else "time"
            if out is not None:
                proceeds = p["shares"]*out*(1-slip)*(1-fee)
                cash += proceeds
                stress_cash += p["shares"]*out*(1-stress_slip)*(1-stress_fee)
                pnl = proceeds-p["cost"]
                trades.append({"entry_time": p["time"].isoformat(), "exit_bar": c.time.isoformat(),
                               "entry_quote": p["entry"], "exit_quote": out, "shares": p["shares"],
                               "return_pct": pnl/p["cost"]*100, "pnl": pnl,
                               "planned_loss": p["planned_loss"], "reason": reason,
                               "verified_bars": p["verified"]})
                p = None
        marked = cash if p is None else cash+p["shares"]*c.close*(1-slip)*(1-fee)
        peak = max(peak, marked)
        dd = max(dd, (peak-marked)/peak)
        daily[day] = marked/capital
    wins = sum(max(0, t["pnl"]) for t in trades)
    losses = -sum(min(0, t["pnl"]) for t in trades)
    return {"status": "ok", "trades": len(trades), "return_pct": (cash/capital-1)*100,
            "stress_return_pct": (stress_cash/capital-1)*100, "max_drawdown_pct": dd*100,
            "mean_trade_pct": mean(t["return_pct"] for t in trades) if trades else None,
            "win_rate_pct": sum(t["pnl"] > 0 for t in trades)/len(trades)*100 if trades else None,
            "profit_factor": wins/losses if losses else None,
            "unverified_trades": sum(not t["verified_bars"] for t in trades),
            "trade_log": trades, "daily_curve": [{"day": d, "equity": e} for d, e in daily.items()]}


def aggregate(results):
    good = [r for r in results if r["status"] == "ok"]
    days = sorted({p["day"] for r in good for p in r["daily_curve"]})
    curves = [{p["day"]: p["equity"] for p in r["daily_curve"]} for r in good]
    marked = [1.]*len(good)
    daily, peak, dd = [], 1., 0.
    for day in days:
        for j, curve in enumerate(curves):
            marked[j] = curve.get(day, marked[j])
        equity = mean(marked)
        peak = max(peak, equity)
        dd = max(dd, (peak-equity)/peak)
        daily.append({"day": day, "equity": equity})
    logs = [t for r in good for t in r["trade_log"]]
    return {"accounts": len(good), "trades": len(logs),
            "return_pct": mean(r["return_pct"] for r in good) if good else 0.,
            "stress_return_pct": mean(r["stress_return_pct"] for r in good) if good else 0.,
            "positive_accounts": sum(r["return_pct"] > 0 for r in good),
            "max_drawdown_pct": dd*100, "unverified_trades": sum(r["unverified_trades"] for r in good),
            "mean_trade_pct": mean(t["return_pct"] for t in logs) if logs else None,
            "daily_curve": daily}


def passes_screen(development, validation):
    """Descriptive search gates, never a claim of statistical significance."""
    return bool(development["accounts"] >= 5 and development["trades"] >= 40
                and validation["trades"] >= 12
                and development["positive_accounts"] >= ceil(.6*development["accounts"])
                and validation["positive_accounts"] >= ceil(.5*validation["accounts"])
                and development["return_pct"] > 0 and development["stress_return_pct"] > 0
                and validation["return_pct"] > 0 and validation["stress_return_pct"] > 0
                and development["unverified_trades"] == validation["unverified_trades"] == 0
                and development["max_drawdown_pct"] < 15)


def registry_json():
    return [asdict(s) for s in STRATEGIES]
