"""Single shared, unleveraged research account; decisions never see outcomes.

Candidates already contain causal model scores. Outcomes only schedule fills and
settlements. Drawdown is realised equity at cost, not intrabar mark-to-market.
"""
from collections import Counter
from datetime import datetime
from itertools import groupby
from math import floor
from .signals import MSK
from .risk import position_size

RULES = {'capital':100000., 'risk_pct':.5, 'allocation_pct':25., 'max_open_risk_pct':1.5,
         'daily_realised_loss_pct':1.5, 'max_positions':4, 'fee_bps':5, 'slippage_bps':5,
         'same_time_priority':'expected net descending, then ticker; no outcome ranking',
         'unknown':'reserve capital/risk for full hour, report unknown, withhold profit/readiness',
         'drawdown':'realised equity plus cost of remaining positions; intrabar risk not measured'}


def simulate(rows, multiplier=1):
    cash = peak = RULES['capital']
    positions, busy, trades = [], {}, []
    daily, day_start, skipped = Counter(), {}, Counter()
    unknown = 0
    worst, max_used, max_open = 0., 0., 0
    fee = slip = .0005*multiplier

    def equity():
        return cash+sum(p['cost'] for p in positions)

    def settle(at):
        nonlocal cash, peak, worst
        # Equal-time exits are aggregated before any new entry. No arbitrary
        # within-candle win/loss ordering is interpreted as an equity path.
        due = sorted((p for p in positions if p['exit_at']<=at),key=lambda p:p['exit_at'])
        for exit_at, group in groupby(due,key=lambda p:p['exit_at']):
            for p in group:
                cash += p['cost']+p['pnl']
                positions.remove(p)
                if p['unknown']:
                    continue
                local_day = datetime.fromisoformat(exit_at).astimezone(MSK).date().isoformat()
                daily[local_day] += p['pnl']
                trades.append({k:p[k] for k in ('ticker','at','exit_at','pnl','shares','cost','planned_loss')})
            value = equity()
            peak = max(peak,value)
            worst = max(worst,100*(peak-value)/peak)

    ordered = sorted(rows,key=lambda r:(r['at'],-r.get('expected_net_pct',r.get('probability',0)),r['ticker']))
    for at, group in groupby(ordered,key=lambda r:r['at']):
        settle(at)
        day = datetime.fromisoformat(at).astimezone(MSK).date().isoformat()
        day_start.setdefault(day,equity()-daily[day])
        for row in group:
            if not row.get('selected'):
                continue
            if busy.get(row['ticker'],'') > at:
                skipped['overlap'] += 1
                continue
            if daily[day] <= -day_start[day]*RULES['daily_realised_loss_pct']/100:
                skipped['daily_loss'] += 1
                continue
            if len(positions) >= RULES['max_positions']:
                skipped['positions'] += 1
                continue
            o = row['outcome']
            entry, stop = row['entry'], row['stop']
            if entry is None or stop is None:
                unknown += 1
                skipped['missing_entry'] += 1
                busy[row['ticker']] = row['label_end']
                continue
            size = position_size(equity(),RULES['risk_pct'],RULES['allocation_pct'],round(entry,9),round(stop,9),
                                 round(entry+2*(entry-stop),9),row['lot'],fee_bps=5*multiplier,slippage_bps=5*multiplier)
            per_share = entry*(1+fee)*(1+slip)
            risk_per_share = per_share-stop*(1-fee)*(1-slip)
            available_risk = max(0,equity()*RULES['max_open_risk_pct']/100-sum(p['planned_loss'] for p in positions))
            lots = min(size['lots'],floor((cash+1e-9)/(per_share*row['lot'])),floor((available_risk+1e-9)/(risk_per_share*row['lot'])))
            shares = max(0,lots)*row['lot']
            if not shares:
                skipped['capital_or_risk'] += 1
                continue
            cost = per_share*shares
            cash -= cost
            if o is None:
                unknown += 1
            pnl = shares*(o['exit']*(1-fee)*(1-slip)-per_share) if o else 0.
            positions.append({'ticker':row['ticker'],'at':at,'exit_at':o['exit_at'] if o else row['label_end'],
                              'pnl':pnl,'shares':shares,'cost':cost,'planned_loss':risk_per_share*shares,'unknown':o is None})
            busy[row['ticker']] = row['label_end']
            max_used = max(max_used,sum(p['cost'] for p in positions))
            max_open = max(max_open,len(positions))
    settle('9999')
    result = (cash/RULES['capital']-1)*100
    return {'trades':len(trades),'unknown_trades':unknown,'return_pct':result if not unknown else None,
            'known_pnl_rub':sum(t['pnl'] for t in trades),'final_capital':cash if not unknown else None,
            'win_rate_pct':100*sum(t['pnl']>0 for t in trades)/len(trades) if trades else None,
            'max_realised_drawdown_pct':worst,'max_committed_cash':max_used,'max_positions':max_open,
            'skipped':dict(skipped),'daily_pnl':dict(daily),'trade_log':trades}
