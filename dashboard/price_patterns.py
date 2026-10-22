"""Causal OHLCV events, not pictures classified using future pivots.

All three events are confirmed on a completed candle. Order-book confirmation
is a separate live observation, never invented from historical OHLCV.
"""
from datetime import timedelta
from statistics import mean

from .signals import MSK, features
from .method_study import validate_candles


VERSION = "price_patterns_v1"
NAMES = {"reclaim": "Возврат после ложного пробоя",
         "orb_retest": "Пробой утреннего диапазона и ретест",
         "flag": "Рост → спокойный откат → продолжение"}
EXPLANATIONS = {
    "reclaim": "Цена опустилась под ранее известную поддержку, но за три свечи вернулась выше неё. Это гипотеза окончания давления продавцов.",
    "orb_retest": "Цена вышла выше диапазона 10:00–11:00 МСК, проверила уровень сверху и только отдельной следующей свечой подтвердила восстановление.",
    "flag": "После трёх свечей роста последовали 2–4 свечи небольшого отката с меньшим объёмом; цена закрылась выше максимума этого отката.",
}


def scan_patterns(candles, rows=None):
    validate_candles(candles)
    rows = features(candles) if rows is None else rows
    if len(rows) != len(candles):
        raise ValueError("Mismatched pattern features")
    result = {key: [None]*len(candles) for key in NAMES}
    spring, orb, opening, opening_high, session = None, None, [], None, None

    def event(key, i, level, floor, reference_atr):
        result[key][i] = {"key": key, "name": NAMES[key], "time": candles[i].time.isoformat(),
                          "level": float(level), "floor": float(floor), "atr": float(reference_atr),
                          "price": candles[i].close, "rvol": rows[i]["rvol"]}

    for i, c in enumerate(candles):
        local = c.time.astimezone(MSK)
        day, minute = local.date(), local.hour*60+local.minute
        adjacent = i > 0 and c.time-candles[i-1].time == timedelta(minutes=15)
        same_day = i > 0 and candles[i-1].time.astimezone(MSK).date() == day
        if day != session:
            spring, orb, opening, opening_high, session = None, None, [], None, day
        if not adjacent or not same_day:
            spring, orb = None, None
        if local.weekday() >= 5 or not 600 <= minute < 1080:
            continue
        # This is an explicitly registered clock window, not a claim about the
        # historical exchange calendar. Missing opening bars invalidate ORB.
        if 600 <= minute <= 645:
            opening.append(c)
            if minute == 645 and len(opening) == 4 and [b.time.astimezone(MSK).minute for b in opening] == [0, 15, 30, 45]:
                opening_high = max(b.high for b in opening)
        if i < 100:
            continue
        atr = rows[i-1]["atr"]
        if atr <= 0 or c.volume <= 0:
            spring, orb = None, None
            continue
        if spring and i > spring["expires"]:
            spring = None
        if spring is None:
            level = min(b.low for b in candles[i-24:i])
            if level-.5*atr <= c.low < level-.05*atr:
                spring = {"level": level, "floor": c.low, "atr": atr, "expires": i+2}
        if spring:
            spring["floor"] = min(spring["floor"], c.low)
            if spring["floor"] < spring["level"]-.5*spring["atr"]:
                spring = None
            elif c.close > spring["level"]+.05*spring["atr"] and c.close > c.open:
                event("reclaim", i, spring["level"], spring["floor"], spring["atr"])
                spring = None
        if opening_high is not None and minute >= 660:
            if orb and (i > orb["expires"] or c.close < opening_high-.1*orb["atr"]):
                orb = None
            if orb is None and c.close > opening_high+.1*atr and candles[i-1].close <= opening_high+.1*atr:
                orb = {"phase": "retest", "break_index": i, "atr": atr, "expires": i+4}
            elif orb and orb["phase"] == "retest" and i > orb["break_index"]:
                if opening_high-.3*orb["atr"] <= c.low <= opening_high+.1*orb["atr"] and c.close >= opening_high-.1*orb["atr"]:
                    orb.update(phase="confirm", touch_index=i, touch_high=c.high,
                               floor=c.low, expires=i+2)
            elif orb and orb["phase"] == "confirm" and i > orb["touch_index"]:
                orb["floor"] = min(orb["floor"], c.low)
                if c.close > orb["touch_high"] and c.close > c.open and orb["floor"] >= opening_high-.3*orb["atr"]:
                    event("orb_retest", i, opening_high, orb["floor"], orb["atr"])
                    orb = None
        # At most three backward-looking candidate corrections. No future
        # extrema, centered averages or repainting pivots enter the event.
        if not (rows[i]["rising"] and c.close > rows[i]["ema"]):
            continue
        for n in (2, 3, 4):
            first = i-n-3
            block = candles[first:i+1]
            if first < 1 or any(b.time.astimezone(MSK).date() != day for b in block):
                continue
            if any(b.time-a.time != timedelta(minutes=15) for a, b in zip(block, block[1:])):
                continue
            if any(not 600 <= b.time.astimezone(MSK).hour*60+b.time.astimezone(MSK).minute < 1080 for b in block):
                continue
            impulse, correction = candles[first:first+3], candles[first+3:i]
            reference_atr = rows[first-1]["atr"]
            gain = impulse[-1].close-impulse[0].open
            top = max(b.high for b in correction)
            floor = min(b.low for b in correction)
            if (gain >= 1.5*reference_atr and reference_atr > 0
                    and sum(b.close > b.open for b in impulse) >= 2
                    and floor >= impulse[-1].close-.5*gain
                    and top <= max(b.high for b in impulse)+.1*reference_atr
                    and mean(b.volume for b in correction) < mean(b.volume for b in impulse)
                    and c.close > top+.05*reference_atr and c.close > c.open):
                event("flag", i, top, floor, reference_atr)
                break
    return result


def pattern_panel(snapshot, analysis, now):
    """A pattern can be detected but is never an approved trading instruction."""
    closed = [c for c in snapshot.get("candles", []) if c.time+timedelta(minutes=15) <= now]
    base = {"version": VERSION, "entry_allowed": False, "market_confirmed": False,
            "evaluated_at": None, "items": [], "markers": [], "context": [],
            "evidence": {"status": "unvalidated", "message": "Прибыльность паттернов и добавочная польза стакана не подтверждены независимыми наблюдениями."}}
    try:
        events = scan_patterns(closed)
    except (ValueError, TypeError, KeyError, IndexError):
        return {**base, "status": "invalid", "message": "Некорректная история свечей. Паттерны не оцениваются."}
    if len(closed) < 100:
        return {**base, "status": "insufficient", "message": "Для анализа паттернов нужны не менее 100 закрытых 15-минутных свечей."}
    end = closed[-1].time+timedelta(minutes=15)
    stale = not timedelta(0) <= now-end <= timedelta(minutes=17)
    rules = {r["key"]: r for r in analysis["rules"]}
    context = [{"name": rules[key]["name"], "pass": rules[key]["pass"], "detail": rules[key]["detail"]}
               for key in ("flow", "spread", "book")]
    # A fresh chart alone is not a fresh tradable market observation.
    market_ok = bool(snapshot.get("trading") and not analysis["blockers"]
                     and all(r["pass"] is True for r in context))
    items = []
    for key, name in NAMES.items():
        current = events[key][-1] if not stale else None
        items.append({"key": key, "name": name, "detected": current is not None,
                      "explanation": EXPLANATIONS[key], "signal_time": current["time"] if current else None,
                      "level": current["level"] if current else None})
    markers = [e for values in events.values() for e in values[-90:] if e is not None] if not stale else []
    markers.sort(key=lambda e: e["time"])
    return {**base, "status": "stale" if stale else "ok", "evaluated_at": end.isoformat(),
            "items": items, "markers": markers[-40:], "context": context,
            "market_confirmed": market_ok and any(item["detected"] for item in items),
            "message": "График устарел: текущий паттерн не подтверждается." if stale else
                       "Паттерн — исследовательская гипотеза, а не команда покупать. Свечи, сделки и стакан проверяются раздельно."}
