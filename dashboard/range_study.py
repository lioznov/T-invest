"""Fixed daily LONG hypotheses: prior extrema, confirmation and causal stops.

Exploratory research only. No broker order methods or trading approval.
"""
from dataclasses import asdict, dataclass
from statistics import mean

from .risk import position_size
from .signals import MSK
from .strategy_search import feature_rows, validate_bars


VERSION = 'range_swing_v1'
TICKERS = ('SBER', 'GAZP', 'LKOH', 'NVTK', 'GMKN', 'ROSN', 'YDEX')
RESERVES = ('TATN', 'CHMF', 'PHOR')
PERIODS = {'development': ('2023-10-02', '2026-07-01'),
           'selection': ('2026-07-01', '2026-09-01'),
           'recent': ('2026-09-01', '2026-10-03')}


@dataclass(frozen=True)
class RangeRule:
    family: str
    lookback: int

    @property
    def key(self):
        return f'{self.family}_{self.lookback}'


VARIANTS = tuple(RangeRule(family, n) for family in ('breakout_trailing', 'low_reclaim') for n in (20, 55))
RULES = {'previous_trials': 69, 'timeframe': 'day', 'entry': 'confirmation close -> next weekday open',
         'trend': 'close > EMA100 and EMA100 rising versus five bars ago; warmup 100',
         'breakout': 'close > maximum high of preceding 20/55 bars; no current high in threshold',
         'low_reclaim': 'previous bar low < its preceding 20/55-bar minimum, closes above that minimum; separate next green close > previous high, low not below reversal low',
         'stop': 'breakout initial 3 ATR14; reclaim floor -0.1 ATR, minimum 0.5%; stop gaps filled at open',
         'trailing': 'breakout: max previous stop, previous held closes maximum -3 prior ATR; effective next bar only',
         'exit': 'breakout: close below prior 10/20-day low -> next open; reclaim: close >= preceding 5-bar high -> next open',
         'time_exit_bars': {'breakout_trailing': 60, 'low_reclaim': 10},
         'entry_gap_atr_max': 1, 'anomaly_open_ratio': [.6, 1.4],
         'capital_per_stock': 100000, 'allocation_pct_max': 25, 'risk_pct': .5,
         'fee_bps_each_side': 5, 'slippage_bps_each_side': 5, 'stress_multiplier': 2,
         'max_calendar_gap_days': 4, 'minimum_development_trades': 40,
         'minimum_selection_trades': 12, 'minimum_recent_trades': 12,
         'approval': 'always false; old periods already examined, no independent holdout',
         'limitations': 'current lot size; no dividend/total-return adjustment, taxes, queue, financing, exchange calendar or guaranteed fills'}


def registry():
    return {r.key: asdict(r) for r in VARIANTS}


def features(bars):
    base = feature_rows(bars)
    result = []
    for i, (c, f) in enumerate(zip(bars, base)):
        prior = lambda n: bars[max(0, i-n):i]
        result.append({**f, 'rising': i >= 5 and f['ema'][100] > base[i-5]['ema'][100],
                       'highs': {n: max((b.high for b in prior(n)), default=c.high) for n in (5, 20, 55)},
                       'lows': {n: min((b.low for b in prior(n)), default=c.low) for n in (10, 20, 55)}})
    return result


def signal(bars, rows, i, rule):
    if i < 100:
        return False
    c, f = bars[i], rows[i]
    if c.volume <= 0 or c.close <= f['ema'][100] or not f['rising']:
        return False
    if rule.family == 'breakout_trailing':
        return c.close > f['highs'][rule.lookback]
    before, p = bars[i-1], rows[i-1]
    return (before.volume > 0 and before.low < p['lows'][rule.lookback]
            and before.close > p['lows'][rule.lookback]
            and c.close > c.open and c.close > before.high and c.low >= before.low
            and (c.time-before.time).days <= 4)


def simulate(bars, rows, rule, first_day, last_day, lot=1):
    validate_bars(bars)
    if len(bars) != len(rows) or rule not in VARIANTS:
        raise ValueError('Unregistered rule or mismatched features')
    indices = [i for i, c in enumerate(bars) if i >= 101
               and first_day <= c.time.astimezone(MSK).date().isoformat() < last_day]
    cash = stress_cash = 100000.
    peak, drawdown, position = cash, 0., None
    trades, curve = [], []
    skipped = {'price_gap': 0, 'calendar_gap': 0, 'no_lot': 0}

    def close(out, c, reason):
        nonlocal cash, stress_cash, position
        p = position
        proceeds = p['shares']*out*.9995*.9995
        cash += proceeds
        stress_cash += p['shares']*out*.999*.999
        pnl = proceeds-p['cost']
        trades.append({'signal_time': p['signal_time'], 'entry_time': p['time'].isoformat(),
                       'exit_bar': c.time.isoformat(), 'entry_quote': p['entry'], 'exit_quote': out,
                       'shares': p['shares'], 'pnl': pnl, 'return_pct': pnl/p['cost']*100,
                       'reason': reason, 'verified_bars': p['verified'], 'stop_at_exit': p['stop']})
        position = None

    for i in indices:
        c, before, f = bars[i], bars[i-1], rows[i-1]
        day = c.time.astimezone(MSK).date()
        calendar_gap = (c.time-before.time).days > 4
        suspicious = not .6 <= c.open/before.close <= 1.4
        exited_at_open = False
        if position is not None:
            if calendar_gap or suspicious or c.volume <= 0:
                position['verified'] = False
            if rule.family == 'breakout_trailing':
                # Previous close was known BEFORE today's open. Today's high is
                # never used to tighten a stop and trigger it on today's low.
                position['stop'] = max(position['stop'], position['max_close']-3*f['atr'])
                should_exit = before.close < f['lows'][10 if rule.lookback == 20 else 20]
            else:
                should_exit = before.close >= f['highs'][5]
            if c.open <= position['stop']:
                close(c.open, c, 'gap_stop')
            elif should_exit:
                close(c.open, c, 'signal_next_open')
            elif calendar_gap:
                close(c.open, c, 'unknown_calendar_gap')
            exited_at_open = position is None
        if position is None and not exited_at_open and day.weekday() < 5 and signal(bars, rows, i-1, rule):
            if calendar_gap:
                skipped['calendar_gap'] += 1
            elif suspicious or abs(c.open-before.close) > f['atr']:
                skipped['price_gap'] += 1
            else:
                stop = c.open-max(3*f['atr'], .005*c.open) if rule.family == 'breakout_trailing' else min(before.low, bars[i-2].low)-.1*f['atr']
                stop = round(min(stop, c.open*.995), 9)
                if stop > 0:
                    size = position_size(cash, .5, 25, round(c.open, 9), stop,
                                         round(c.open+2*(c.open-stop), 9), lot)
                    shares = size['shares']
                    if shares:
                        cost = shares*c.open*1.0005*1.0005
                        cash -= cost
                        stress_cash -= shares*c.open*1.001*1.001
                        position = {'entry': c.open, 'stop': stop, 'index': i, 'time': c.time,
                                    'signal_time': before.time.isoformat(), 'shares': shares, 'cost': cost,
                                    'max_close': c.open, 'verified': c.volume > 0}
                    else:
                        skipped['no_lot'] += 1
        if position is not None:
            if c.low <= position['stop']:
                close(position['stop'], c, 'stop')
            elif i-position['index']+1 >= RULES['time_exit_bars'][rule.family] or i == indices[-1]:
                close(c.close, c, 'window_end' if i == indices[-1] else 'time')
            else:
                position['max_close'] = max(position['max_close'], c.close)
        marked = cash if position is None else cash+position['shares']*c.close*.9995*.9995
        peak = max(peak, marked)
        drawdown = max(drawdown, (peak-marked)/peak*100)
        curve.append({'day': day.isoformat(), 'equity': marked/100000})
    return {'status': 'ok' if indices else 'insufficient', 'trades': len(trades),
            'return_pct': (cash/100000-1)*100, 'stress_return_pct': (stress_cash/100000-1)*100,
            'max_drawdown_pct': drawdown, 'unverified_trades': sum(not t['verified_bars'] for t in trades),
            'mean_trade_pct': mean(t['return_pct'] for t in trades) if trades else None,
            'trade_log': trades, 'daily_curve': curve, 'skipped_entries': skipped}


def passes(periods, qualities):
    return bool(set(qualities) == set(TICKERS) and set(periods) == set(PERIODS)
        and all(v is True for v in qualities.values()) and all(
        p['accounts'] == len(TICKERS) and p['return_pct'] > 0 and p['stress_return_pct'] > 0
        and p['trades'] >= RULES[f'minimum_{name}_trades']
        and p['positive_accounts'] >= 4 and p['unverified_trades'] == 0 and p['max_drawdown_pct'] < 15
        for name, p in periods.items()))
