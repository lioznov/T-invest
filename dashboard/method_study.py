"""Fixed, exploratory candle experiments. Never changes live signals or sends orders.

The five hypotheses share an execution model. Historical results are descriptive,
not a calibrated probability of profit and not independent forward validation.
"""
from collections import defaultdict, deque
from datetime import timedelta
from math import isfinite, prod
from statistics import mean, median

from .risk import position_size
from .signals import MSK, features, buy_hold_return


PROTOCOL_VERSION = "intraday_methods_v1"
METHODS = {
    "baseline": "Тренд + VWAP + объём, редкий вход",
    "hourly": "База + подтверждение часового тренда",
    "seasonal": "База с объёмом для этого времени дня",
    "combined": "Часовой тренд + объём по времени дня",
    "pullback": "Откат к VWAP и возобновление роста",
}
RULES = {
    "ema_period": 50, "hourly_warmup": 100, "volume_days": 20,
    "min_volume_days": 10, "volume_ratio": 1.3,
    "pullback_volume_ratio": 1.0, "stop_atr": 1.5,
    "minimum_stop_fraction": .003, "target_r": 2,
    "entry_msk": "10:30–17:00, Monday–Friday",
    "exit_msk": "18:00 (close of the 17:45 candle)",
    "maximum_hold_minutes": 180, "maximum_entries_per_day": 1,
    "capital": 100000, "risk_pct": .5, "allocation_pct": 25,
    "fee_bps_per_side": 5, "slippage_bps_per_side": 5,
    "windows": 4, "context_fraction": .2,
}


def validate_candles(candles):
    previous = None
    for c in candles:
        prices = (c.open, c.high, c.low, c.close)
        if (c.time.tzinfo is None or (previous is not None and c.time <= previous)
                or not all(isfinite(p) and p > 0 for p in prices)
                or c.low > min(c.open, c.close) or c.high < max(c.open, c.close)
                or c.low > c.high or not isfinite(c.volume) or c.volume < 0
                or c.time.minute % 15 or c.time.second or c.time.microsecond):
            raise ValueError("Нужны упорядоченные, уникальные и корректные 15-минутные свечи.")
        previous = c.time


def study_features(candles):
    """Only completed, contiguous clock hours enter the hourly EMA.

    Seasonal volume compares the same Moscow HH:MM with preceding weekdays.
    Today's volume is inserted only AFTER its feature has been calculated.
    """
    validate_candles(candles)
    rows = features(candles)
    volume_history = defaultdict(lambda: deque(maxlen=RULES["volume_days"]))
    hour_key, hour_bars, hourly_values = None, [], []
    hour_close, hour_at = None, None
    alpha = 2 / (RULES["ema_period"] + 1)
    for c, row in zip(candles, rows):
        local = c.time.astimezone(MSK)
        key = (local.date(), local.hour)
        if key != hour_key:
            hour_key, hour_bars = key, []
        hour_bars.append(c)
        if (local.minute == 45 and len(hour_bars) == 4
                and [x.time.astimezone(MSK).minute for x in hour_bars] == [0, 15, 30, 45]
                and all(b.time-a.time == timedelta(minutes=15)
                        for a, b in zip(hour_bars, hour_bars[1:]))):
            hour_close, hour_at = c.close, c.time + timedelta(minutes=15)
            hourly_values.append(hour_close if not hourly_values else
                                 alpha*hour_close + (1-alpha)*hourly_values[-1])
        current_at = c.time + timedelta(minutes=15)
        ready = (len(hourly_values) >= RULES["hourly_warmup"] and hour_at is not None
                 and timedelta(0) <= current_at-hour_at <= timedelta(minutes=90))
        row["hourly_count"] = len(hourly_values)
        row["hourly_at"] = hour_at.isoformat() if hour_at else None
        row["hourly_ema"] = hourly_values[-1] if hourly_values else None
        row["hourly_trend"] = bool(ready and hour_close > hourly_values[-1]
                                   and hourly_values[-1] > hourly_values[-4])
        history = volume_history[(local.hour, local.minute)]
        reference = median(history) if len(history) >= RULES["min_volume_days"] else 0
        row["seasonal_samples"] = len(history)
        row["seasonal_rvol"] = c.volume/reference if reference > 0 else None
        if local.weekday() < 5:
            history.append(c.volume)
    return rows


def study_signals(candles, rows):
    if len(candles) != len(rows):
        raise ValueError("Число свечей и признаков не совпадает.")
    result = {key: [] for key in METHODS}
    for i, (c, row) in enumerate(zip(candles, rows)):
        trend = i >= 100 and c.close > row["ema"] and row["rising"]
        above_vwap = row["vwap"] is not None and c.close > row["vwap"]
        baseline = trend and above_vwap and row["rvol"] >= RULES["volume_ratio"]
        seasonal = (trend and above_vwap and row["seasonal_rvol"] is not None
                    and row["seasonal_rvol"] >= RULES["volume_ratio"])
        pullback = False
        if i and trend and above_vwap and row["hourly_trend"]:
            before, prev = candles[i-1], rows[i-1]
            same_day = c.time.astimezone(MSK).date() == before.time.astimezone(MSK).date()
            pullback = bool(same_day and c.time-before.time == timedelta(minutes=15)
                            and prev["vwap"] is not None and before.low <= prev["vwap"]
                            and c.close > before.high and c.close > c.open
                            and c.close <= row["vwap"] + row["atr"]
                            and row["seasonal_rvol"] is not None
                            and row["seasonal_rvol"] >= RULES["pullback_volume_ratio"])
        values = (baseline, baseline and row["hourly_trend"], seasonal,
                  seasonal and row["hourly_trend"], pullback)
        for key, value in zip(METHODS, values):
            result[key].append(bool(value))
    return result


def simulate_method(candles, rows, flags, start, end, lot, *, capital=100000,
                    risk_pct=.5, allocation_pct=25, fee_bps=5, slippage_bps=5):
    """Long-only whole lots, next open, at most one new signal per weekday.

    Missing bars while holding invalidate that trade for evidence. A next-known-
    open liquidation is still shown as a model assumption, never a verified fill.
    Double-cost stress preserves the same quotes, quantities and exit path.
    """
    if (not 100 <= start < end <= len(candles) or len(rows) != len(candles)
            or len(flags) != len(candles)):
        raise ValueError("Неверные границы исследования.")
    # Reuse the public calculator's validation, including integer lot constraints.
    position_size(capital, risk_pct, allocation_pct, 100, 99, 102, lot,
                  fee_bps, slippage_bps)
    fee, slip = fee_bps/10000, slippage_bps/10000
    stress_fee, stress_slip = 2*fee, 2*slip
    cash = stress_cash = float(capital)
    peak, drawdown, position, last_day = cash, 0.0, None, None
    trades, curve, skipped_lots, skipped_gaps = [], [], 0, 0
    for i in range(start, end):
        c = candles[i]
        local = c.time.astimezone(MSK)
        minutes = local.hour*60 + local.minute
        adjacent = c.time-candles[i-1].time == timedelta(minutes=15)
        edge = flags[i-1] and not flags[i-2]
        if (position is None and edge and local.weekday() < 5
                and 630 <= minutes <= 1020 and local.date() != last_day):
            if not adjacent:
                skipped_gaps += 1
            elif candles[i-1].volume > 0:
                distance = max(rows[i-1]["atr"]*RULES["stop_atr"],
                               c.open*RULES["minimum_stop_fraction"])
                # SDK quotes have at most nine decimal places; remove float tails
                # before feeding the exact Decimal-based position calculator.
                entry_quote = round(c.open, 9)
                stop = round(entry_quote-distance, 9)
                target = round(entry_quote+RULES["target_r"]*distance, 9)
                if stop > 0:
                    size = position_size(cash, risk_pct, allocation_pct, entry_quote, stop,
                                         target, lot, fee_bps, slippage_bps)
                    shares = size["shares"]
                    if shares:
                        entry_cost = shares*c.open*(1+slip)*(1+fee)
                        stress_cost = shares*c.open*(1+stress_slip)*(1+stress_fee)
                        cash -= entry_cost
                        stress_cash -= stress_cost
                        position = {"entry": c.open, "stop": stop, "target": target,
                                    "shares": shares, "cost": entry_cost,
                                    "stress_cost": stress_cost, "time": c.time,
                                    "planned_loss": size["planned_loss"], "verified": True}
                        last_day = local.date()
                    else:
                        skipped_lots += 1
        if position is not None:
            p = position
            # The entry bar's total volume is NOT known at its open. It cannot
            # veto an entry retroactively; a zero-volume bar instead makes the
            # modeled fill unverified and disqualifies positive evidence.
            if c.volume == 0:
                p["verified"] = False
            out, reason, verified = None, "", p["verified"]
            if i > start and c.time > p["time"] and not adjacent:
                out, reason, verified = c.open, "missing_quotes", False
            elif c.open <= p["stop"]:
                out, reason = c.open, "gap_stop"
            elif c.open >= p["target"]:
                out, reason = p["target"], "gap_target"
            elif c.low <= p["stop"]:
                out, reason = p["stop"], "stop"
            elif c.high >= p["target"]:
                out, reason = p["target"], "target"
            elif (c.time+timedelta(minutes=15)-p["time"] >= timedelta(minutes=180)
                  or minutes >= 1065 or i == end-1):
                out, reason = c.close, "window_end" if i == end-1 else "time"
            if out is not None:
                proceeds = p["shares"]*out*(1-slip)*(1-fee)
                stress_proceeds = p["shares"]*out*(1-stress_slip)*(1-stress_fee)
                pnl = proceeds-p["cost"]
                cash += proceeds
                stress_cash += stress_proceeds
                trades.append({"signal_available_at": p["time"].isoformat(),
                               "entry_time": p["time"].isoformat(),
                               "exit_bar": c.time.isoformat(), "entry_quote": p["entry"],
                               "exit_quote": out, "shares": p["shares"],
                               "planned_loss": p["planned_loss"], "pnl": pnl,
                               "return_pct": pnl/p["cost"]*100, "reason": reason,
                               "verified_bars": verified,
                               "gross_pnl": p["shares"]*(out-p["entry"])})
                position = None
        marked = cash if position is None else cash + position["shares"]*c.close*(1-slip)*(1-fee)
        peak = max(peak, marked)
        drawdown = max(drawdown, (peak-marked)/peak)
        curve.append({"time": (c.time+timedelta(minutes=15)).isoformat(), "equity": marked/capital})
    profits = sum(max(0, t["pnl"]) for t in trades)
    losses = -sum(min(0, t["pnl"]) for t in trades)
    return {"trades": len(trades), "return_pct": (cash/capital-1)*100,
            "stress_return_pct": (stress_cash/capital-1)*100,
            "max_drawdown_pct": drawdown*100,
            "mean_trade_pct": mean(t["return_pct"] for t in trades) if trades else None,
            "win_rate_pct": sum(t["pnl"] > 0 for t in trades)/len(trades)*100 if trades else None,
            "profit_factor": profits/losses if losses else None,
            "gross_pnl": sum(t["gross_pnl"] for t in trades),
            "net_pnl": sum(t["pnl"] for t in trades),
            "unverified_trades": sum(not t["verified_bars"] for t in trades),
            "skipped_lots": skipped_lots, "skipped_gaps": skipped_gaps,
            "trade_log": trades, "curve": curve}


def compare_methods(candles, lot, *, fee_bps=5, slippage_bps=5):
    rows = study_features(candles)
    context = max(400, len(candles)//5)
    if len(candles)-context < 400:
        return {"status": "insufficient", "required_bars": 800}
    flags = study_signals(candles, rows)
    edges = [context+(len(candles)-context)*i//4 for i in range(5)]
    windows = [{"index": i+1, "start_index": a, "end_index": b,
                "start": candles[a].time.isoformat(),
                "end": (candles[b-1].time+timedelta(minutes=15)).isoformat(),
                "buy_hold_pct": buy_hold_return(candles, a, b, fee_bps, slippage_bps)}
               for i, (a, b) in enumerate(zip(edges, edges[1:]))]
    methods = []
    for key, name in METHODS.items():
        results = [simulate_method(candles, rows, flags[key], w["start_index"],
                                   w["end_index"], lot, fee_bps=fee_bps,
                                   slippage_bps=slippage_bps) for w in windows]
        logs = [t for result in results for t in result["trade_log"]]
        equity, peak, drawdown = 1., 1., 0.
        for result in results:
            for point in result["curve"]:
                marked = equity*point["equity"]
                peak = max(peak, marked)
                drawdown = max(drawdown, (peak-marked)/peak)
            equity *= 1+result["return_pct"]/100
        stress = (prod(1+w["stress_return_pct"]/100 for w in results)-1)*100
        uncertain = sum(w["unverified_trades"] for w in results)
        positives = sum(w["return_pct"] > 0 for w in results)
        enough = len(logs) >= 30 and all(w["trades"] >= 5 for w in results)
        mean_trade = mean(t["return_pct"] for t in logs) if logs else None
        verdict = ("incomplete_quotes" if uncertain else "insufficient_trades" if not enough
                   else "negative" if equity <= 1 or mean_trade <= 0
                   else "mixed" if positives < 3 or stress <= 0 else "positive_unproven")
        methods.append({"key": key, "name": name, "trades": len(logs),
                        "return_pct": (equity-1)*100, "stress_return_pct": stress,
                        "max_drawdown_pct": drawdown*100, "mean_trade_pct": mean_trade,
                        "win_rate_pct": sum(t["pnl"] > 0 for t in logs)/len(logs)*100 if logs else None,
                        "positive_windows": positives, "unverified_trades": uncertain,
                        "verdict": verdict,
                        "windows": [{k: v for k, v in r.items() if k != "curve"} for r in results]})
    return {"status": "ok", "protocol": PROTOCOL_VERSION, "windows": windows,
            "methods": methods, "count": len(candles), "lot": lot,
            "context_bars": context, "fee_bps": fee_bps, "slippage_bps": slippage_bps}
