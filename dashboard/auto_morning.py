"""Per-stock next-morning research estimates from completed exchange candles."""
from collections import defaultdict
from datetime import timedelta, timezone
from statistics import mean, stdev

import numpy as np

from .market_provider import MarketUnavailable, open_client, resolve
from .market_stream_provider import exchange_candles
from .morning_model import MSK, clock, validate

BAR = timedelta(minutes=15)
SLOTS = ((9, 30), (10, 30), (13, 0), (17, 45))
HISTORY_DAYS = 210
MAX_GAP = 5


def next_morning(response, day):
    calendar = {}
    for exchange in response.exchanges:
        if exchange.exchange == "MOEX":
            for row in exchange.days:
                if row.date.date() in calendar:
                    raise MarketUnavailable("Расписание биржи неоднозначно. Попробуй ещё раз.")
                calendar[row.date.date()] = row
    for offset in range(1, MAX_GAP + 1):
        date = day + timedelta(days=offset)
        row = calendar.get(date)
        if row is None:
            break
        if row.is_trading_day is False:
            continue
        target = clock(date, 10)
        start, end = row.start_time, row.end_time
        clearing_start, clearing_end = row.clearing_start_time, row.clearing_end_time
        if (row.is_trading_day is True and start and end and start <= target
                and target + BAR <= end and not (clearing_start and clearing_end
                and clearing_start < target + BAR and target < clearing_end)):
            return target
        break
    raise MarketUnavailable("Биржа не подтвердила ближайшую сессию на 10:00 МСК.")


def samples(candles, slot, cutoff):
    validate(candles)
    if any(bar.time + BAR > cutoff for bar in candles):
        raise ValueError("Unclosed or future candle")
    sessions = defaultdict(dict)
    for bar in candles:
        local = bar.time.astimezone(MSK)
        # Ignore late/early special sessions that cannot supply this forecast's features.
        if local.hour >= 9 and local.hour < 18:
            sessions[local.date()][local] = bar
    days = sorted(sessions)
    past = []
    examples = []
    current = None
    for index, day in enumerate(days):
        at = clock(day, *slot)
        start = clock(day, 9, 15) if slot == SLOTS[0] else clock(day, 10)
        bars = [sessions[day].get(start + BAR*i) for i in range((at-start)//BAR)]
        if not bars or any(bar is None or bar.volume <= 0 for bar in bars):
            continue
        price = bars[-1].close
        volume = sum(bar.volume for bar in bars)
        if len(past) >= 10:
            before = past[-10:]
            change = lambda after, earlier: after / earlier - 1
            x = [
                change(price, before[-1][0]),
                change(price, before[-5][0]),
                change(price, mean(p for p, _ in before)),
                change(price, bars[0].open),
                change(price, bars[-1].open),
                (max(b.high for b in bars) - min(b.low for b in bars)) / price,
                change(volume, mean(v for _, v in before)),
                stdev(change(b[0], a[0]) for a, b in zip(before, before[1:])),
            ]
            if day == cutoff.date() and at == cutoff:
                current = (price, x)
            if index + 1 < len(days):
                target_day = days[index + 1]
                gap = (target_day - day).days
                target = clock(target_day, 10)
                actual = sessions[target_day].get(target)
                if actual and actual.volume > 0 and 1 <= gap <= MAX_GAP:
                    examples.append((at, target + BAR, price, actual.open, [*x, gap]))
        past.append((price, volume))
    return examples, current


def estimate(candles, cutoff, target):
    rows, current = samples(candles, (cutoff.hour, cutoff.minute), cutoff)
    if current is None:
        raise MarketUnavailable("Не хватает завершённых свечей для этого времени. Попробуй позже.")
    split = int(len(rows)*.8)
    test = rows[split:]
    if len(test) < 20:
        raise MarketUnavailable("Недостаточно истории для отдельной проверки прогноза по этой акции.")
    train = [row for row in rows[:split] if row[1] < test[0][0]]
    if len(train) < 60:
        raise MarketUnavailable("Недостаточно истории для обучения по этой акции.")
    x = np.asarray([row[4] for row in train])
    y = np.asarray([row[3]/row[2]-1 for row in train])
    center = x.mean(axis=0)
    scale = x.std(axis=0)
    scale[scale < 1e-10] = 1

    def features(values):
        return np.insert(np.clip((np.asarray(values)-center)/scale, -5, 5), 0, 1, axis=-1)

    z = features(x)
    penalty = np.eye(z.shape[1])*20
    penalty[0, 0] = 0
    weights = np.linalg.solve(z.T@z + penalty, z.T@y)
    price, current_x = current
    prediction = price*(1 + float(features([*current_x, (target.date()-cutoff.date()).days])@weights))
    if not np.isfinite(prediction) or prediction <= 0:
        raise MarketUnavailable("Прогноз не прошёл проверку. Попробуй позже.")
    errors = [abs(price0*(1 + float(features(x0)@weights))-actual) for _, _, price0, actual, x0 in test]
    baseline = [abs(price0-actual) for _, _, price0, actual, _ in test]
    return {
        "as_of": cutoff.isoformat(), "target_at": target.isoformat(), "anchor_price": price,
        "predicted_price": prediction,
        "quality": {
            "train": len(train), "test": len(test), "from": test[0][1].date().isoformat(),
            "to": test[-1][1].date().isoformat(), "mae_rub": mean(errors), "baseline_mae_rub": mean(baseline),
            "mape": mean(100*error/row[3] for error, row in zip(errors, test)),
            "baseline_mape": mean(100*error/row[3] for error, row in zip(baseline, test)),
        },
    }


def calculate(ticker, now, progress, cached_history):
    from t_tech.invest.schemas import CandleInterval

    local = now.astimezone(MSK)
    day = local.date()
    midnight = clock(day, 0)
    interval = CandleInterval.CANDLE_INTERVAL_15_MIN
    with open_client() as client:
        instrument = resolve(client, ticker)
        response = client.instruments.trading_schedules(
            exchange="MOEX", from_=now,
            to=now + timedelta(days=MAX_GAP+1),
        )
        target = next_morning(response, day)
        today_rows = [d for e in response.exchanges if e.exchange == "MOEX"
                      for d in e.days if d.date.date() == day]
        if len(today_rows) != 1:
            raise MarketUnavailable("Расписание на сегодня недоступно. Попробуй ещё раз.")
        today_is_open = today_rows[0].is_trading_day
        if today_is_open and local < clock(day, *SLOTS[0]):
            raise MarketUnavailable("Первый расчёт доступен после 09:30 МСК. Попробуй позже.")
        uid = instrument["uid"]
        past = cached_history(uid, midnight, lambda start, end: exchange_candles(
            client, uid, start.astimezone(timezone.utc), end.astimezone(timezone.utc), interval, 15))
        today = exchange_candles(client, uid, midnight.astimezone(timezone.utc), now, interval, 15)
    progress("analyzing")
    bars = past + today
    available = {b.time.astimezone(MSK): b.volume for b in bars[-600:]}
    anchors = [day] if today_is_open else [
        day-timedelta(days=n) for n in range(MAX_GAP)
        if (target.date()-day).days + n <= MAX_GAP
    ]
    cutoffs = [clock(date, *slot) for date in anchors for slot in reversed(SLOTS)]
    for cutoff in cutoffs:
        start = clock(cutoff.date(), 9, 15) if (cutoff.hour, cutoff.minute) == SLOTS[0] else clock(cutoff.date(), 10)
        if cutoff > local or any(not available.get(start + BAR*i) for i in range((cutoff-start)//BAR)):
            continue
        progress("forecasting")
        result = estimate([bar for bar in bars if bar.time + BAR <= cutoff], cutoff, target)
        return {**result, "instrument": instrument}
    raise MarketUnavailable("Не удалось загрузить свежие свечи для прогноза. Попробуй ещё раз позже.")
