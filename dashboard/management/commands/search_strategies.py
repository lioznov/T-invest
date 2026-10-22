"""Search a fixed registry, then inspect one selected strategy on sealed stocks."""
import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from dashboard.signals import Candle, MSK
from dashboard.strategy_search import (VERSION, STRATEGIES, REGISTRY, DEVELOPMENT,
                                       VALIDATION, DISCOVERY_TICKERS, RESERVE_TICKERS,
                                       feature_rows, validate_bars, simulate, aggregate,
                                       passes_screen, registry_json)


def read_input(path):
    saved = json.loads(path.read_text(encoding="utf-8"))
    bars = [Candle(datetime.fromisoformat(c["time"]),
                   *(float(c[k]) for k in ("open", "high", "low", "close")), int(c["volume"]))
            for c in saved["bars"]]
    validate_bars(bars)
    if saved.get("source") != "T-Bank SDK GetCandles" or saved["instrument"]["lot"] < 1:
        raise ValueError("No broker provenance or lot size")
    bars = [c for c in bars if c.time.astimezone(MSK).weekday() < 5
            and (saved["timeframe"] == "day" or 10 <= c.time.astimezone(MSK).hour < 18)]
    return saved, bars


def acquire(directory, ticker, timeframe, fetch, until):
    path = directory/f"search-input-{ticker}-{timeframe}-20261002.json"
    if path.exists():
        saved, bars = read_input(path)
        return saved, bars
    if not fetch:
        raise CommandError(f"Missing {path.name}; use --fetch to read historical broker data.")
    from dashboard.market_provider import open_client, resolve, number, error_message
    from t_tech.invest.schemas import CandleInterval
    start = datetime(2023 if timeframe == "day" else 2025, 10, 2, tzinfo=timezone.utc)
    step = timedelta(days=1) if timeframe == "day" else timedelta(hours=1)
    window = timedelta(days=365) if timeframe == "day" else timedelta(days=30)
    interval = CandleInterval.CANDLE_INTERVAL_DAY if timeframe == "day" else CandleInterval.CANDLE_INTERVAL_HOUR
    collected = {}
    try:
        with open_client() as client:
            instrument = resolve(client, ticker)
            cursor = start
            while cursor < until:
                end = min(cursor+window, until)
                response = client.market_data.get_candles(instrument_id=instrument["uid"],
                                                         from_=cursor, to=end, interval=interval)
                for raw in response.candles:
                    if raw.is_complete and raw.time+step <= until:
                        c = Candle(raw.time, number(raw.open), number(raw.high), number(raw.low),
                                   number(raw.close), raw.volume)
                        # Only registered weekday core hours; no dealer/weekend
                        # fills sneak into an exchange-session swing experiment.
                        local = c.time.astimezone(MSK)
                        if local.weekday() < 5 and (timeframe == "day" or 10 <= local.hour < 18):
                            collected[c.time] = c
                cursor = end
    except Exception as exc:
        raise CommandError(f"{ticker}/{timeframe}: {error_message(exc)}") from None
    bars = [collected[t] for t in sorted(collected)]
    validate_bars(bars)
    if len(bars) < 120:
        raise CommandError(f"{ticker}/{timeframe}: insufficient broker history ({len(bars)} bars).")
    saved = {"source": "T-Bank SDK GetCandles", "fetched_at": datetime.now(timezone.utc).isoformat(),
             "requested_start": start.isoformat(), "requested_end": until.isoformat(),
             "timeframe": timeframe, "instrument": instrument, "bars": [asdict(c) for c in bars]}
    path.write_text(json.dumps(saved, ensure_ascii=False, default=lambda x: x.isoformat()), encoding="utf-8")
    return saved, bars


class Command(BaseCommand):
    help = "Search 24 registered long-only strategies; keep one held-stock evaluation separate."

    def add_arguments(self, parser):
        parser.add_argument("--fetch", action="store_true", help="Read missing daily/hourly historical inputs from T-Bank")
        parser.add_argument("--confirm", action="store_true", help="Evaluate exactly one frozen candidate on three reserved stocks")

    def handle(self, *args, **options):
        directory = Path(settings.BASE_DIR)/"data"
        directory.mkdir(exist_ok=True)
        at = datetime.now(timezone.utc)
        suffix = VERSION.rsplit("_", 1)[-1]
        protocol_path = directory/f"strategy-search-protocol-{suffix}.json"
        protocol_source = Path(__file__).resolve().parents[2]/"strategy_search.py"
        code_hash = hashlib.sha256(protocol_source.read_bytes()).hexdigest()
        protocol = {"version": VERSION, "registry": registry_json(), "development": DEVELOPMENT,
                    "validation": VALIDATION, "discovery_tickers": DISCOVERY_TICKERS,
                    "reserved_tickers": RESERVE_TICKERS, "registered_at": at.isoformat(),
                    "code_sha256": code_hash, "fee_bps": 5, "slip_bps": 5,
                    "capital_per_account": 100000, "risk_pct": .5, "allocation_pct": 25,
                    "selection": "screen, then maximum validation double-cost return; key breaks ties",
                    "confirmation": "one selected strategy only, held-stock history 2023-10-02 through 2026-10-03; not future data"}
        if protocol_path.exists():
            old = json.loads(protocol_path.read_text(encoding="utf-8"))
            if old["registry"] != protocol["registry"] or old["code_sha256"] != code_hash:
                raise CommandError("Registered code changed. Preserve the previous experiment; register a new protocol version.")
            protocol = old
        else:
            protocol_path.write_text(json.dumps(protocol, ensure_ascii=False), encoding="utf-8")
        # Freeze first, before either selection inputs or reserved inputs are read.
        end = min(at, datetime(2026, 10, 3, tzinfo=timezone.utc))
        if options["confirm"]:
            choice_path = directory/f"strategy-search-choice-{suffix}.json"
            if not choice_path.exists():
                raise CommandError("Run discovery first; no frozen candidate to confirm.")
            choice = json.loads(choice_path.read_text(encoding="utf-8"))
            if choice["protocol_code_sha256"] != code_hash:
                raise CommandError("Candidate does not match the registered code.")
            strategy = REGISTRY[choice["key"]]
            results = []
            for ticker in RESERVE_TICKERS:
                saved, bars = acquire(directory, ticker, strategy.timeframe, options["fetch"], end)
                model = simulate(bars, feature_rows(bars), strategy, DEVELOPMENT[0], "2026-10-03", saved["instrument"]["lot"])
                results.append({"ticker": ticker, "result": model})
                self.stdout.write(f"CONFIRM {ticker}: {model['return_pct']:+.3f}%; stress {model['stress_return_pct']:+.3f}%; {model['trades']} trades")
            report = {"at": at.isoformat(), "protocol": protocol, "candidate": choice,
                      "stage": "held_stocks", "results": results,
                      "aggregate": aggregate([r["result"] for r in results]),
                      "limitation": "Unseen stock prices, but shared historical market regimes; not prospective validation."}
        else:
            prepared = {}
            for tf in ("day", "hour"):
                for ticker in DISCOVERY_TICKERS:
                    saved, bars = acquire(directory, ticker, tf, options["fetch"], end)
                    prepared[tf, ticker] = (saved, bars, feature_rows(bars))
                    self.stdout.write(f"INPUT {ticker}/{tf}: {len(bars)} closed bars")
            results = []
            for strategy in STRATEGIES:
                accounts = []
                for ticker in DISCOVERY_TICKERS:
                    saved, bars, rows = prepared[strategy.timeframe, ticker]
                    development = simulate(bars, rows, strategy, *DEVELOPMENT, saved["instrument"]["lot"])
                    validation = simulate(bars, rows, strategy, *VALIDATION, saved["instrument"]["lot"])
                    accounts.append({"ticker": ticker, "development": development, "validation": validation})
                development = aggregate([r["development"] for r in accounts])
                validation = aggregate([r["validation"] for r in accounts])
                passing = passes_screen(development, validation)
                results.append({"strategy": asdict(strategy), "development": development,
                                "validation": validation, "passes_screen": passing, "accounts": accounts})
                self.stdout.write(f"{strategy.key}: development {development['return_pct']:+.3f}% ({development['trades']} trades); validation {validation['return_pct']:+.3f}% ({validation['trades']} trades); passes={passing}")
            candidates = [r for r in results if r["passes_screen"]]
            choice = None
            if candidates:
                winner = sorted(candidates, key=lambda r: (-r["validation"]["stress_return_pct"], r["strategy"]["key"]))[0]
                choice = {"key": winner["strategy"]["key"], "selected_at": datetime.now(timezone.utc).isoformat(),
                          "protocol_code_sha256": code_hash, "trials": len(STRATEGIES),
                          "selection_validation_return_pct": winner["validation"]["return_pct"],
                          "selection_validation_stress_return_pct": winner["validation"]["stress_return_pct"]}
                choice_path = directory/f"strategy-search-choice-{suffix}.json"
                if choice_path.exists():
                    existing = json.loads(choice_path.read_text(encoding="utf-8"))
                    if existing["key"] != choice["key"]:
                        raise CommandError("A different candidate is already frozen; do not reuse its held-out test.")
                    choice = existing
                else:
                    choice_path.write_text(json.dumps(choice), encoding="utf-8")
                self.stdout.write(f"CANDIDATE FROZEN: {choice['key']}; confirmation not yet inspected.")
            report = {"at": at.isoformat(), "protocol": protocol, "stage": "discovery",
                      "results": results, "candidate": choice,
                      "limitation": "Validation is used for selecting among 24 trials; it is not an independent final test. Some discovery history was inspected earlier."}
        path = directory/f"strategy-search-{report['stage']}-{at.strftime('%Y%m%d-%H%M%S-%f')}.json"
        path.write_text(json.dumps(report, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        self.stdout.write(f"Report: {path}")
