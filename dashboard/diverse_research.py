"""A fixed exploratory LONG registry. Closed candles only, no order endpoints.

The 24 rules are hypotheses, not trained probabilities. Cross-sectional context
uses exact matching timestamps and excludes the instrument being evaluated.
"""
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from statistics import mean, median

from .signals import MSK, features
from .method_study import validate_candles

VERSION = 'diverse_long_v1'
TICKERS = ('SBER', 'GAZP', 'LKOH', 'NVTK', 'GMKN', 'ROSN', 'YDEX')
FAMILIES = {
    'compression': 'Пробой после затишья',
    'false_low': 'Ложный пробой минимума',
    'exhaustion': 'Отскок после всплеска продаж',
    'quiet_pullback': 'Тихий откат в растущем тренде',
    'relative_leader': 'Сильная акция на растущем рынке',
    'laggard_recovery': 'Отстающая акция начинает догонять',
    'gap_repair': 'Восстановление после утреннего разрыва вниз',
    'efficient_trend': 'Направленный тренд без сильного шума',
}
VARIANTS = {f'{family}_{hold}': {'family': family, 'hold': hold}
            for family in FAMILIES for hold in (4, 8, 16)}
PERIODS = {'summer': ('2026-07-01', '2026-09-01'),
           'september': ('2026-09-01', '2026-10-01'),
           'october_early': ('2026-10-01', '2026-10-13'),
           'october_later': ('2026-10-13', '2026-10-17')}
FRESH = ('2026-10-19', '2026-10-21')
RULES = {
    'entry': 'next contiguous bar open; weekday decision closes 10:30..14:30 MSK',
    'hold': '4/8/16 contiguous positive-volume same-session bars (1/2/4h)',
    'stop': '3 ATR14 known at signal close, minimum 0.5% entry; gap open or stop-first low',
    'regular_session': '10:00..18:30 MSK, weekdays; no overnight positions',
    'seasonal_volume': 'same opening HH:MM, previous 20 weekdays, at least 5 observations; excludes today',
    'peers': 'median return over 4 bars, exact current timestamp, >=4 OTHER stocks, no forward filling',
    'compression': 'previous 7-bar range <55% previous 20 range; close > prior7 high, seasonal volume>=1.2, above rising EMA50',
    'false_low': 'current low below prior20 low, close reclaims it in top quarter, green candle',
    'exhaustion': 'prior red body>=1 ATR14 and seasonal volume>=2; current green close>prior midpoint in top half',
    'quiet_pullback': 'above rising EMA50, preceding 4-bar return<0, previous seasonal volume<=1, close>previous high',
    'relative_leader': 'own 1h return>peer median+0.15pp, peer median>0, >=60% peers positive, green candle, above EMA50',
    'laggard_recovery': 'own 1h return<peer median-0.3pp, peer median>0, close>previous high, green candle',
    'gap_repair': '10:00 open below prior weekday regular close by>=0.3%; new upward crossing of gap midpoint before12:00',
    'efficient_trend': '20-bar efficiency>=0.55, three increasing closes, close>prior8 high, above rising EMA50',
    'tie_break': 'alphabetical ticker; no future-return ranking; no multiple entries per ticker before full horizon',
    'costs': '0 / 5bps fee+5bps slip each side / double costs; zero costs diagnostic only',
    'selection': 'screen needs >=40 trades total, >=3 each old fold, no unknowns, positive BASE and DOUBLE in ALL four folds; rank by worst double-cost fold then sum, alphabetical tie',
    'fresh_check': 'lock one audit candidate before reading Oct19..20; if none passes screen, audit highest ranked sufficiently sampled failure, NOT an approved strategy; never select on fresh results',
    'limitations': 'old dates repeatedly studied; survivor universe; candle-only; assumed costs, no historical L2, dividends, taxes or real fill validation; two new sessions not proof',
    'entry_allowed': False,
}


def feature_rows(candles):
    validate_candles(candles)
    base, result = features(candles), []
    clocks = defaultdict(lambda: deque(maxlen=20))
    session, day_bars, regular_close, prior_close = None, [], None, None
    regular_history = deque(maxlen=20)
    for i, c in enumerate(candles):
        local = c.time.astimezone(MSK)
        minute = local.hour * 60 + local.minute
        regular = local.weekday() < 5 and 600 <= minute < 1110
        if local.date() != session:
            if regular_close is not None:
                prior_close = regular_close
            session, day_bars, regular_close = local.date(), [], None
        f = {**base[i], 'seasonal_volume': None, 'peer_return': None,
             'peer_breadth': None, 'return4': None, 'flags': dict.fromkeys(FAMILIES, False)}
        if not regular:
            result.append(f)
            continue
        history = clocks[minute]
        if len(history) >= 5 and median(history) > 0:
            f['seasonal_volume'] = c.volume / median(history)
        history.append(c.volume)
        regular_close = c.close
        if day_bars and c.time - day_bars[-1].time != timedelta(minutes=15):
            day_bars = []
        day_bars.append(c)
        prior = list(regular_history)
        regular_history.append(c)
        if len(day_bars) >= 5 and all(b.volume > 0 for b in day_bars[-5:]):
            f['return4'] = c.close/day_bars[-5].close - 1
        if i >= 100 and c.volume > 0 and base[i-1]['atr'] > 0:
            p, atr = candles[i-1], base[i-1]['atr']
            # Range/efficiency use the previous 20 observed regular-session
            # bars, possibly across a night. Entry/exit paths must instead be
            # fully contiguous; an overnight gap is never an assumed fill.
            continuous = len(prior) == 20 and all(b.volume > 0 for b in prior)
            span = c.high-c.low
            upper_quarter = span > 0 and (c.close-c.low)/span >= .75
            green, trend = c.close > c.open, c.close > f['ema'] and f['rising']
            adjacent = c.time-p.time == timedelta(minutes=15) and len(day_bars) >= 2
            if continuous:
                high20, low20 = max(b.high for b in prior), min(b.low for b in prior)
                high7, low7 = max(b.high for b in prior[-7:]), min(b.low for b in prior[-7:])
                f['flags']['compression'] = bool(high20 > low20 and high7-low7 < .55*(high20-low20)
                    and c.close > high7 and (f['seasonal_volume'] or 0) >= 1.2 and trend)
                f['flags']['false_low'] = c.low < low20 < c.close and upper_quarter and green
                closes = [b.close for b in prior]+[c.close]
                distance = sum(abs(b-a) for a,b in zip(closes,closes[1:]))
                efficiency = abs(c.close-prior[0].close)/distance if distance else 0
                f['flags']['efficient_trend'] = bool(efficiency >= .55 and trend
                    and c.close > max(b.high for b in prior[-8:])
                    and c.close > prior[-1].close > prior[-2].close > prior[-3].close)
            if adjacent:
                prev = result[-1]
                f['flags']['exhaustion'] = bool(p.open-p.close >= atr
                    and (prev['seasonal_volume'] or 0) >= 2 and green
                    and c.close > (p.open+p.close)/2 and span > 0 and c.close >= (c.high+c.low)/2)
                f['flags']['quiet_pullback'] = bool(trend and prev['return4'] is not None
                    and prev['return4'] < 0 and prev['seasonal_volume'] is not None
                    and prev['seasonal_volume'] <= 1 and c.close > p.high)
                # Missing 10:00 bar means there is no defensible opening gap.
                opening = day_bars[0]
                if opening.time.astimezone(MSK).hour == 10 and opening.time.minute == 0 and prior_close:
                    midpoint = (opening.open+prior_close)/2
                    f['flags']['gap_repair'] = bool(minute < 720 and opening.open/prior_close-1 <= -.003
                        and p.close <= midpoint < c.close)
        result.append(f)
    return result


def add_peer_context(universe):
    """Universe maps ticker -> (candles, causal features). No nearest-time match."""
    by_time = defaultdict(dict)
    for ticker, (candles, rows) in universe.items():
        if len(candles) != len(rows):
            raise ValueError('Feature length mismatch')
        for c, f in zip(candles, rows):
            if f['return4'] is not None:
                by_time[c.time][ticker] = f['return4']
    for ticker, (candles, rows) in universe.items():
        for i, (c, f) in enumerate(zip(candles, rows)):
            peers = [v for other, v in by_time[c.time].items() if other != ticker]
            if len(peers) < 4 or f['return4'] is None or i < 100:
                continue
            benchmark, breadth = median(peers), mean(v > 0 for v in peers)
            f['peer_return'], f['peer_breadth'] = benchmark, breadth
            green = c.close > c.open
            f['flags']['relative_leader'] = bool(f['return4'] > benchmark+.0015
                and benchmark > 0 and breadth >= .6 and green and c.close > f['ema'])
            f['flags']['laggard_recovery'] = bool(f['return4'] < benchmark-.003
                and benchmark > 0 and green and c.close > candles[i-1].high)


def replay_rows(candles, rows, ticker, lot, variant):
    policy = VARIANTS[variant]
    hold, family = policy['hold'], policy['family']
    for i in range(100, len(candles)):
        c, f = candles[i], rows[i]
        at = c.time+timedelta(minutes=15)
        local = at.astimezone(MSK)
        if (local.weekday() >= 5 or not 630 <= local.hour*60+local.minute <= 870
                or not f['flags'][family]):
            continue
        end = at+timedelta(minutes=15*hold)
        future = candles[i+1:i+1+hold]
        entry = future[0].open if future and future[0].time == at and future[0].volume > 0 else None
        stop = entry-max(3*f['atr'], .005*entry) if entry else None
        outcome = None
        valid = (len(future) == hold and stop is not None and stop > 0
                 and all(b.volume > 0 and b.time == at+timedelta(minutes=15*j)
                     and b.time.astimezone(MSK).date() == local.date()
                     and .6 <= b.open/candles[i+j].close <= 1.4 for j, b in enumerate(future)))
        if valid:
            out, exit_at, reason = future[-1].close, end, 'horizon'
            for b in future:
                if b.open <= stop:
                    out, exit_at, reason = b.open, b.time, 'gap_stop'
                    break
                if b.low <= stop:
                    out, exit_at, reason = stop, b.time+timedelta(minutes=15), 'stop'
                    break
            outcome = {'entry': entry, 'exit': out, 'stop': stop, 'reason': reason,
                       'exit_at': exit_at.astimezone(timezone.utc).isoformat(),
                       'label_end': end.astimezone(timezone.utc).isoformat()}
        yield {'ticker': ticker, 'lot': lot, 'at': at.astimezone(timezone.utc).isoformat(),
               'label_end': end.astimezone(timezone.utc).isoformat(), 'entry': entry,
               'stop': stop if stop and stop > 0 else None, 'outcome': outcome,
               'selected': True, 'expected_net_pct': 0.}


def in_period(row, bounds):
    first, last = bounds
    return (first <= datetime.fromisoformat(row['at']).astimezone(MSK).date().isoformat() < last
            and datetime.fromisoformat(row['label_end']).astimezone(MSK).date().isoformat() < last)


def choose(results):
    """Only old folds enter this function. New-period prices are not an input."""
    summaries = {}
    for key, folds in results.items():
        base = [v['base'] for v in folds.values()]
        stress = [v['double_cost'] for v in folds.values()]
        enough = sum(v['trades'] for v in base) >= 40 and all(v['trades'] >= 3 for v in base)
        verified = all(v['return_pct'] is not None for v in base+stress)
        passes = enough and verified and all(v['return_pct'] > 0 for v in base+stress)
        # Unknown results cannot outrank observed results, even when they would
        # look attractive if the missing paths were silently dropped.
        ranked = verified and enough
        summaries[key] = {'passes_screen': passes, 'sufficient_sample': enough,
                          'rankable': ranked, 'trades': sum(v['trades'] for v in base),
                          'worst_stress_pct': min(v['return_pct'] for v in stress) if verified else None,
                          'sum_stress_pct': sum(v['return_pct'] for v in stress) if verified else None}
    order = sorted((k for k, v in summaries.items() if v['rankable']),
                   key=lambda k: (-int(summaries[k]['passes_screen']), -summaries[k]['worst_stress_pct'],
                                  -summaries[k]['sum_stress_pct'], k))
    selected = order[0] if order else None
    return {'audit_candidate': selected, 'passed': [k for k in order if summaries[k]['passes_screen']],
            'summaries': summaries, 'entry_allowed': False}
