"""Frozen pattern-flow v2, immutable exchange inputs; no order calls or live edits."""
import hashlib
import inspect
import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from importlib import metadata
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from dashboard.method_study import validate_candles
from dashboard.pattern_flow_study import (PROTOCOL, START, END, NOTE, VARIANTS, PERIODS,
                                          TICKERS, RESERVES, compare_flow, flow_coverage,
                                          passes_flow_screen, protocol_spec, source_hashes)
from dashboard.signals import Candle
from dashboard.strategy_search import aggregate


MAX_INPUT_BYTES = 100_000_000


def write_immutable(path, saved):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(saved, stream, ensure_ascii=False, sort_keys=True, allow_nan=False)


def freeze_protocol(directory):
    directory.mkdir(parents=True, exist_ok=True)
    spec = json.loads(json.dumps(protocol_spec(), ensure_ascii=False, sort_keys=True))
    signature = {"spec": spec, "source_sha256": source_hashes(),
                 "protocol_spec_sha256": hashlib.sha256(json.dumps(spec, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()}
    path = directory/"pattern-flow-protocol-v2.json"
    if path.exists():
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
            matching = all(old.get(k) == v for k, v in signature.items())
        except (OSError, ValueError, TypeError, AttributeError):
            old, matching = {"error": "Unreadable protocol, original retained."}, False
        if not matching:
            at = datetime.now(timezone.utc)
            audit = directory/f"pattern-flow-protocol-mismatch-{at.strftime('%Y%m%d-%H%M%S-%f')}.json"
            write_immutable(audit, {"at": at.isoformat(), "frozen": old, "attempted": signature})
            raise CommandError(f"Frozen v2 source/spec changed; original retained, audit {audit.name}. Register a new version.")
        saved = old
    else:
        saved = {"at": datetime.now(timezone.utc).isoformat(), **signature}
        write_immutable(path, saved)
    return saved, hashlib.sha256(path.read_bytes()).hexdigest()


def load_input(path, ticker):
    """Validate exact immutable request bounds and source; retain all source bars."""
    if not path.is_file() or path.stat().st_size > MAX_INPUT_BYTES:
        raise ValueError("Missing or oversized input")
    raw = path.read_bytes()
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError("Oversized input")
    saved = json.loads(raw.decode("utf-8"))
    request = saved["request"]
    if (not isinstance(saved.get("schema_version"), int) or isinstance(saved.get("schema_version"), bool)
            or saved.get("schema_version") != 1 or saved.get("source") != "T-Bank SDK GetCandles"
            or saved["instrument"]["ticker"] != ticker or request["interval_minutes"] != 15
            or request["maximum_chunk_days"] != 7
            or datetime.fromisoformat(request["from_utc"]) != START
            or datetime.fromisoformat(request["to_utc_exclusive"]) != END):
        raise ValueError("Input source/request does not match the registered experiment")
    lot = saved["instrument"]["lot"]
    if isinstance(lot, bool) or not isinstance(lot, int) or lot < 1:
        raise ValueError("Invalid instrument lot")
    candles, buys, sells = [], [], []
    for item in saved["bars"]:
        if item.get("is_complete") is not True:
            raise ValueError("Incomplete source candle")
        volume = item["volume"]
        if isinstance(volume, bool) or not isinstance(volume, int) or not 0 <= volume < 2**63:
            raise ValueError("Invalid total volume")
        time = datetime.fromisoformat(item["time"])
        if time.tzinfo is None or not START <= time or time+timedelta(minutes=15) > END:
            raise ValueError("Candle outside registered bounds")
        candles.append(Candle(time, *(float(item[k]) for k in ("open", "high", "low", "close")), volume))
        buys.append(item.get("volume_buy"))
        sells.append(item.get("volume_sell"))
    if not candles:
        raise ValueError("No completed candles")
    validate_candles(candles)
    quality = flow_coverage(candles, buys, sells)
    sources = Counter(item.get("candle_source") for item in saved["bars"])
    exchange_requested = request.get("candle_source_type") == "CANDLE_SOURCE_EXCHANGE"
    verified_sources = all(item.get("candle_source") == "CANDLE_SOURCE_EXCHANGE" for item in saved["bars"])
    quality.update(exchange_requested=exchange_requested, exchange_source_verified=verified_sources,
                   candle_sources={key or "UNKNOWN": value for key, value in sources.items()},
                   passed=quality["passed"] and exchange_requested and verified_sources)
    return {"saved": saved, "candles": candles, "buys": buys, "sells": sells, "lot": lot,
            "quality": quality, "source": {"file": path.name, "sha256": hashlib.sha256(raw).hexdigest()}}


def acquire(directory, ticker, fetch=False, progress=None):
    if ticker not in TICKERS:
        raise CommandError("Only the seven registered discovery stocks are allowed; reserve prices stay sealed.")
    path = directory/f"flow-input-{ticker}-20261003.json"
    if path.exists():
        try:
            if not path.resolve().is_relative_to(directory.resolve()):
                raise ValueError("Input escapes data directory")
            return load_input(path, ticker)
        except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
            raise CommandError(f"{ticker}: invalid immutable input {path.name}; original retained, not overwritten.") from None
    if not fetch:
        raise CommandError(f"Missing {path.name}; cached replay makes no broker calls. Use --fetch only when authorized.")
    if datetime.now(timezone.utc) < END:
        raise CommandError("The fixed historical endpoint has not completed yet.")
    from dashboard.market_provider import open_client, resolve, number, error_message
    from t_tech.invest.schemas import CandleInterval, CandleSource
    collected, chunks = {}, []
    try:
        with open_client() as client:
            instrument = resolve(client, ticker)
            method = client.market_data.get_candles
            exchange = ("candle_source_type" in inspect.signature(method).parameters
                        and hasattr(CandleSource, "CANDLE_SOURCE_EXCHANGE"))
            extra = {"candle_source_type": CandleSource.CANDLE_SOURCE_EXCHANGE} if exchange else {}
            cursor = START
            while cursor < END:
                until = min(cursor+timedelta(days=7), END)
                response = method(instrument_id=instrument["uid"], from_=cursor, to=until,
                                  interval=CandleInterval.CANDLE_INTERVAL_15_MIN, **extra)
                kept = 0
                for candle in response.candles:
                    if (not candle.is_complete or not cursor <= candle.time < until
                            or candle.time+timedelta(minutes=15) > END):
                        continue
                    source = getattr(candle, "candle_source", getattr(candle, "candle_source_type", None))
                    row = {"time": candle.time.isoformat(), "open": number(candle.open),
                           "high": number(candle.high), "low": number(candle.low), "close": number(candle.close),
                           "volume": candle.volume, "volume_buy": getattr(candle, "volume_buy", None),
                           "volume_sell": getattr(candle, "volume_sell", None), "is_complete": True,
                           "candle_source": getattr(source, "name", None)}
                    if candle.time in collected and collected[candle.time] != row:
                        raise ValueError("Broker returned inconsistent duplicate candles")
                    collected[candle.time] = row
                    kept += 1
                chunks.append({"from_utc": cursor.isoformat(), "to_utc_exclusive": until.isoformat(),
                               "completed_candles": kept})
                if progress:
                    progress(f"{ticker}: chunk {len(chunks)} through {until.date().isoformat()}, {len(collected)} completed candles")
                cursor = until
    except Exception as exc:
        raise CommandError(f"{ticker}: read-only acquisition unavailable. {error_message(exc)}") from None
    try:
        sdk_version = metadata.version("t-tech-investments")
    except metadata.PackageNotFoundError:
        sdk_version = "unavailable"
    saved = {"schema_version": 1, "source": "T-Bank SDK GetCandles",
             "fetched_at": datetime.now(timezone.utc).isoformat(), "sdk_version": sdk_version,
             "instrument": instrument,
             "request": {"from_utc": START.isoformat(), "to_utc_exclusive": END.isoformat(),
                         "interval_minutes": 15, "maximum_chunk_days": 7,
                         "candle_source_type": "CANDLE_SOURCE_EXCHANGE" if exchange else "unsupported_by_sdk",
                         "chunks": chunks}, "bars": [collected[time] for time in sorted(collected)]}
    # Exclusive creation prevents silently replacing an earlier dataset.
    write_immutable(path, saved)
    try:
        return load_input(path, ticker)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
        raise CommandError(f"{ticker}: acquired input failed validation; immutable raw input retained for audit.") from None


def _aggregate(models):
    result = aggregate(models)
    if not result["accounts"]:
        result.update(return_pct=None, stress_return_pct=None, max_drawdown_pct=None)
    return result


def _empty_model():
    return {"status": "insufficient", "trades": 0, "return_pct": None,
            "stress_return_pct": None, "max_drawdown_pct": None, "unverified_trades": 0,
            "trade_log": [], "daily_curve": [], "win_rate_pct": None}


class Command(BaseCommand):
    help = "Test three registered candle-flow patterns v2; same-source baselines, no order API calls."

    def add_arguments(self, parser):
        parser.add_argument("--fetch", action="store_true", help="Read only missing immutable historical inputs from T-Bank.")

    def handle(self, *args, **options):
        directory = (Path(settings.BASE_DIR)/"data").resolve()
        protocol, protocol_hash = freeze_protocol(directory)  # Must precede any price/flow read.
        datasets, errors = {}, {}
        for ticker in TICKERS:
            try:
                datasets[ticker] = acquire(directory, ticker, options["fetch"], self.stdout.write)
                self.stdout.write(f"INPUT {ticker}: {len(datasets[ticker]['candles'])} completed candles; flow quality={datasets[ticker]['quality']['passed']}")
            except CommandError as exc:
                errors[ticker] = str(exc)
                self.stderr.write(str(exc))
        if not datasets:
            raise CommandError("No valid flow inputs. Protocol is frozen; no results or profits have been fabricated.")
        # All seven inputs are collected before scanning or inspecting P&L.
        at = datetime.now(timezone.utc)
        results, qualities, inputs = [], {}, {}
        for ticker in TICKERS:
            dataset = datasets.get(ticker)
            qualities[ticker] = dataset["quality"] if dataset else {"passed": False, "status": "missing"}
            inputs[ticker] = dataset["source"] if dataset else {"file": None, "sha256": None}
            patterns = compare_flow(dataset["candles"], dataset["buys"], dataset["sells"], dataset["lot"]) if dataset else {
                key: {period: {"baseline": _empty_model(), "flow": _empty_model(), "difference_pp": None}
                      for period in PERIODS} for key in VARIANTS}
            results.append({"ticker": ticker, "status": "ok" if dataset else "insufficient",
                            "error": errors.get(ticker), "source": inputs[ticker],
                            "quality": qualities[ticker], "patterns": patterns})
        summaries = []
        for key, variant in VARIANTS.items():
            flow = {period: _aggregate([r["patterns"][key][period]["flow"] for r in results]) for period in PERIODS}
            baseline = {period: _aggregate([r["patterns"][key][period]["baseline"] for r in results]) for period in PERIODS}
            difference = {p: flow[p]["return_pct"]-baseline[p]["return_pct"]
                          if flow[p]["return_pct"] is not None and baseline[p]["return_pct"] is not None else None for p in PERIODS}
            allowed = passes_flow_screen(flow, qualities)
            summaries.append({"key": key, "name": variant["name"], "flow": flow, "baseline": baseline,
                              "difference_pp": difference, "eligible": allowed})
            self.stdout.write(f"{key}: " + "; ".join(
                f"{period} {s['return_pct']:+.3f}% / {s['stress_return_pct']:+.3f}% stress, {s['trades']} trades"
                if s["return_pct"] is not None else f"{period} insufficient" for period, s in flow.items()) + f"; eligible={allowed}")
        passing = sorted((s for s in summaries if s["eligible"]), key=lambda s: (-s["flow"]["selection"]["stress_return_pct"], s["key"]))
        candidate, choice_path = None, directory/"pattern-flow-choice-v2.json"
        if choice_path.exists() and not passing:
            raise CommandError("A v2 candidate is already frozen but no longer passes this replay; original retained, do not hide or replace it.")
        if passing:
            winner = passing[0]
            candidate = json.loads(json.dumps({"key": winner["key"], "variant": VARIANTS[winner["key"]],
                "protocol_sha256": protocol_hash, "inputs": inputs, "reserve_tickers": RESERVES,
                "selection_stress_return_pct": winner["flow"]["selection"]["stress_return_pct"],
                "status": "frozen_exploratory_not_confirmed"}))
            if choice_path.exists():
                old = json.loads(choice_path.read_text(encoding="utf-8"))
                if any(old.get(k) != v for k, v in candidate.items()):
                    raise CommandError("A different v2 candidate is already frozen; original retained.")
                candidate = old
            else:
                candidate = {"at": at.isoformat(), **candidate}
                write_immutable(choice_path, candidate)
        meta = {"schema_version": 2, "protocol": PROTOCOL, "protocol_sha256": protocol_hash,
                "source_sha256": protocol["source_sha256"], "at": at.isoformat(), "inputs": inputs,
                "periods": PERIODS, "candidate": candidate, "note": NOTE}
        stamp = at.strftime("%Y%m%d-%H%M%S-%f")
        full_path = directory/f"pattern-flow-study-{stamp}.json"
        write_immutable(full_path, {**meta, "results": results, "summaries": summaries})
        def compact(model):
            return {k: v for k, v in model.items() if k not in ("trade_log", "daily_curve")}
        rows = [{**r, "patterns": {key: {period: {"baseline": compact(values["baseline"]), "flow": compact(values["flow"]),
                                                    "difference_pp": values["difference_pp"]}
                                          for period, values in periods.items()} for key, periods in r["patterns"].items()}}
                for r in results]
        short = [{**s, "flow": {p: compact(v) for p, v in s["flow"].items()},
                  "baseline": {p: compact(v) for p, v in s["baseline"].items()}} for s in summaries]
        summary_path = directory/f"pattern-flow-summary-{stamp}.json"
        write_immutable(summary_path, {**meta, "results": rows, "summaries": short})
        self.stdout.write(f"Full report: {full_path}\nCompact report: {summary_path}\nReserve prices were not read. {NOTE}")
