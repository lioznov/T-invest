"""Read-only paper trades from successive archived quotes, never broker orders.

This deliberately leaves gaps unresolved: a quote after a data outage cannot
tell us whether the stop or target was touched while the collector was offline.
"""
import json
import math
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from django.conf import settings


STRATEGY_VERSION = "six_rules_v1"
MAX_GAP = timedelta(seconds=90)
MAX_HOLD = timedelta(minutes=180)

REASONS = {
    "awaiting_quote": "Ждём следующий свежий стакан для учебного входа.",
    "holding": "Учебная позиция открыта; ждём условия выхода.",
    "entry_timeout": "После сигнала нет нового подходящего стакана в течение 90 секунд.",
    "entry_outside_plan": "К следующему стакану цена входа оказалась за пределами стопа и цели.",
    "data_gap": "В наблюдениях перерыв больше 90 секунд; исход сделки неизвестен.",
    "version_changed": "Во время наблюдения изменилась версия правил; исход не засчитан.",
    "stop": "Наблюдаемая цена продажи достигла стопа.",
    "target": "Наблюдаемая цена продажи достигла цели.",
    "exit": "Алгоритм дал сигнал выхода.",
    "time": "Прошло 180 минут с учебного входа.",
}


def _timestamp(value):
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(result, datetime) or result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("Timezone-aware timestamp required")
    return result.astimezone(timezone.utc)


def _positive(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value > 0)


def _quote(payload, at):
    try:
        book = payload["book"]
        book_at = _timestamp(book["time"])
        bid, bid_size = book["bids"][0]
        ask, ask_size = book["asks"][0]
        if (not all(_positive(x) for x in (bid, ask, bid_size, ask_size))
                or ask < bid or not timedelta(0) <= at - book_at <= MAX_GAP):
            return None
        return book_at, float(bid), float(ask)
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def _plan(payload):
    plan = payload.get("plan")
    if not isinstance(plan, dict):
        return None
    numbers = [plan.get(key) for key in ("entry", "stop", "target", "risk_per_share")]
    if not all(_positive(value) for value in numbers):
        return None
    entry, stop, target, risk = numbers
    if not stop < entry < target:
        return None
    return {"entry": float(entry), "stop": float(stop), "target": float(target),
            "risk_per_share": float(risk)}


def _cost(value):
    if (not isinstance(value, (int, float)) or isinstance(value, bool)
            or not math.isfinite(value) or not 0 <= value < 10000):
        raise ValueError("Costs must be finite basis points from 0 to below 10000")
    return value / 10000


def review_paper_trades(rows, fee_bps=5, slippage_bps=5, now=None):
    """Replay one ticker in order; an observed LONG starts a pending paper entry.

    ``now`` bounds the replay, including tests/historical inspections. A newer
    quote is required both to enter and to exit. Continuous LONG observations
    are one event, even after an unresolved gap, until WAIT/EXIT rearms entry.
    """
    fee, slip = _cost(fee_bps), _cost(slippage_bps)
    current = _timestamp(now) if now is not None else datetime.now(timezone.utc)
    observations = {}
    for row in rows:
        try:
            at, raw = row
            at = _timestamp(at)
            payload = json.loads(raw) if isinstance(raw, str) else raw
            if at <= current and isinstance(payload, dict):
                # The archive has a unique ticker/time key. Repeated input rows
                # must not create an extra fill at the same observation time.
                observations.setdefault(at, payload)
        except (ValueError, TypeError):
            continue

    ordered = sorted(observations.items())
    trades, active = [], None
    previous_decision = None
    latest_quote_at = None
    last_covered_at = None
    eligible_times = []

    def mark(reason, status="unverified"):
        active.update(status=status, reason=reason, reason_text=REASONS[reason])

    for at, payload in ordered:
        version_ok = payload.get("strategy_version") == STRATEGY_VERSION
        quote = _quote(payload, at) if payload.get("review_ready") is True else None

        if active is not None:
            reference = last_covered_at if active["entry_at"] else _timestamp(active["signal_at"])
            if at - reference > MAX_GAP:
                mark("data_gap" if active["entry_at"] else "entry_timeout")
                active = None
            elif not version_ok:
                mark("version_changed")
                active = None

        if not version_ok or quote is None:
            continue
        eligible_times.append(at)
        book_at, bid, ask = quote
        decision = payload.get("decision")
        # Reusing an unchanged quote is not new evidence and cannot extend
        # coverage, fill an order, or trigger an exit with a hindsight price.
        new_quote = latest_quote_at is None or book_at > latest_quote_at
        if active is not None and new_quote:
            if active["entry_at"] is None:
                signal_at = _timestamp(active["signal_at"])
                # The quote must be produced after the signal was observed,
                # not simply fetched later from a cached pre-signal book.
                if at > signal_at and book_at > signal_at:
                    entry = ask * (1 + slip)
                    if not active["stop"] < entry < active["target"]:
                        mark("entry_outside_plan")
                        active = None
                    else:
                        active.update(entry_at=at.isoformat(), entry=entry,
                                      entry_quote_at=book_at.isoformat())
                        mark("holding", "open")
                        last_covered_at = at
            else:
                if latest_quote_at is not None and book_at - latest_quote_at > MAX_GAP:
                    mark("data_gap")
                    active = None
                else:
                    last_covered_at = at
                    exit_price, reason = None, None
                    if bid <= active["stop"]:
                        exit_price, reason = bid, "stop"
                    elif bid >= active["target"]:
                        exit_price, reason = active["target"], "target"
                    elif decision == "EXIT":
                        exit_price, reason = bid, "exit"
                    elif at - _timestamp(active["entry_at"]) >= MAX_HOLD:
                        exit_price, reason = bid, "time"
                    if reason:
                        exit_price *= 1 - slip
                        net = exit_price * (1 - fee) / (active["entry"] * (1 + fee)) - 1
                        active.update(exit_at=at.isoformat(), exit=exit_price,
                                      exit_quote_at=book_at.isoformat(), net_pct=net * 100)
                        mark(reason, "closed")
                        active = None

        if active is None and decision == "LONG" and previous_decision != "LONG" and new_quote:
            plan = _plan(payload)
            if plan is not None:
                instrument = payload.get("instrument") or {}
                active = {"signal_at": at.isoformat(), "entry_at": None, "exit_at": None,
                          "entry": None, "exit": None, "planned_entry": plan["entry"],
                          "stop": plan["stop"], "target": plan["target"],
                          "risk_per_share": plan["risk_per_share"], "net_pct": None,
                          "strategy_version": STRATEGY_VERSION,
                          "ticker": instrument.get("ticker") if isinstance(instrument, dict) else None,
                          "lot": instrument.get("lot") if isinstance(instrument, dict) else None}
                mark("awaiting_quote", "pending")
                trades.append(active)
                last_covered_at = at

        if decision in ("LONG", "WAIT", "EXIT"):
            previous_decision = decision
        if new_quote:
            latest_quote_at = book_at

    if active is not None:
        reference = last_covered_at if active["entry_at"] else _timestamp(active["signal_at"])
        if current - reference > MAX_GAP:
            mark("data_gap" if active["entry_at"] else "entry_timeout")

    closed = [trade for trade in trades if trade["status"] == "closed"]
    returns = [trade["net_pct"] for trade in closed]
    equity, peak, drawdown = 1.0, 1.0, 0.0
    for value in returns:
        equity *= 1 + value / 100
        peak = max(peak, equity)
        drawdown = max(drawdown, (1 - equity / peak) * 100)
    gains = sum(max(value, 0) for value in returns)
    losses = -sum(min(value, 0) for value in returns)
    summary = {
        "total": len(trades), "closed": len(closed),
        "open": sum(trade["status"] == "open" for trade in trades),
        "pending": sum(trade["status"] == "pending" for trade in trades),
        "unverified": sum(trade["status"] == "unverified" for trade in trades),
        "net_return_pct": (equity - 1) * 100 if closed else None,
        "win_rate": sum(value > 0 for value in returns) / len(returns) * 100 if returns else None,
        "profit_factor": gains / losses if losses else None,
        "max_drawdown_pct": drawdown if closed else None,
    }
    return {
        "status": "ok", "version": STRATEGY_VERSION, "summary": summary,
        "trades": list(reversed(trades[-50:])), "fee_bps": fee_bps, "slippage_bps": slippage_bps,
        "coverage": {"first_at": eligible_times[0].isoformat() if eligible_times else None,
                     "last_at": eligible_times[-1].isoformat() if eligible_times else None,
                     "observations": len(eligible_times),
                     "recorded_observations": len(ordered),
                     "latest_recorded_at": ordered[-1][0].isoformat() if ordered else None},
        "method": ("Учебный вход по следующему свежему стакану после LONG, не позднее 90 секунд. "
                   "План фиксируется при сигнале. Покупка по цене продавца, выход по цене покупателя; "
                   "выход по цели ограничен самой целью. Комиссия и проскальзывание учтены с обеих сторон. "
                   "Не более одной позиции; выход по стопу, цели, EXIT или через 180 минут."),
        "limitation": ("Это расчёт по отдельным снимкам стакана, реальных сделок нет. Между снимками "
                       "могли происходить движения цены. Перерывы более 90 секунд оставляют исход неизвестным. "
                       "Сводка учитывает только завершённые сделки: доходность с условным реинвестированием, "
                       "просадка по закрытиям. Пропущенные исходы могут искажать результат. "
                       "Количество снимков не доказывает надёжность стратегии."),
    }


def load_paper_journal(ticker, fee_bps=5, slippage_bps=5):
    """Read only an existing archive. Old observations cannot create new plans."""
    path = Path(settings.BASE_DIR) / "data" / "market.sqlite3"
    if not path.is_file():
        return review_paper_trades([], fee_bps, slippage_bps)
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
            tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('paper_observations', 'observations')"
            )}
            if not tables:
                return review_paper_trades([], fee_bps, slippage_bps)
            table = "paper_observations" if "paper_observations" in tables else "observations"
            rows = db.execute(f"SELECT at, payload FROM {table} WHERE ticker=? ORDER BY at",
                              (ticker,)).fetchall()
    except sqlite3.Error:
        return {"status": "error", "message": "Не удалось прочитать журнал учебных сделок."}
    return review_paper_trades(rows, fee_bps, slippage_bps)
