"""Experimental live display isolated from frozen historical research."""
from datetime import datetime, timedelta, timezone

from .candle_patterns_v3 import NAMES, scan_candle_patterns

EXPLANATIONS = {
    "engulf_pullback": "После красной свечи появляется зелёная, перекрывающая её тело возле средней цены. Это только гипотеза завершения отката.",
    "hammer_confirm": "Свеча с длинной нижней тенью возле поддержки, затем отдельная свеча закрывается выше её максимума. Одна тень ничего не подтверждает.",
    "inside_break": "Маленькая свеча внутри предыдущей; только следующая свеча закрывается выше большого диапазона. Пробой вниз не используется для покупки.",
}


def candle_panel(snapshot, now=None):
    now = now or datetime.now(timezone.utc)
    base = {"version": "candle_patterns_v3", "entry_allowed": False, "status": "insufficient",
            "evaluated_at": None, "items": [], "markers": [], "market_confirmed": False}
    candles = [c for c in snapshot.get("candles", []) if c.time+timedelta(minutes=15) <= now]
    if len(candles) < 100:
        return base
    end = candles[-1].time+timedelta(minutes=15)
    if not timedelta(0) <= now-end <= timedelta(minutes=17):
        return {**base, "status": "stale"}
    try:
        events = scan_candle_patterns(candles)
    except (ValueError, TypeError, KeyError, IndexError):
        return {**base, "status": "invalid"}
    items = [{"key": key, "name": name, "explanation": EXPLANATIONS[key],
              "detected": events[key][-1] is not None,
              "signal_time": events[key][-1]["time"] if events[key][-1] else None,
              "level": events[key][-1]["level"] if events[key][-1] else None} for key, name in NAMES.items()]
    markers = [event for values in events.values() for event in values[-90:] if event is not None]
    return {**base, "status": "ok", "evaluated_at": end.isoformat(), "items": items,
            "markers": sorted(markers, key=lambda row: (row["time"], row["key"]))[-40:]}
