"""Deterministic, read-only signal calculations. No broker order methods."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from statistics import mean

MSK = timezone(timedelta(hours=3))


@dataclass(frozen=True)
class Candle:
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int


def ema(values, period=50):
    if not values:
        return []
    result = [values[0]]
    alpha = 2 / (period + 1)
    for value in values[1:]:
        result.append(alpha * value + (1 - alpha) * result[-1])
    return result


def features(candles):
    """Causal features: each output uses only candles up to that row."""
    averages = ema([c.close for c in candles])
    result, pv, volume, session, ranges = [], 0.0, 0, None, []
    for i, c in enumerate(candles):
        day = c.time.astimezone(MSK).date()
        if day != session:
            pv, volume, session = 0.0, 0, day
        pv += (c.high + c.low + c.close) / 3 * c.volume
        volume += c.volume
        previous_close = candles[i - 1].close if i else c.open
        ranges.append(max(c.high - c.low, abs(c.high - previous_close), abs(c.low - previous_close)))
        reference = mean([x.volume for x in candles[max(0, i-20):i]]) if i else 0
        result.append({
            "ema": averages[i], "rising": i >= 3 and averages[i] > averages[i-3],
            "vwap": pv / volume if volume else None,
            "rvol": c.volume / reference if reference else 0,
            "atr": mean(ranges[-14:]),
            "breakout": i >= 20 and c.close > max(x.high for x in candles[i-20:i]),
        })
    return result


def valid_book(book):
    bids, asks = book.get("bids", []), book.get("asks", [])
    return bool(bids and asks and bids[0][0] > 0 and asks[0][0] >= bids[0][0]
                and all(p > 0 and q > 0 for p, q in bids + asks))


def analyse(snapshot, now=None):
    now = now or datetime.now(timezone.utc)
    candles, minutes = snapshot["candles"], snapshot.get("minutes", [])
    books, trades = snapshot.get("books", []), snapshot.get("trades", [])
    f = features(candles)[-1] if candles else {}
    book = books[-1] if books else {}
    book_ok = valid_book(book)
    price = (book["bids"][0][0] + book["asks"][0][0]) / 2 if book_ok else (candles[-1].close if candles else 0)
    spread = (book["asks"][0][0] - book["bids"][0][0]) / price * 10000 if book_ok else None
    today_minutes = [c for c in minutes if c.time.astimezone(MSK).date() == now.astimezone(MSK).date()]
    vf = features(today_minutes)
    vwap = vf[-1]["vwap"] if vf else None
    vwap_ok = len(vf) >= 2 and all(c.close >= x["vwap"] for c, x in zip(today_minutes[-2:], vf[-2:]) if x["vwap"] is not None) and vwap is not None and price >= vwap
    recent = [t for t in trades if now-timedelta(minutes=5) <= t["time"] <= now and t["side"] in ("buy", "sell") and t["quantity"] > 0]
    buy = sum(t["quantity"] for t in recent if t["side"] == "buy")
    sell = sum(t["quantity"] for t in recent if t["side"] == "sell")
    share = buy / (buy + sell) if buy + sell else None
    observed = [b for b in books if now-timedelta(seconds=120) <= b["time"] <= now and valid_book(b)]
    # Same price band avoids labelling a moving top-five window as cancellations.
    stable, retention = None, None
    if book_ok and len(observed) >= 3 and (observed[-1]["time"]-observed[0]["time"]).total_seconds() >= 45:
        floor, ceiling = price * .998, price * 1.002
        depths = [sum(p*q for p,q in b["bids"] if floor <= p <= ceiling) for b in observed]
        peak = max(depths[:-1], default=0)
        retention = depths[-1] / peak if peak else None
        stable = retention >= .65 if retention is not None else None
    trend = len(candles) >= 100 and candles[-1].close > f.get("ema", float("inf")) and f.get("rising", False)
    rules = [
        {"key":"trend", "name":"Цена движется вверх", "weight":20, "pass":trend if len(candles)>=100 else None,
         "detail":"Закрытие 15-минутной свечи выше EMA 50, средняя растёт. Для прогрева нужны 100 свечей."},
        {"key":"flow", "name":"Покупатели активнее", "weight":20, "pass":share >= .58 if share is not None and len(recent)>=5 else None,
         "detail":"Не менее 58% объёма направленных сделок за 5 минут приходится на покупки. Это сторона инициатора: у каждой сделки есть и покупатель, и продавец."},
        {"key":"volume", "name":"Объём выше обычного", "weight":15, "pass":f.get("rvol", 0)>=1.3 if len(candles)>=21 else None,
         "detail":"Объём закрытой 15-минутной свечи ≥ 1,3 среднего предыдущих 20 свечей. Сезонность времени дня пока не учтена."},
        {"key":"vwap", "name":"Цена удерживает среднюю дня", "weight":20, "pass":bool(vwap_ok) if len(vf)>=2 and vwap else None,
         "detail":"Два последних минутных закрытия и текущая цена выше VWAP. VWAP приближён по минутным свечам; сброс в 00:00 МСК."},
        {"key":"spread", "name":"Небольшой разрыв цен", "weight":15, "pass":spread <= 10 if spread is not None else None,
         "detail":"Разница лучшей покупки и продажи ≤ 0,10%. Это фильтр спреда, а не гарантия исполнения крупного объёма."},
        {"key":"book", "name":"Объём покупок устойчив", "weight":10, "pass":stable,
         "detail":"В полосе ±0,2% около текущей цены сохраняется ≥65% предыдущего максимума заявок покупателей. Нужно 3 снимка за ≥45 секунд. Причина исчезновения объёма неизвестна."},
    ]
    def fresh(ts, seconds):
        return ts is not None and 0 <= (now-ts).total_seconds() <= seconds
    # A completed 15-minute candle is stamped at its OPEN, up to 30 minutes ago.
    fresh_data = (fresh(book.get("time"),45) and candles and fresh(candles[-1].time,1920)
                  and today_minutes and fresh(today_minutes[-1].time,180)
                  and recent and fresh(max(t["time"] for t in recent),90))
    blockers = []
    if not snapshot.get("trading", False): blockers.append("Обычная торговая сессия закрыта или статус торгов не подтверждён.")
    if not fresh_data: blockers.append("Недостаточно свежих рыночных данных. Сигнал на вход заблокирован.")
    missing = sum(r["pass"] is None for r in rules)
    passed = sum(r["pass"] is True for r in rules)
    score = sum(r["weight"] for r in rules if r["pass"] is True)
    exit_reasons = []
    if len(candles) >= 100 and candles[-1].close < f["ema"] and not f["rising"]:
        exit_reasons.append("Цена ниже падающей EMA 50")
    if share is not None and len(recent) >= 5 and share <= .42:
        exit_reasons.append("В последних сделках преобладают продажи")
    if len(vf) >= 2 and vwap is not None and price < vwap and all(
            c.close < x["vwap"] for c, x in zip(today_minutes[-2:], vf[-2:])):
        exit_reasons.append("Цена и два минутных закрытия ниже VWAP")
    decision = "LONG" if passed == 6 and not blockers else "EXIT" if len(exit_reasons) == 3 and not blockers else "WAIT"
    if missing and decision != "EXIT": blockers.append(f"Ожидаем данные для условий: {missing}.")
    atr = f.get("atr",0)
    entry = book["asks"][0][0] if book_ok else price
    risk = max(1.5 * atr, entry * .003) if atr else 0
    history = features(candles)
    chart = [{"time":c.time.isoformat(), "open":c.open,"high":c.high,"low":c.low,"close":c.close,"volume":c.volume,"ema":x["ema"],"vwap":x["vwap"]} for c,x in zip(candles,history)][-90:]
    return {
        "decision":decision,"score":score,"passed":passed,"rules":rules,"blockers":blockers,"exit_reasons":exit_reasons,
        "price":price,"ema":f.get("ema"),"vwap":vwap,"rvol":f.get("rvol"),"buy_share":share,
        "spread_bps":spread,"retention":retention,"buy_volume":buy,"sell_volume":sell,"trade_count":len(recent),
        "book_samples":len(observed),"data_time":book.get("time").isoformat() if book.get("time") else None,
        "plan":{"entry":entry,"stop":max(0,entry-risk),"target":entry+2*risk,"risk_per_share":risk} if risk and decision=="LONG" else None,
        "chart":chart,"book":{"bids":book.get("bids",[])[:8],"asks":book.get("asks",[])[:8]},
        "updated_at":now.isoformat(),
    }


STRATEGIES = {
    "trend":"Тренд EMA 50",
    "confirmed":"Тренд + VWAP + объём",
    "breakout":"Пробой 20 свечей + объём",
    "confirmed_once":"Подтверждение · один вход в день",
    "confirmed_hold":"Подтверждение · до 32 свечей",
}


def candle_signal(candle, f, strategy):
    trend = candle.close > f["ema"] and f["rising"]
    if strategy == "trend": return trend
    if strategy in ("confirmed", "confirmed_once", "confirmed_hold"):
        return trend and f["vwap"] is not None and candle.close > f["vwap"] and f["rvol"] >= 1.3
    return f["breakout"] and f["rvol"] >= 1.3


def simulate(candles, strategy, start, end, fee_bps=5, slippage_bps=5, *, feature_rows=None,
             curve_limit=80, stress_costs=None):
    """Next-open entry, conservative stop-first OHLC fills; fixed rules, no fitting."""
    fs = features(candles) if feature_rows is None else feature_rows
    equity, peak, dd, trades, position = 1.0, 1.0, 0.0, [], None
    last_entry_day = None
    one_entry_per_day = strategy in ("confirmed_once", "confirmed_hold")
    max_hold = 32 if strategy == "confirmed_hold" else 12
    fee, slip = fee_bps/10000, slippage_bps/10000
    profits, losses, stress_equity = 0.0, 0.0, 1.0
    stress_fee, stress_slip = (value/10000 for value in stress_costs) if stress_costs is not None else (fee, slip)
    curve = []
    for i in range(max(100,start), end):
        c = candles[i]
        signaled = candle_signal(candles[i-1], fs[i-1], strategy)
        previous_signaled = candle_signal(candles[i-2], fs[i-2], strategy) if one_entry_per_day and i >= 2 else False
        entry_day = c.time.astimezone(MSK).date()
        if (position is None and signaled and
                (not one_entry_per_day or (not previous_signaled and entry_day != last_entry_day))):
            entry = c.open*(1+slip)
            risk = max(fs[i-1]["atr"]*1.5,entry*.003)
            position = {"entry":entry,"entry_quote":c.open,"stop":entry-risk,"target":entry+2*risk,"index":i,"time":c.time}
            last_entry_day = entry_day
        if position is not None:
            p = position
            out, reason = None, ""
            if c.open <= p["stop"]: out, reason = c.open, "gap_stop"
            elif c.open >= p["target"]: out, reason = p["target"], "gap_target"
            elif c.low <= p["stop"]: out, reason = p["stop"], "stop"
            elif c.high >= p["target"]: out, reason = p["target"], "target"
            elif i-p["index"]>=max_hold-1 or i==end-1: out, reason = c.close, "time"
            if out is not None:
                net = out*(1-slip)*(1-fee)/(p["entry"]*(1+fee))-1
                pnl = equity*net
                profits += max(0,pnl)
                losses += max(0,-pnl)
                equity *= 1+net
                # Reprice identical observed entry/exit quotes. Changing a fill
                # assumption must not change the strategy path in a cost-only test.
                stress_equity *= out*(1-stress_slip)*(1-stress_fee)/(p["entry_quote"]*(1+stress_slip)*(1+stress_fee))
                trades.append({"entry_time":p["time"].isoformat(),"exit_time":c.time.isoformat(),"return_pct":net*100,"reason":reason})
                position = None
        marked = equity if position is None else equity*c.close*(1-slip)*(1-fee)/(position["entry"]*(1+fee))
        peak = max(peak,marked)
        dd = max(dd,(peak-marked)/peak)
        if curve_limit != 0:
            curve.append({"time":c.time.isoformat(),"equity":marked})
    result = {"trades":len(trades),"return_pct":(equity-1)*100,"max_drawdown_pct":dd*100,
            "win_rate":sum(t["return_pct"]>0 for t in trades)/len(trades)*100 if trades else None,
            "profit_factor":profits/losses if losses else None,"recent_trades":trades[-8:],
            "curve":curve if curve_limit is None else [
                {"time":point["time"],"equity":round(point["equity"],6)}
                for point in curve[::max(1,len(curve)//max(1,curve_limit))]]}
    if stress_costs is not None:
        result["stress_return_pct"] = (stress_equity-1)*100
    return result


def buy_hold_return(candles, start, end, fee_bps, slippage_bps):
    """One purchase at the first open and sale at the final close of this window."""
    fee, slip = fee_bps / 10000, slippage_bps / 10000
    return (candles[end-1].close * (1-slip) * (1-fee) /
            (candles[start].open * (1+slip) * (1+fee)) - 1) * 100


def chronological_validation(candles, fee_bps=5, slippage_bps=5, *, feature_rows=None):
    """Four fixed-rule evaluation windows with expanding causal indicator history.

    There is no parameter selection or fitting. Each evaluation window starts flat
    and liquidates at its end; the combined result compounds these window returns.
    Previously inspected history is not an independent prospective experiment.
    """
    window_count, min_window_bars = 4, 100
    context_count = max(100, len(candles) // 5)
    result = {
        "status":"insufficient", "windows":[], "strategies":[],
        "min_window_bars":min_window_bars, "required_bars":500,
        "fee_bps":fee_bps, "slippage_bps":slippage_bps,
        "stress_fee_bps":fee_bps*2, "stress_slippage_bps":slippage_bps*2,
        "method":"Четыре последовательных проверочных участка, минимум 100 свечей в каждом. "
                 "Первый контекст — 20% истории, но не менее 100 свечей; перед каждым участком "
                 "индикаторы используют всю предшествующую историю. Параметры не подбираются. "
                 "Каждый участок начинается без позиции, в конце позиция закрывается. "
                 "Итог — последовательное перемножение результатов участков; просадка учитывает "
                 "все закрытия свечей. Стресс-проверка пересчитывает те же входы и выходы "
                 "с удвоенными комиссией и проскальзыванием на каждой стороне, сохраняя "
                 "моменты сделок, стопы и цели основного расчёта. Это повторная проверка "
                 "известной истории, а не независимое подтверждение на будущих котировках.",
        "verdict_method":"Менее 30 сделок суммарно или менее 5 на одном участке — мало наблюдений. "
                         "Иначе убыток суммарно означает отрицательный результат. Для предварительно "
                         "положительной оценки нужны минимум 3 прибыльных участка и положительный "
                         "итог при удвоенных расходах. Эти пороги описательны и не доказывают "
                         "статистическую значимость или превосходство над удержанием акции.",
    }
    if len(candles)-context_count < window_count*min_window_bars:
        result["message"] = "Для четырёх проверочных участков нужно минимум 500 закрытых 15-минутных свечей."
        return result
    fs = features(candles) if feature_rows is None else feature_rows
    edges = [context_count + (len(candles)-context_count)*i//window_count
             for i in range(window_count+1)]
    for index, (start, end) in enumerate(zip(edges, edges[1:]), 1):
        result["windows"].append({
            "index":index, "start":candles[start].time.isoformat(),
            "end":candles[end-1].time.isoformat(), "start_index":start,
            "end_index":end, "history_count":start, "bars":end-start,
            "buy_hold_pct":buy_hold_return(candles,start,end,fee_bps,slippage_bps),
            "cash_pct":0,
        })
    for key, name in STRATEGIES.items():
        windows = []
        equity, stress_equity, peak, drawdown = 1.0, 1.0, 1.0, 0.0
        for window in result["windows"]:
            start, end = window["start_index"], window["end_index"]
            normal = simulate(candles,key,start,end,fee_bps,slippage_bps,
                              feature_rows=fs,curve_limit=None,
                              stress_costs=(fee_bps*2,slippage_bps*2))
            for point in normal["curve"]:
                marked = equity*point["equity"]
                peak = max(peak,marked)
                drawdown = max(drawdown,(peak-marked)/peak)
            equity *= 1+normal["return_pct"]/100
            stress_equity *= 1+normal["stress_return_pct"]/100
            windows.append({
                "return_pct":normal["return_pct"], "trades":normal["trades"],
                "max_drawdown_pct":normal["max_drawdown_pct"],
                "profit_factor":normal["profit_factor"],
                "buy_hold_pct":window["buy_hold_pct"],
                "stress_return_pct":normal["stress_return_pct"],
            })
        positive_windows = sum(window["return_pct"] > 0 for window in windows)
        total_trades = sum(window["trades"] for window in windows)
        total_return, stress_return = (equity-1)*100, (stress_equity-1)*100
        if total_trades < 30 or any(window["trades"] < 5 for window in windows):
            verdict = "needs_evidence"
        elif total_return <= 0:
            verdict = "negative"
        elif positive_windows < 3 or stress_return <= 0:
            verdict = "mixed"
        else:
            verdict = "positive_unproven"
        result["strategies"].append({
            "key":key, "name":name, "windows":windows,
            "positive_windows":positive_windows, "total_windows":window_count,
            "total_trades":total_trades, "return_pct":total_return,
            "max_drawdown_pct":drawdown*100, "stress_return_pct":stress_return,
            "verdict":verdict,
        })
    result.update(status="ok", context_start=candles[0].time.isoformat(),
                  start=candles[edges[0]].time.isoformat(), end=candles[-1].time.isoformat())
    return result


def backtest(candles, fee_bps=5, slippage_bps=5):
    if len(candles)<200:
        return {"status":"insufficient", "message":"Для сравнения нужно минимум 200 закрытых 15-минутных свечей.",
                "validation":chronological_validation(candles,fee_bps,slippage_bps)}
    split = 100+int((len(candles)-100)*.7)
    fs = features(candles)
    rows = []
    for key,name in STRATEGIES.items():
        rows.append({"key":key,"name":name,"development":simulate(candles,key,100,split,fee_bps,slippage_bps,feature_rows=fs),
                     "holdout":simulate(candles,key,split,len(candles),fee_bps,slippage_bps,feature_rows=fs)})
    return {"status":"ok","strategies":rows,"count":len(candles),"fee_bps":fee_bps,"slippage_bps":slippage_bps,
            "start":candles[0].time.isoformat(),"holdout_start":candles[split].time.isoformat(),"end":candles[-1].time.isoformat(),
            "buy_hold_pct":buy_hold_return(candles,split,len(candles),fee_bps,slippage_bps),"cash_pct":0,
            "validation":chronological_validation(candles,fee_bps,slippage_bps,feature_rows=fs),
            "method":"Первые 100 свечей — прогрев. Затем 70% истории для сравнения, последние 30% — отдельная проверка. Вход на открытии следующей свечи, выход по стопу 1,5 ATR, цели 2R или через 12 свечей. Два новых варианта входят только при новом появлении условий и не чаще раза в день; один из них допускает выход через 32 свечи. Если внутри свечи достигнуты стоп и цель — первым считается стоп. Все расходы применяются на входе и выходе. Размер позиции — условные 100% капитала, без плеча; это модель для сравнения, не рекомендуемый размер сделки.",
            "limitation":"Тест только свечных правил. Лента сделок и прежние стаканы здесь НЕ проверены. Прибыль на этом участке не доказывает будущую прибыль; меньше 30 сделок — малая выборка."}
