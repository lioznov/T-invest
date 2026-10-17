"""Three new fixed, causal long-only hypotheses. No trading/order API.

These are exploratory definitions, not a claim that named candle shapes predict
returns. Closed bars only; a later confirmation can never rewrite an old event.
"""
from datetime import timedelta

from .method_study import validate_candles
from .signals import MSK, features


NAMES = {"engulf_pullback": "Поглощение после отката",
         "hammer_confirm": "Молот у поддержки + подтверждение",
         "inside_break": "Внутренняя свеча + пробой вверх"}
VARIANTS = {f"{key}_{suffix}": {"family": key, "volume": True, "flow": flow,
                                "name": name + (" · покупки ≥ 60%" if flow else " · объём ≥ 1,3")}
            for key, name in NAMES.items() for suffix, flow in (("volume", False), ("buy60", True))}
RULES = {
    "common": "15m closed bars; warmup 100; weekdays 10–18 Moscow; positive volume; contiguous same-session pattern; EMA50 rising and close above it; confirmation rvol >=1.3; next-open execution.",
    "engulf_pullback": "Previous red body >=0.1 prior ATR; current green body >=0.25 prior ATR engulfs it; previous/current low touches prior EMA20 within 0.3 ATR; close above EMA20. Stop below two-bar low.",
    "hammer_confirm": "Previous bar lower wick >=2 body, body >=0.05 prior ATR, upper wick <=body; low within 0.3 ATR of minimum low of preceding 24 bars; next separate green bar closes above hammer high without breaking hammer low. Stop below hammer low.",
    "inside_break": "Mother range >=0.5 prior ATR; next bar strictly inside mother; next separate green bar closes above mother high +0.05 ATR without breaking inside low. Stop below inside low.",
    "buy60": "Additionally API classified volume_buy/(buy+sell) >=0.60 on the completed confirmation bar; missing/inconsistent classification vetoes, never inferred from OHLCV.",
}


def scan_candle_patterns(candles, rows=None):
    validate_candles(candles)
    rows = features(candles) if rows is None else rows
    if len(rows) != len(candles):
        raise ValueError("Mismatched features")
    result = {key: [None]*len(candles) for key in NAMES}
    ema20 = []
    for c in candles:
        ema20.append(c.close if not ema20 else ema20[-1]+2/21*(c.close-ema20[-1]))

    def event(key, i, level, floor, atr):
        result[key][i] = {"key": key, "name": NAMES[key], "time": candles[i].time.isoformat(),
                          "level": float(level), "floor": float(floor), "atr": float(atr),
                          "price": candles[i].close, "rvol": rows[i]["rvol"]}

    def contiguous(block):
        return (all(b.volume > 0 and b.time.astimezone(MSK).weekday() < 5
                    and 600 <= b.time.astimezone(MSK).hour*60+b.time.astimezone(MSK).minute < 1080
                    and b.time.astimezone(MSK).date() == block[-1].time.astimezone(MSK).date()
                    for b in block)
                and all(b.time-a.time == timedelta(minutes=15) for a, b in zip(block, block[1:])))

    for i in range(100, len(candles)):
        c, p, mother = candles[i], candles[i-1], candles[i-2]
        atr = rows[i-1]["atr"]
        if (atr <= 0 or not contiguous([p, c]) or not rows[i]["rising"]
                or c.close <= rows[i]["ema"] or c.close <= c.open):
            continue
        average = ema20[i-1]
        if (p.open-p.close >= .1*atr and c.close-c.open >= .25*atr
                and c.open <= p.close and c.close >= p.open
                and abs(min(p.low, c.low)-average) <= .3*atr and c.close > average):
            event("engulf_pullback", i, p.open, min(p.low, c.low), atr)
        body = abs(p.close-p.open)
        lower, upper = min(p.open, p.close)-p.low, p.high-max(p.open, p.close)
        support = min(b.low for b in candles[i-25:i-1])
        if (body >= .05*atr and lower >= 2*body and upper <= body
                and abs(p.low-support) <= .3*atr and c.close > p.high and c.low >= p.low):
            event("hammer_confirm", i, p.high, p.low, atr)
        if (contiguous([mother, p, c]) and mother.high-mother.low >= .5*atr
                and p.high < mother.high and p.low > mother.low
                and c.close > mother.high+.05*atr and c.low >= p.low):
            event("inside_break", i, mother.high, p.low, atr)
    return result
