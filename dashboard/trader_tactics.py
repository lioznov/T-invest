"""Registered LONG research inspired by Cesar Alvarez, not his performance."""
from dataclasses import asdict, dataclass
from statistics import mean

from .risk import position_size
from .signals import MSK
from .strategy_search import feature_rows, validate_bars

VERSION = 'trader_pullbacks_v1'
SOURCES = ('https://alvarezquanttrading.com/blog/avoiding-gap-trades/',
           'https://alvarezquanttrading.com/blog/the-abcs-of-creating-a-mean-reversion-strategy-part1/')


@dataclass(frozen=True)
class Tactic:
    key: str
    limit_pct: float = 0
    three_down: bool = False


VARIANTS = (Tactic('rsi_open'), Tactic('rsi_limit_4',4), Tactic('rsi_three_down',three_down=True))
NAMES = {'rsi_open':'Покупка сильной акции после падения',
         'rsi_limit_4':'То же, но ждать цену на 4% ниже',
         'rsi_three_down':'То же + три снижения подряд'}
RULES = {'previous_trials':77, 'source':SOURCES,
         'setup':'completed close > SMA200, RSI2 <5; warmup 200; optional 3 consecutive close declines',
         'entry':'next observed bar, weekday only; open or one-day 4% below setup close limit',
         'limit_execution':'strict low below limit/(1+0.001) or open <= that barrier; quote min(open,barrier); model caps slipped price at limit even under stress; touch alone is not fill',
         'stop':'fixed prior 3 ATR14 or minimum .5% of planned entry; gap through stop exits at opening quote, also on weekends',
         'exit':'RSI2>70 or 10 held closed daily bars -> next observed open; window end liquidated at close',
         'risk_pct':.5,'allocation_pct':25,'capital_per_stock':100000,'fee_bps_each_side':5,'slip_bps_each_side':5,'stress_multiplier':2,
         'quality':'calendar gap >4 days, price anomaly .6..1.4 or held zero-volume bar -> unverified; limit fills not guaranteed',
         'minimum_trades':{'development':40,'selection':12,'recent':12},
         'adaptation':'Russian stocks, protective stop, sizing and weekday entries differ from author; no original portfolio volatility ranking',
         'approval':'always false; previously studied periods, no independent holdout, no dividends/taxes/order queue/exchange calendar'}


def registry():
    return {r.key:asdict(r) for r in VARIANTS}


def features(bars):
    rows = feature_rows(bars)
    for i,f in enumerate(rows):
        f['sma200'] = mean(c.close for c in bars[i-199:i+1]) if i >= 199 else None
        f['three_down'] = i >= 3 and all(bars[j].close < bars[j-1].close for j in range(i-2,i+1))
    return rows


def setup(c,f,rule):
    return (f['sma200'] is not None and c.volume > 0 and c.close > f['sma200']
            and f['rsi2'] < 5 and (not rule.three_down or f['three_down']))


def simulate(bars,rows,rule,first,last,lot=1):
    validate_bars(bars)
    if len(rows) != len(bars) or rule not in VARIANTS:
        raise ValueError('Unregistered tactic or mismatched features')
    indices = [i for i,c in enumerate(bars) if i >= 200 and first <= c.time.astimezone(MSK).date().isoformat() < last]
    cash = stress_cash = 100000.
    peak, dd, p = cash, 0., None
    trades, curve = [], []
    setups = missed_limits = ambiguous = 0

    def close(quote,c,reason):
        nonlocal cash,stress_cash,p
        proceeds = p['shares']*quote*.9995*.9995
        cash += proceeds
        stress_cash += p['shares']*quote*.999*.999
        pnl = proceeds-p['cost']
        trades.append({'signal_time':p['signal_time'],'entry_time':p['time'].isoformat(),'exit_bar':c.time.isoformat(),
                       'entry_quote':p['entry'],'planned_limit':p['limit'],'entry_cost_per_share':p['cost']/p['shares'],
                       'stress_entry_cost_per_share':p['stress_cost']/p['shares'],'exit_quote':quote,'shares':p['shares'],
                       'pnl':pnl,'return_pct':pnl/p['cost']*100,'verified_bars':p['verified'],
                       'fill_assumption':p['fill'],'reason':reason})
        p = None

    for i in indices:
        c,before,f = bars[i],bars[i-1],rows[i-1]
        gap = (c.time-before.time).days > 4
        suspicious = not .6 <= c.open/before.close <= 1.4
        exited = False
        if p is not None:
            p['verified'] &= not gap and not suspicious and c.volume > 0
            if c.open <= p['stop']:
                close(c.open,c,'gap_stop')
            elif f['rsi2'] > 70 or i-p['index'] >= 10:
                close(c.open,c,'signal_next_open' if f['rsi2'] > 70 else 'time_next_open')
            exited = p is None
        if p is None and not exited and c.time.astimezone(MSK).weekday() < 5 and setup(before,f,rule):
            setups += 1
            planned = before.close*(1-rule.limit_pct/100) if rule.limit_pct else c.open
            quote, fill = c.open, 'next_open_assumption'
            if rule.limit_pct:
                barrier = planned/1.001
                if c.open <= barrier:
                    quote,fill = c.open,'limit_open_assumption'
                elif c.low < barrier:
                    quote,fill = barrier,'strict_limit_cross_assumption'
                else:
                    quote = None
                    missed_limits += 1
                    ambiguous += c.low <= planned
            stop = round(planned-max(3*f['atr'],planned*.005),9)
            # Limit order sizing uses its pre-session limit, never the later low.
            if quote is not None and stop > 0:
                size = position_size(cash,.5,25,round(planned,9),stop,round(planned+2*(planned-stop),9),lot)
                shares = size['shares']
                if shares:
                    cost,stress_cost = shares*quote*1.0005*1.0005,shares*quote*1.001*1.001
                    cash -= cost
                    stress_cash -= stress_cost
                    p = {'entry':quote,'limit':planned if rule.limit_pct else None,'index':i,'time':c.time,
                         'signal_time':before.time.isoformat(),'stop':stop,'shares':shares,'cost':cost,'stress_cost':stress_cost,
                         'verified':not gap and not suspicious and c.volume > 0,'fill':fill}
                    if quote <= stop:
                        close(quote,c,'entry_gap_through_stop')
        if p is not None:
            if c.low <= p['stop']:
                close(p['stop'],c,'stop')
            elif i == indices[-1]:
                close(c.close,c,'window_end')
        marked = cash if p is None else cash+p['shares']*c.close*.9995*.9995
        peak = max(peak,marked)
        dd = max(dd,(peak-marked)/peak*100)
        curve.append({'day':c.time.astimezone(MSK).date().isoformat(),'equity':marked/100000})
    return {'status':'ok' if indices else 'insufficient','trades':len(trades),'return_pct':(cash/100000-1)*100,
            'stress_return_pct':(stress_cash/100000-1)*100,'max_drawdown_pct':dd,
            'unverified_trades':sum(not t['verified_bars'] for t in trades),'trade_log':trades,'daily_curve':curve,
            'setups':setups,'missed_limits':missed_limits,'ambiguous_touches':ambiguous}


def passes(periods):
    return set(periods) == set(RULES['minimum_trades']) and all(m['accounts']==7 and m['return_pct']>0
        and m['stress_return_pct']>0 and m['trades'] >= RULES['minimum_trades'][name]
        and m['positive_accounts'] >=4 and m['unverified_trades']==0 and m['max_drawdown_pct']<15
        for name,m in periods.items())
