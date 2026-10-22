"""Registered monthly, long-only portfolio experiment; never sends orders."""
from bisect import bisect_left
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date
from math import floor, isfinite
from random import Random
from statistics import mean

from .signals import MSK
from .strategy_search import validate_bars


VERSION = 'monthly_momentum_v1'
TICKERS = ('SBER', 'GAZP', 'LKOH', 'NVTK', 'GMKN', 'ROSN', 'YDEX')
PERIODS = {
    'full': ('2024-01-01', '2026-10-03'),
    'development': ('2024-01-01', '2026-01-01'),
    'selection': ('2026-01-01', '2026-07-01'),
    'recent': ('2026-07-01', '2026-10-03'),
}
FOLDS = (('2025-01-01', '2025-07-01'), ('2025-07-01', '2026-01-01'),
         ('2026-01-01', '2026-07-01'), ('2026-07-01', '2026-10-03'))
SCENARIOS = {'gross': (0, 0), 'base': (1, 0), 'double_cost': (2, 0), 'delayed': (1, 1)}
RULES = {
    'previous_trials': 83, 'new_trials': 12, 'capital': 100000,
    'ranking': 'positive 3/6/12 calendar-month price return ending before the previous calendar month; skip last month',
    'trend': 'last known close > SMA of 200 completed daily bars',
    'breadth': 'optional: >=half of fixed universe have fresh close > SMA200; missing counts as not above',
    'schedule': 'first observed weekday of month; ranks only data before its date; hold retained names without churn',
    'sizing': 'risk <=0.5% equity per stock including costs; entry allocation <=25% per stock; 1 or 3 positions; whole lots; no borrowing',
    'stop': 'initial and trailing 3 ATR14, at least 0.5% at entry; trailing from previous closes only',
    'exit': 'stop including gaps on every observed day, or next monthly rebalance; no take-profit',
    'freshness': 'last bar <=4 calendar days; monthly endpoint <=7 days before cutoff',
    'costs': '5 bps fee +5 bps slippage each side; double both; independent sizing per scenario',
    'quality': 'missing held bar, gap >4 days, zero volume or opening discontinuity outside [0.6,1.4] marks entire result unverified',
    'selection': 'training >=10 trades, zero unknowns, double-cost return>0 and drawdown<=10%; max double-cost return then key',
    'final_screen': '>=30 full trades; positive base/double-cost/delayed in development, selection and recent; no unknowns; max drawdown<=10%',
    'statistics': '2000 shared circular block-bootstrap samples of monthly returns, block=3 months, seed=7102026; current 12-model family only',
    'benchmarks': 'equal-weight buy-and-hold of same seven names at 25%/75% initial total allocation; same costs, no stops, unequal risk; cash earns zero',
    'limitations': 'already studied dates; current survivor universe and lot sizes; no dividends, taxes, splits adjustment, cash interest, queue or actual fills',
    'entry_allowed': False,
}


@dataclass(frozen=True)
class Rule:
    months: int
    count: int
    breadth: bool

    @property
    def key(self):
        return f'm{self.months}_top{self.count}_breadth{int(self.breadth)}'


VARIANTS = tuple(Rule(m, n, b) for m in (3, 6, 12) for n in (1, 3) for b in (False, True))


def shift_month(day, offset):
    value = day.year*12+day.month-1+offset
    return date(value//12, value % 12+1, 1)


def prepare(bars, lot):
    validate_bars(bars)
    if type(lot) is not int or lot < 1:
        raise ValueError('Invalid lot')
    dates = [b.time.astimezone(MSK).date() for b in bars]
    if len(set(dates)) != len(dates):
        raise ValueError('Duplicate daily dates')
    rows, ranges, closes = [], [], []
    for i, bar in enumerate(bars):
        previous = bars[i-1].close if i else bar.open
        ranges.append(max(bar.high-bar.low, abs(bar.high-previous), abs(bar.low-previous)))
        closes.append(bar.close)
        rows.append({'atr': mean(ranges[-14:]), 'sma200': mean(closes[-200:]) if i >= 199 else None})
    return {'bars': bars, 'dates': dates, 'rows': rows, 'by_date': dict(zip(dates, range(len(bars)))), 'lot': lot}


def previous(data, day):
    return bisect_left(data['dates'], day)-1


def rank(prepared, day, rule):
    if rule not in VARIANTS:
        raise ValueError('Unregistered rule')
    ranked, above = [], 0
    end, start = shift_month(day, -1), shift_month(day, -1-rule.months)
    for ticker, data in prepared.items():
        i = previous(data, day)
        if i < 199 or (day-data['dates'][i]).days > 4:
            continue
        bar, row = data['bars'][i], data['rows'][i]
        if bar.volume <= 0 or bar.close <= row['sma200']:
            continue
        above += 1
        a, b = previous(data, start), previous(data, end)
        if a < 0 or b <= a or (start-data['dates'][a]).days > 7 or (end-data['dates'][b]).days > 7:
            continue
        momentum = data['bars'][b].close/data['bars'][a].close-1
        if momentum > 0:
            ranked.append((momentum, ticker))
    if rule.breadth and above*2 < len(prepared):
        return []
    return [ticker for _, ticker in sorted(ranked, key=lambda x: (-x[0], x[1]))[:rule.count]]


def simulate(prepared, rule, first, last, cost_multiplier=1, delay=0):
    if rule not in VARIANTS or cost_multiplier not in (0, 1, 2) or delay not in (0, 1) or first >= last:
        raise ValueError('Unregistered simulation')
    days = sorted({d for p in prepared.values() for d in p['dates'] if first <= d.isoformat() < last})
    cash, peak, drawdown = 100000., 100000., 0.
    fee = slip = .0005*cost_multiplier
    positions, trades, curve, decisions, incidents = {}, [], [], [], []
    bucket, pending = None, None

    def close(ticker, quote, day, reason):
        nonlocal cash
        p = positions.pop(ticker)
        proceeds = p['shares']*quote*(1-slip)*(1-fee)
        cash += proceeds
        trades.append({**p, 'exit_day': day.isoformat(), 'exit_quote': quote, 'reason': reason,
                       'pnl': proceeds-p['cost'], 'return_pct': (proceeds/p['cost']-1)*100})

    def bad(ticker, day, reason):
        incident = {'ticker': ticker, 'day': day.isoformat(), 'reason': reason}
        if incident not in incidents:
            incidents.append(incident)
        if ticker in positions:
            positions[ticker]['verified'] = False

    for day in days:
        active = {ticker: p['by_date'].get(day) for ticker, p in prepared.items()}
        # Inspect held paths on weekends too. Unknown records stay in the ledger.
        for ticker in list(positions):
            data, p, i = prepared[ticker], positions[ticker], active[ticker]
            if i is None:
                bad(ticker, day, 'missing_held_bar')
                continue
            bar = data['bars'][i]
            if i and ((day-data['dates'][i-1]).days > 4 or not .6 <= bar.open/data['bars'][i-1].close <= 1.4):
                bad(ticker, day, 'gap_or_corporate_action')
            if bar.volume <= 0:
                bad(ticker, day, 'zero_volume_held_bar')
            if i:
                p['stop'] = max(p['stop'], data['bars'][i-1].close-3*data['rows'][i-1]['atr'])
            if bar.open <= p['stop']:
                close(ticker, bar.open, day, 'gap_stop')

        current_bucket = (day.year, day.month)
        if day.weekday() < 5 and current_bucket != bucket:
            bucket = current_bucket
            pending = {'signal_day': day.isoformat(), 'wanted': rank(prepared, day, rule), 'remaining': delay}
            decisions.append({'day': day.isoformat(), 'wanted': pending['wanted'][:], 'delay': delay})
        execute = pending is not None and day.weekday() < 5
        if execute and pending['remaining']:
            pending['remaining'] -= 1
            execute = False
        if execute:
            wanted = pending['wanted']
            for ticker in list(positions):
                if ticker not in wanted:
                    i = active[ticker]
                    if i is None:
                        bad(ticker, day, 'missing_rebalance_exit')
                    else:
                        close(ticker, prepared[ticker]['bars'][i].open, day, 'monthly_exit')
            equity = cash+sum(p['shares']*(prepared[t]['bars'][active[t]].open if active[t] is not None else p['last_close']) for t, p in positions.items())
            for ticker in wanted:
                if ticker in positions or len(positions) >= rule.count:
                    continue
                data, i = prepared[ticker], active[ticker]
                if i is None:
                    bad(ticker, day, 'missing_scheduled_entry')
                    continue
                bar = data['bars'][i]
                j = previous(data, day)
                if j < 0:
                    bad(ticker, day, 'missing_entry_context')
                    continue
                distance = max(3*data['rows'][j]['atr'], bar.open*.005)
                stop = bar.open-distance
                if stop <= 0:
                    bad(ticker, day, 'invalid_stop')
                    continue
                unit_cost = bar.open*(1+slip)*(1+fee)
                unit_risk = unit_cost-stop*(1-slip)*(1-fee)
                shares = max(0, floor(min(equity*.005/unit_risk, equity*.25/unit_cost, cash/unit_cost)/data['lot'])*data['lot'])
                if not shares:
                    continue
                positions[ticker] = {'ticker': ticker, 'signal_day': pending['signal_day'], 'entry_day': day.isoformat(),
                                     'entry_quote': bar.open, 'shares': shares, 'cost': shares*unit_cost,
                                     'stop': stop, 'last_close': bar.open, 'verified': True}
                cash -= shares*unit_cost
                # These properties become known during the day, so they flag
                # quality AFTER the open decision, never suppress a losing entry.
                if bar.volume <= 0 or (day-data['dates'][j]).days > 4 or not .6 <= bar.open/data['bars'][j].close <= 1.4:
                    bad(ticker, day, 'unverified_entry_bar')
            pending = None

        for ticker in list(positions):
            i = active[ticker]
            if i is None:
                continue
            bar, p = prepared[ticker]['bars'][i], positions[ticker]
            if bar.low <= p['stop']:
                close(ticker, min(bar.open, p['stop']), day, 'stop')
            else:
                p['last_close'] = bar.close
        # Known reporting boundary: hypothetical close, without looking at next
        # period. Stale end marks explicitly contaminate the result.
        if day == days[-1]:
            for ticker in list(positions):
                if active[ticker] is None:
                    bad(ticker, day, 'missing_final_price')
                close(ticker, positions[ticker]['last_close'], day, 'reporting_boundary')
        equity = cash+sum(p['shares']*p['last_close']*(1-slip)*(1-fee) for p in positions.values())
        peak = max(peak, equity)
        drawdown = max(drawdown, (peak-equity)/peak)
        curve.append({'day': day.isoformat(), 'equity': equity, 'exposure_pct': sum(p['shares']*p['last_close'] for p in positions.values())/equity*100})
        if cash < -1e-6 or not isfinite(equity):
            raise ValueError('Invalid portfolio ledger')
    gains = sum(max(t['pnl'], 0) for t in trades)
    losses = -sum(min(t['pnl'], 0) for t in trades)
    return {'return_pct': (cash/100000-1)*100 if days else None,
            'max_drawdown_pct': drawdown*100 if days else None, 'trades': len(trades),
            'win_rate_pct': mean(t['pnl'] > 0 for t in trades)*100 if trades else None,
            'profit_factor': gains/losses if losses else None, 'unknown_trades': sum(not t['verified'] for t in trades),
            'quality_incidents': incidents, 'verified': bool(days) and not incidents,
            'mean_exposure_pct': mean(r['exposure_pct'] for r in curve) if curve else None,
            'trade_log': trades, 'curve': curve, 'decisions': decisions, 'entry_allowed': False}


def choose(training):
    eligible = [(v['return_pct'], k) for k, v in training.items() if v['verified'] and v['trades'] >= 10
                and v['return_pct'] > 0 and v['max_drawdown_pct'] <= 10]
    return sorted(eligible, key=lambda x: (-x[0], x[1]))[0][1] if eligible else None


def screen(periods):
    if periods['full']['base']['trades'] < 30:
        return False
    return all((r := periods[p][s])['verified'] and r['trades'] > 0 and r['return_pct'] > 0 and r['max_drawdown_pct'] <= 10
               for p in ('development', 'selection', 'recent') for s in ('base', 'double_cost', 'delayed'))


def monthly_returns(curve):
    ends = {}
    for row in curve:
        ends[row['day'][:7]] = row['equity']
    result, previous_equity = {}, 100000.
    for month, value in sorted(ends.items()):
        result[month] = value/previous_equity-1
        previous_equity = value
    return result


def buy_hold(prepared, first, last, allocation, cost_multiplier=1):
    """Price-only comparison at fixed exposure; explicitly not risk-matched."""
    if allocation not in (.25, .75) or cost_multiplier not in (1, 2):
        raise ValueError('Unregistered benchmark')
    days = sorted({d for p in prepared.values() for d in p['dates'] if first <= d.isoformat() < last})
    entry_day = next((d for d in days if d.weekday() < 5), None)
    cash, peak, dd, positions, incidents = 100000., 100000., 0., {}, []
    fee = slip = .0005*cost_multiplier
    for day in days:
        for ticker, data in prepared.items():
            i = data['by_date'].get(day)
            if day == entry_day:
                if i is None:
                    incidents.append({'ticker': ticker, 'day': day.isoformat(), 'reason': 'missing_entry'})
                    continue
                bar = data['bars'][i]
                unit_cost = bar.open*(1+fee)*(1+slip)
                shares = floor(100000*allocation/len(prepared)/unit_cost/data['lot'])*data['lot']
                positions[ticker] = {'shares': shares, 'last': bar.open}
                cash -= shares*unit_cost
            if ticker in positions:
                if i is None:
                    incidents.append({'ticker': ticker, 'day': day.isoformat(), 'reason': 'missing_held_bar'})
                    continue
                bar = data['bars'][i]
                if bar.volume <= 0 or (i and ((day-data['dates'][i-1]).days > 4 or not .6 <= bar.open/data['bars'][i-1].close <= 1.4)):
                    incidents.append({'ticker': ticker, 'day': day.isoformat(), 'reason': 'unverified_bar'})
                positions[ticker]['last'] = bar.close
        equity = cash+sum(p['shares']*p['last']*(1-fee)*(1-slip) for p in positions.values())
        peak = max(peak, equity)
        dd = max(dd, (peak-equity)/peak)
    return {'return_pct': (equity/100000-1)*100 if days else None, 'max_drawdown_pct': dd*100 if days else None,
            'verified': entry_day is not None and not incidents, 'quality_incidents': incidents,
            'entry_allowed': False, 'note': 'Price-only basket, initial allocation fixed; no stops or dividends; not risk-matched.'}


def bootstrap_family(models, draws=2000):
    """Descriptive family-wise max test, NOT adjustment for all past searches."""
    if not models or any(not r['verified'] for r in models.values()):
        return {'status': 'unverified_inputs', 'entry_allowed': False}
    monthly = {k: monthly_returns(v['curve']) for k, v in models.items()}
    months = sorted(set.intersection(*(set(v) for v in monthly.values())))
    if len(months) < 12:
        return {'status': 'insufficient_months', 'entry_allowed': False}
    values = {k: [v[m] for m in months] for k, v in monthly.items()}
    means = {k: mean(v) for k, v in values.items()}
    winner = sorted(means, key=lambda k: (-means[k], k))[0]
    observed = means[winner]
    rng, exceed, samples = Random(7102026), 0, []
    for _ in range(draws):
        indices = []
        while len(indices) < len(months):
            start = rng.randrange(len(months))
            indices.extend((start+j) % len(months) for j in range(3))
        indices = indices[:len(months)]
        null_max = max(mean(v[i]-means[k] for i in indices) for k, v in values.items())
        exceed += null_max >= observed
        samples.append(mean(values[winner][i] for i in indices)*100)
    samples.sort()
    return {'status': 'descriptive_only', 'months': len(months), 'family_size': len(models),
            'best_mean_monthly_key': winner, 'mean_monthly_pct': observed*100,
            'family_adjusted_p': (exceed+1)/(draws+1),
            'mean_monthly_interval_95_pct': [samples[int(draws*.025)], samples[min(draws-1, int(draws*.975))]],
            'draws': draws, 'block_months': 3, 'seed': 7102026, 'entry_allowed': False,
            'note': 'Current fixed family only; excludes correction for earlier 83 attempts. Conditional historical estimate; no guarantee.'}
