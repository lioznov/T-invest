"""Isolated v2: broker-classified candle flow, not inferred trader identities.

The three fixed hypotheses add one known-at-confirmation filter to the frozen
volume-only pattern engine. Neither that engine nor live trading rules change.
"""
import hashlib
import json
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from .management.commands.study_patterns import PERIODS, RESERVES, TICKERS, eligible
from .pattern_study import VARIANTS as BASE_VARIANTS, simulate_pattern
from .price_patterns import NAMES, scan_patterns
from .signals import MSK


PROTOCOL = "pattern_flow_study_v2"
START = datetime(2026, 3, 30, tzinfo=MSK).astimezone(timezone.utc)
END = datetime(2026, 10, 3, tzinfo=MSK).astimezone(timezone.utc)
MIN_BUY_FRACTION = .60
MIN_COVERAGE_PCT = 99.
VARIANTS = {f"{family}_buy60": {"family": family, "volume": True,
                              "buy_fraction_min": MIN_BUY_FRACTION,
                              "name": f"{name} · объём ≥ 1,3 + покупки API ≥ 60%"}
            for family, name in NAMES.items()}
NOTE = ("Три новые исследовательские гипотезы после 56 прежних вариантов. "
        "Все три периода уже просматривались: это не независимый holdout. "
        "Направление объёма берётся из API, метод классификации агрессора не подтверждён. "
        "Источник свечей обновлён: сравнение допустимо с baseline этого же источника, а не напрямую с цифрами v1. "
        "Исторический стакан и реальные исполнения не моделируются; это не команда покупать.")


def classify_flow(volume, buy, sell):
    """UNKNOWN is never interpreted as zero buying or as a sell signal."""
    def integer(value):
        return isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 2**63

    if not integer(volume):
        return "malformed", None
    if buy is None or sell is None:
        return "unknown_missing", None
    if not integer(buy) or not integer(sell):
        return "malformed", None
    if volume == 0:
        return ("unknown_zero_volume", None) if buy == sell == 0 else ("malformed", None)
    if buy == sell == 0:
        # Proto3 int fields can default to zero when data is not supplied.
        return "unknown_unpopulated", None
    if buy+sell != volume:
        return "malformed", None
    return "classified", buy/volume


def _coverage(candles, states):
    nonzero = [i for i, c in enumerate(candles) if c.volume > 0]
    core = [i for i in nonzero if c_weekday(candles[i]) and 10 <= candles[i].time.astimezone(MSK).hour < 18]
    classified = sum(states[i] == "classified" for i in nonzero)
    core_classified = sum(states[i] == "classified" for i in core)
    overall = classified/len(nonzero)*100 if nonzero else None
    core_pct = core_classified/len(core)*100 if core else None
    malformed = sum(s == "malformed" for s in states)
    return {"bars": len(candles), "nonzero_bars": len(nonzero), "classified_nonzero_bars": classified,
            "core_nonzero_bars": len(core), "core_classified_nonzero_bars": core_classified,
            "overall_coverage_pct": overall, "core_coverage_pct": core_pct,
            "malformed_records": malformed, "states": dict(Counter(states)),
            "passed": bool(overall is not None and core_pct is not None and
                           overall >= MIN_COVERAGE_PCT and core_pct >= MIN_COVERAGE_PCT and malformed == 0)}


def c_weekday(candle):
    return candle.time.astimezone(MSK).weekday() < 5


def flow_coverage(candles, buys, sells):
    if len(candles) != len(buys) or len(candles) != len(sells):
        raise ValueError("Flow arrays must correspond to every source candle.")
    states = [classify_flow(c.volume, b, s)[0] for c, b, s in zip(candles, buys, sells)]
    overall = _coverage(candles, states)
    periods = {}
    for name, (first, last) in PERIODS.items():
        indices = [i for i, c in enumerate(candles) if first <= c.time.astimezone(MSK).date().isoformat() < last]
        periods[name] = _coverage([candles[i] for i in indices], [states[i] for i in indices])
    return {**overall, "periods": periods,
            "passed": overall["passed"] and all(p["passed"] for p in periods.values())}


def filter_flow_events(events, candles, buys, sells):
    """Independent copies; confirmation volume is known before the next-open fill."""
    if (len(candles) != len(buys) or len(candles) != len(sells)
            or set(events) != set(NAMES) or any(len(v) != len(candles) for v in events.values())):
        raise ValueError("Mismatched event/flow arrays")
    fractions = [classify_flow(c.volume, b, s)[1] for c, b, s in zip(candles, buys, sells)]
    return {family: [deepcopy(event) if event is not None and fractions[i] is not None
                    and fractions[i] >= MIN_BUY_FRACTION else None
                    for i, event in enumerate(values)] for family, values in events.items()}


def compare_flow(candles, buys, sells, lot):
    events = scan_patterns(candles)
    filtered = filter_flow_events(events, candles, buys, sells)
    result = {}
    for key, variant in VARIANTS.items():
        baseline = next(v for v in BASE_VARIANTS.values() if v["family"] == variant["family"] and v["volume"])
        periods = {}
        for name, (first, last) in PERIODS.items():
            plain = simulate_pattern(candles, events, baseline, first, last, lot)
            flow = simulate_pattern(candles, filtered, variant, first, last, lot)
            periods[name] = {"baseline": plain, "flow": flow,
                             "difference_pp": flow["return_pct"]-plain["return_pct"]
                             if flow["status"] == plain["status"] == "ok" else None}
        result[key] = periods
    return result


def protocol_spec():
    if len(VARIANTS) != 3 or {v["family"] for v in VARIANTS.values()} != set(NAMES):
        raise ValueError("Exactly three registered pattern-flow hypotheses required")
    return {"version": PROTOCOL, "registry": VARIANTS, "previous_trials": 56,
            "tickers": TICKERS, "reserved_tickers": RESERVES, "periods_halfopen": PERIODS,
            "input_from_utc": START.isoformat(), "input_to_utc_exclusive": END.isoformat(),
            "input": {"source": "T-Bank SDK GetCandles", "interval_minutes": 15,
                      "chunk_max_days": 7, "candle_source_type": "EXCHANGE when supported",
                      "weekends": "Retain original completed source candles; existing scanner controls entry sessions.",
                      "buy_sell_fields": "Preserve raw API integers/absence; never infer from OHLCV.",
                      "coverage_min_pct": MIN_COVERAGE_PCT,
                      "coverage_scope": "Each stock overall and weekday 10:00–18:00, also within each of the three periods.",
                      "missing_or_unpopulated": "UNKNOWN, not zero buy/sell; veto the corresponding flow event.",
                      "source_quality": "EXCHANGE must be requested and every retained candle explicitly exchange-sourced; dealer/unknown veto eligibility.",
                      "malformed": "Any noninteger/negative flow or inconsistent nonzero sum invalidates evidence."},
            "execution": {"direction": "long_only", "real_orders": False,
                          "engine": "Unchanged pattern_study.simulate_pattern",
                          "capital_per_stock": 100000, "risk_pct": .5, "allocation_pct": 25,
                          "fee_bps_per_side": 5, "slippage_bps_per_side": 5,
                          "stress_cost_multiplier": 2,
                          "filter": "Completed confirmation bar rvol >=1.3 and buy/(buy+sell) >=0.60; enter next open.",
                          "baseline": "Same-source volume-only family; descriptive comparison, not an additional trial.",
                          "aggregation": "Equal-capital independent stock accounts, not an executable shared portfolio."},
            "eligibility": {"normal_and_stress_positive": "All three periods", "unverified_trades": 0,
                            "minimum_accounts_each_period": 7,
                            "minimum_trades": {"development": 40, "selection": 20, "recent": 20},
                            "minimum_positive_stocks": {"development": 4, "selection": 4},
                            "max_drawdown_pct_exclusive": 15,
                            "data_quality": "All seven stock coverage checks must pass.",
                            "one_choice": "Maximum selection stress return; ascending key tie-break. Reserve not read."},
            "note": NOTE}


def source_hashes():
    directory = Path(__file__).resolve().parent
    names = ("price_patterns.py", "pattern_study.py", "risk.py", "signals.py", "strategy_search.py",
             "method_study.py", "market_provider.py", "pattern_flow_study.py",
             "management/commands/study_patterns.py", "management/commands/study_pattern_flow.py")
    return {name: hashlib.sha256((directory/name).read_bytes()).hexdigest() for name in names}


def passes_flow_screen(summaries, qualities):
    return bool(set(qualities) == set(TICKERS) and all(q.get("passed") is True for q in qualities.values())
                and eligible(summaries))
