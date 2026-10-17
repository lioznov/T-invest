"""Prefer recorded EXCHANGE stream; old provider retained as blocked fallback.

Frozen historical adapters and six-rule mathematics are not modified.
"""
from datetime import datetime, timedelta, timezone
import math
from pathlib import Path
from django.conf import settings

from .market_provider import open_client, resolve, number, MarketUnavailable
from .market_provider import live_snapshot as legacy_snapshot
from .method_study import validate_candles
from .signals import Candle, MSK
from .stream_inputs import stream_window


VERSION = "stream_exchange_v1"


def exchange_candles(client, uid, start, end, interval, minutes):
    from t_tech.invest.schemas import CandleSource
    values, cursor = {}, start
    while cursor < end:
        until = min(end, cursor+timedelta(days=7 if minutes == 15 else 1))
        response = client.market_data.get_candles(instrument_id=uid, from_=cursor, to=until,
            interval=interval, candle_source_type=CandleSource.CANDLE_SOURCE_EXCHANGE)
        for c in response.candles:
            if not c.is_complete or not cursor <= c.time < until or c.time+timedelta(minutes=minutes) > end:
                continue
            source = getattr(c, "candle_source", getattr(c, "candle_source_type", None))
            if getattr(source, "name", None) != "CANDLE_SOURCE_EXCHANGE":
                raise MarketUnavailable("API не подтвердил биржевой источник свечей; смешанная история не используется для входа.")
            value = Candle(c.time, *(number(getattr(c, k)) for k in ("open", "high", "low", "close")), c.volume)
            if c.time in values and values[c.time] != value:
                raise MarketUnavailable("Противоречивые свечи API; оценка заблокирована.")
            values[c.time] = value
        cursor = until
    result = [values[t] for t in sorted(values)]
    if minutes == 15:
        validate_candles(result)
    elif minutes == 1:
        for c in result:
            if (c.time.second or c.time.microsecond or type(c.volume) is not int or c.volume < 0
                    or not all(math.isfinite(p) and p > 0 for p in (c.open, c.high, c.low, c.close))
                    or not c.low <= min(c.open, c.close) <= max(c.open, c.close) <= c.high):
                raise MarketUnavailable("Некорректные минутные свечи API; оценка заблокирована.")
    else:
        raise ValueError("Unsupported candle interval")
    return result


def live_snapshot(ticker, previous=None, now=None):
    now = now or datetime.now(timezone.utc)
    previous = previous or {}
    from .continuous_watch import load_health
    root = Path(settings.BASE_DIR)/"data"/"watch"
    health = load_health(root, now)
    if not health.get("alive") or ticker not in health.get("tickers", []):
        snapshot = legacy_snapshot(ticker, previous if previous.get("provider_version") != VERSION else None, now)
        snapshot["provider_version"] = "legacy_unverified_book"
        snapshot["data_quality"] = {"version": "legacy_unverified_book", "action_blocked": True,
            "warning": "Отдельный запрос старого SDK не подтверждает источник/consistency стакана. График доступен, вход по этим данным заблокирован. Запустите watch_market для этой акции."}
        return snapshot
    from t_tech.invest.schemas import CandleInterval
    clean_cache = previous if previous.get("provider_version") == VERSION else {}
    instrument = clean_cache.get("instrument")
    refresh = not clean_cache.get("candles") or (now-clean_cache["candles_fetched"]).total_seconds() >= 60
    if instrument is None or refresh:
        with open_client() as client:
            instrument = instrument or resolve(client, ticker)
            start = now-timedelta(hours=2) if clean_cache.get("candles") else now-timedelta(days=30)
            new = exchange_candles(client, instrument["uid"], start, now, CandleInterval.CANDLE_INTERVAL_15_MIN, 15)
            merged = {c.time: c for c in clean_cache.get("candles", [])+new}
            candles = [merged[t] for t in sorted(merged) if t >= now-timedelta(days=30)]
            day = now.astimezone(MSK).replace(hour=0, minute=0, second=0, microsecond=0)
            minutes = exchange_candles(client, instrument["uid"], day.astimezone(timezone.utc), now,
                                       CandleInterval.CANDLE_INTERVAL_1_MIN, 1)
    else:
        candles, minutes = clean_cache["candles"], clean_cache["minutes"]
    # Quotes are read AFTER possibly slow candle requests, never stamped with a
    # fabricated current exchange time. Receiver and exchange clocks are retained.
    observed_now = datetime.now(timezone.utc)
    stream = stream_window(root, ticker, instrument, observed_now)
    return {**stream, "instrument": instrument, "candles": candles, "minutes": minutes,
            "provider_version": VERSION, "candles_fetched": now if refresh else clean_cache["candles_fetched"]}


def apply_quality_guard(analysis, snapshot):
    quality = snapshot.get("data_quality", {})
    analysis["data_quality"] = quality
    if quality.get("action_blocked"):
        analysis["blockers"].append(quality.get("blocked_reason") or quality.get("warning", "Есть пропуски или непроверенные данные потока; действие заблокировано."))
        analysis["decision"], analysis["plan"] = "WAIT", None
        for key in ("patterns", "candle_patterns"):
            if key in analysis:
                analysis[key]["market_confirmed"] = False
    return analysis
