"""Read only compact v2 results; never fetch prices or activate trading rules."""
import hashlib
import json
import math
import re
from datetime import datetime
from pathlib import Path

from django.conf import settings


MAX_BYTES = 2_000_000
HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
TICKER_RE = re.compile(r"[A-Z0-9][A-Z0-9.\-]{0,14}\Z")


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _model_valid(model):
    if not isinstance(model, dict) or model.get("status") not in ("ok", "insufficient"):
        return False
    trades = model.get("trades")
    if isinstance(trades, bool) or not isinstance(trades, int) or not 0 <= trades <= 1_000_000:
        return False
    unverified = model.get("unverified_trades")
    if type(unverified) is not int or not 0 <= unverified <= trades:
        return False
    if model["status"] == "insufficient" and trades:
        return False
    for key in ("return_pct", "stress_return_pct", "win_rate_pct", "max_drawdown_pct"):
        value = model.get(key)
        if value is not None and not _finite(value):
            return False
    if model["status"] == "ok":
        if any(model.get(key) is None for key in ("return_pct", "stress_return_pct", "max_drawdown_pct")):
            return False
        if not 0 <= model["max_drawdown_pct"] <= 100:
            return False
        if trades and (model.get("win_rate_pct") is None or not 0 <= model["win_rate_pct"] <= 100):
            return False
    return True


def _quality_valid(quality):
    if not isinstance(quality, dict) or quality.get("passed") is not True:
        return False
    if quality.get("exchange_requested") is not True or quality.get("exchange_source_verified") is not True:
        return False
    periods = quality.get("periods")
    if not isinstance(periods, dict) or set(periods) != {"development", "selection", "recent"}:
        return False
    checks = [quality, *periods.values()]
    for check in checks:
        if (not isinstance(check, dict) or check.get("passed") is not True
                or type(check.get("malformed_records")) is not int or check["malformed_records"] != 0):
            return False
        if any(not _finite(check.get(key)) or not 99 <= check[key] <= 100
               for key in ("overall_coverage_pct", "core_coverage_pct")):
            return False
    return True


def load_pattern_flow_research(ticker):
    empty = {"status": "insufficient", "patterns": [], "at": None, "entry_allowed": False,
             "message": "Нет проверяемой дополнительной проверки объёма покупок по API.",
             "note": "Эти результаты не разрешают покупку и не восстанавливают исторический стакан."}
    if not isinstance(ticker, str) or not TICKER_RE.fullmatch(ticker.upper()):
        return empty
    ticker = ticker.upper()
    directory = (Path(settings.BASE_DIR)/"data").resolve()
    try:
        paths = sorted(directory.glob("pattern-flow-summary-*.json"), reverse=True)
        if not paths:
            return empty
        from .pattern_flow_study import PROTOCOL, VARIANTS, TICKERS, NOTE, source_hashes
        hashes = source_hashes()
        protocol_path = directory/"pattern-flow-protocol-v2.json"
        if (not protocol_path.resolve().is_relative_to(directory) or not protocol_path.is_file()
                or protocol_path.stat().st_size > 50_000):
            return empty
        protocol_raw = protocol_path.read_bytes()
        if len(protocol_raw) > 50_000:
            return empty
        protocol = json.loads(protocol_raw.decode("utf-8"))
        if protocol.get("source_sha256") != hashes or protocol.get("spec", {}).get("version") != PROTOCOL:
            return empty
        protocol_hash = hashlib.sha256(protocol_raw).hexdigest()
        for path in paths[:20]:
            try:
                if not path.resolve().is_relative_to(directory) or path.stat().st_size > MAX_BYTES:
                    continue
                raw = path.read_bytes()
                if len(raw) > MAX_BYTES:
                    continue
                saved = json.loads(raw.decode("utf-8"))
                if (not isinstance(saved, dict) or type(saved.get("schema_version")) is not int
                        or saved["schema_version"] != 2 or saved.get("protocol") != PROTOCOL
                        or saved.get("source_sha256") != hashes or saved.get("protocol_sha256") != protocol_hash):
                    continue
                at = datetime.fromisoformat(saved["at"])
                if at.tzinfo is None or at.utcoffset() is None:
                    continue
                results = saved["results"]
                if (not isinstance(results, list) or len(results) != len(TICKERS)
                        or {r["ticker"] for r in results} != set(TICKERS)):
                    continue
                row = next((r for r in results if r["ticker"] == ticker), None)
                if row is None:
                    return {**empty, "at": saved["at"], "note": NOTE,
                            "message": "Акция отсутствует в дополнительном исследовании; доходность не считается нулевой."}
                if row.get("status") != "ok" or not _quality_valid(row.get("quality")):
                    return {**empty, "at": saved["at"], "note": NOTE,
                            "message": "Недостаточно классифицированных биржевых объёмов по этой акции. Отсутствие данных не считается продажами."}
                source = row["source"]
                if (not isinstance(source.get("file"), str) or Path(source["file"]).name != source["file"]
                        or not isinstance(source.get("sha256"), str) or not HASH_RE.fullmatch(source["sha256"])):
                    continue
                patterns = row["patterns"]
                if set(patterns) != set(VARIANTS):
                    continue
                converted = []
                for key, variant in VARIANTS.items():
                    values = patterns[key]["recent"]
                    flow, baseline = values["flow"], values["baseline"]
                    if not _model_valid(flow) or not _model_valid(baseline):
                        raise ValueError("Invalid model metrics")
                    unverified = flow.get("unverified_trades")
                    if type(unverified) is not int or unverified < 0:
                        raise ValueError("Invalid quality count")
                    enough = flow["status"] == "ok" and flow["trades"] > 0
                    good = enough and not unverified and flow["return_pct"] > 0 and flow["stress_return_pct"] > 0
                    converted.append({"key": key, "name": variant["name"], "trades": flow["trades"],
                                      **{k: flow.get(k) if enough else None for k in ("return_pct", "stress_return_pct", "win_rate_pct", "max_drawdown_pct")},
                                      "baseline_return_pct": baseline.get("return_pct") if baseline["trades"] else None,
                                      "difference_pp": flow["return_pct"]-baseline["return_pct"]
                                      if enough and baseline["trades"] and baseline["status"] == "ok" else None,
                                      "verdict": "insufficient" if not enough else "exploratory" if good else "failed"})
                enough = any(p["trades"] for p in converted)
                return {**empty, "status": "ok" if enough else "insufficient", "at": saved["at"],
                        "message": "1 сентября — 2 октября 2026: покупки API ≥60% на подтверждающей свече; сравнение с базой на том же источнике. Это не независимое подтверждение заработка." if enough else
                                   "По этой акции в последнем участке нет сделок с новым фильтром. Это не прибыль 0%.",
                        "note": NOTE, "patterns": converted,
                        "coverage_pct": row["quality"]["core_coverage_pct"]}
            except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
                continue
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
        pass
    return empty
