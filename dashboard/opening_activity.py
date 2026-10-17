"""Causal, LONG-only adaptations of opening activity and breakout/retest ideas."""
from collections import deque
from datetime import timedelta
from statistics import mean

from .method_study import validate_candles
from .risk import position_size
from .signals import MSK

VERSION = 'opening_activity_v1'
TICKERS = ('SBER', 'GAZP', 'LKOH', 'NVTK', 'GMKN', 'ROSN', 'YDEX')
VARIANTS = {
    'plain': 'Пробой диапазона 10:00–10:15',
    'active': 'Пробой двух самых активных акций',
    'retest': 'Активная акция: пробой → возврат → подтверждение',
}
PERIODS = {'development': ('2026-03-30', '2026-08-01'),
           'selection': ('2026-08-01', '2026-09-01'),
           'recent': ('2026-09-01', '2026-10-03'),
           'additional': ('2026-10-03', '2026-10-13')}
SCENARIOS = {'base': (1, 0), 'double_cost': (2, 0), 'delayed': (1, 1)}
RULES = {
    'previous_trials': 95, 'new_trials': 3, 'total_trials': 98,
    'opening': 'completed positive-volume green 15-minute EXCHANGE bar starting exactly 10:00 Moscow, weekdays',
    'activity': 'opening volume / mean of previous 14 observed weekday 10:00 bars; reference excludes current day',
    'selection': 'active/retest: RVOL>=1.5; top two among fixed seven positive-opening names; descending RVOL then ticker; selected at 10:15, before later candles',
    'warmup': 'all versions require 14 prior valid weekday opening bars; missing openings never fabricated',
    'breakout': 'first green completed close > opening high, previous close <= high, contiguous from opening; confirmation closes by 12:00',
    'retest': 'after breakout, separate bar touches high from above and closes above it, does not break opening low; next contiguous green close > retest high; at most 4 bars to retest',
    'entry': 'next contiguous bar open, one entry per stock/day; >=opening high and <=confirmation close+0.5*opening range width',
    'stop': 'opening low, minimum initial distance 0.3% of entry; gap stop at actual next open',
    'exit': 'stop or close of 17:45 bar at 18:00 Moscow; no profit target; no overnight position',
    'costs': 'fee 5bps and slippage 5bps each side; double both; delayed scenario enters one extra contiguous 15-minute bar later with original signal',
    'sizing': '100000 rubles per separate stock account, risk 0.5%, allocation max25%, whole lots; independently recalculated for each cost scenario',
    'quality': 'held discontinuity, zero-volume bar, suspicious opening ratio outside [0.6,1.4] or missing 18:00 exit marks outcome unknown; retain provisional ledger',
    'screen': 'all seven inputs; zero unknown trades; at least40/20/20 trades development/selection/recent; positive returns in all three cost scenarios and periods; >=4 positive stock accounts development/selection; drawdown<15%',
    'additional': 'October3–12 new to this archive, historical when rules registered; too short for independent profitability confirmation',
    'limitations': '15-minute adaptation, seven current liquid stocks, LONG only; no author-performance replication, news timestamps, historical order book, taxes, dividends or actual fill evidence',
    'entry_allowed': False,
}
SOURCES = {
    'paper': 'https://concretumgroup.com/wp-content/uploads/2026/02/A-Profitable-Day-Trading-Strategy-For-The-U.S.-Equity-Market.pdf',
    'video_transcript': 'https://www.smbtraining.com/blog/prop-trader-details-a-stock-trade-you-can-make-right-on-the-open',
    'book_author': 'https://www.adamhgrimes.com/the-art-and-science-a-deeper-look-12/',
    'pullback': 'https://www.adamhgrimes.com/pullback-trade-works/',
}


def scan(candles):
    validate_candles(candles)
    events = {key: [None]*len(candles) for key in VARIANTS}
    history = deque(maxlen=14)
    day, opening, breakout, retest, continuous = None, None, None, None, False
    activity = None
    diagnostics = {'opening_bars': 0, 'ready_openings': 0, 'missing_opening_days': 0}
    seen_days = set()

    def event(i):
        c = candles[i]
        return {'signal_at': (c.time+timedelta(minutes=15)).isoformat(), 'bar_at': c.time.isoformat(),
                'day': c.time.astimezone(MSK).date().isoformat(), 'level': opening.high, 'floor': opening.low,
                'width': opening.high-opening.low, 'price': c.close, 'rvol': activity}

    for i, c in enumerate(candles):
        local = c.time.astimezone(MSK)
        current_day, minute = local.date(), local.hour*60+local.minute
        if local.weekday() >= 5:
            continue
        if current_day != day:
            day, opening, breakout, retest, activity, continuous = current_day, None, None, None, None, False
        if minute == 600:
            diagnostics['opening_bars'] += 1
            reference = mean(history) if len(history) == 14 else None
            activity = c.volume/reference if reference and c.volume > 0 else None
            if c.volume > 0:
                history.append(c.volume)
            if activity is not None:
                diagnostics['ready_openings'] += 1
            if c.close > c.open and c.volume > 0 and c.high > c.low and activity is not None:
                opening, continuous = c, True
            continue
        if minute < 615 or minute > 705:
            continue
        if day not in seen_days:
            seen_days.add(day)
            if not any(b.time.astimezone(MSK).date() == day and b.time.astimezone(MSK).hour == 10 and b.time.astimezone(MSK).minute == 0 for b in candles[max(0, i-16):i]):
                diagnostics['missing_opening_days'] += 1
        if not opening or not continuous:
            continue
        if not i or c.time-candles[i-1].time != timedelta(minutes=15) or c.volume <= 0:
            continuous = False
            continue
        if breakout is None:
            if c.close > opening.high and candles[i-1].close <= opening.high and c.close > c.open:
                breakout = i
                events['plain'][i] = event(i)
                if activity >= 1.5:
                    events['active'][i] = event(i)
        elif i > breakout:
            if c.low < opening.low:
                retest = None
                continuous = False
            elif retest is None and i-breakout <= 4 and c.low <= opening.high and c.close >= opening.high:
                retest = i
            elif retest is not None and i == retest+1:
                if c.close > c.open and c.close > candles[retest].high and activity >= 1.5:
                    events['retest'][i] = event(i)
                retest = None
                continuous = False  # exactly one confirmation opportunity
    return {'events': events, 'diagnostics': diagnostics}


def select_active(datasets, scans):
    """Cross-stock ranks use only the first completed bar, never later profit."""
    choices = {}
    for ticker, data in datasets.items():
        history = deque(maxlen=14)
        for c in data['candles']:
            local = c.time.astimezone(MSK)
            if local.weekday() >= 5 or (local.hour, local.minute) != (10, 0):
                continue
            reference = mean(history) if len(history) == 14 else None
            rvol = c.volume/reference if reference and c.volume > 0 else None
            if rvol is not None and rvol >= 1.5 and c.close > c.open:
                choices.setdefault(local.date().isoformat(), []).append((rvol, ticker))
            if c.volume > 0:
                history.append(c.volume)
    chosen = {day: [ticker for _, ticker in sorted(ranks, key=lambda x: (-x[0], x[1]))[:2]]
              for day, ranks in choices.items()}
    for ticker, scanned in scans.items():
        for key in ('active', 'retest'):
            scanned['events'][key] = [e if e and ticker in chosen.get(e['day'], []) else None for e in scanned['events'][key]]
    return chosen


def simulate(candles, events, first, last, lot, multiplier=1, delay=0):
    validate_candles(candles)
    if len(events) != len(candles) or multiplier not in (1, 2) or delay not in (0, 1):
        raise ValueError('Invalid opening-activity simulation')
    position_size(100000, .5, 25, 100, 99, 102, lot)
    indices = [i for i, c in enumerate(candles) if first <= c.time.astimezone(MSK).date().isoformat() < last]
    cash, peak, drawdown, position, entered_day = 100000., 100000., 0., None, None
    fee = slip = .0005*multiplier
    trades, curve, skipped = [], {}, 0
    for i in indices:
        c = candles[i]
        local = c.time.astimezone(MSK)
        minute = local.hour*60+local.minute
        before = candles[i-1] if i else None
        adjacent = before is not None and c.time-before.time == timedelta(minutes=15)
        suspicious = before is not None and not .6 <= c.open/before.close <= 1.4
        signal_i = i-1-delay
        signal = events[signal_i] if signal_i >= 0 else None
        valid_path = signal is not None and all(candles[j].time-candles[j-1].time == timedelta(minutes=15) for j in range(signal_i+1, i+1))
        if (position is None and signal and local.date().isoformat() != entered_day and local.weekday() < 5
                and 630 <= minute <= 735 and signal['day'] == local.date().isoformat()):
            if not valid_path or suspicious or not signal['level'] <= c.open <= signal['price']+.5*signal['width']:
                skipped += 1
            else:
                distance = max(c.open-signal['floor'], c.open*.003)
                stop = round(c.open-distance, 9)
                if stop > 0:
                    size = position_size(cash, .5, 25, round(c.open, 9), stop, round(c.open+2*distance, 9), lot,
                                         fee_bps=5*multiplier, slippage_bps=5*multiplier)
                    shares = size['shares']
                    if shares:
                        cost = shares*c.open*(1+fee)*(1+slip)
                        cash -= cost
                        position = {'entry_time': c.time.isoformat(), 'signal_at': signal['signal_at'], 'entry_quote': c.open,
                                    'stop': stop, 'shares': shares, 'cost': cost, 'verified_bars': True,
                                    'opening_rvol': signal['rvol'], 'planned_loss': size['planned_loss']}
                        entered_day = local.date().isoformat()
        if position is not None:
            p = position
            if c.volume <= 0 or suspicious or not adjacent:
                p['verified_bars'] = False
            quote, reason = None, None
            if c.open <= p['stop']:
                quote, reason = c.open, 'gap_stop'
            elif not adjacent:
                quote, reason = c.open, 'missing_data'
            elif c.low <= p['stop']:
                quote, reason = p['stop'], 'stop'
            elif minute >= 1065 or i == indices[-1]:
                quote, reason = c.close, 'end_of_day' if minute == 1065 else 'missing_session_close'
                if minute != 1065:
                    p['verified_bars'] = False
            if quote is not None:
                proceeds = p['shares']*quote*(1-fee)*(1-slip)
                cash += proceeds
                trades.append({**p, 'exit_bar': c.time.isoformat(), 'exit_quote': quote,
                               'pnl': proceeds-p['cost'], 'return_pct': (proceeds/p['cost']-1)*100, 'reason': reason})
                position = None
        marked = cash if position is None else cash+position['shares']*c.close*(1-fee)*(1-slip)
        peak = max(peak, marked)
        drawdown = max(drawdown, (peak-marked)/peak)
        curve[local.date().isoformat()] = marked/100000
    gains, losses = sum(max(0, t['pnl']) for t in trades), -sum(min(0, t['pnl']) for t in trades)
    return {'status': 'ok' if indices else 'insufficient', 'return_pct': (cash/100000-1)*100 if indices else None,
            'trades': len(trades), 'unverified_trades': sum(not t['verified_bars'] for t in trades),
            'max_drawdown_pct': drawdown*100, 'win_rate_pct': mean(t['pnl'] > 0 for t in trades)*100 if trades else None,
            'mean_trade_pct': mean(t['return_pct'] for t in trades) if trades else None,
            'profit_factor': gains/losses if losses else None, 'trade_log': trades,
            'daily_curve': [{'day': day, 'equity': equity} for day, equity in curve.items()],
            'skipped_entries': skipped, 'entry_allowed': False}


def aggregate(models):
    valid = [m for m in models if m['status'] == 'ok']
    return {'accounts': len(valid), 'trades': sum(m['trades'] for m in valid),
            'return_pct': mean(m['return_pct'] for m in valid) if valid else None,
            'unverified_trades': sum(m['unverified_trades'] for m in valid),
            'positive_accounts': sum(m['return_pct'] > 0 for m in valid),
            'max_stock_drawdown_pct': max((m['max_drawdown_pct'] for m in valid), default=None)}


def passes(periods):
    return all((m := periods[p][s])['accounts'] == 7 and m['trades'] >= n and m['unverified_trades'] == 0
               and m['return_pct'] > 0 and m['max_stock_drawdown_pct'] < 15
               and (p == 'recent' or m['positive_accounts'] >= 4)
               for p, n in (('development', 40), ('selection', 20), ('recent', 20)) for s in SCENARIOS)
