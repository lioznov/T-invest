"""Run and remember a read-only opening price estimate on the local site."""
import io
import json
import os
import sqlite3
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock, Thread

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError

from . import morning_model as model
from .market_provider import error_message, open_client, resolve
from .market_stream_provider import exchange_candles
from .morning_service import _forecast, load_artifact
from .management.commands.forecast_morning import schedule_check


class ForecastUnavailable(Exception):
    """A short reason suitable for display in the application."""


STAGES = {
    "loading": "Загружаем биржевые данные…",
    "training": "Готовим модель на сохранённой истории…",
    "analyzing": "Проверяем свечи и рассчитываем показатели…",
    "forecasting": "Рассчитываем и сохраняем прогноз…",
    "checking_actual": "Загружаем фактическую цену открытия…",
}

_jobs = {}
_lock = Lock()


def now_utc():
    return datetime.now(timezone.utc)


def web_cutoff(now):
    local = now.astimezone(model.MSK)
    at = model.clock(local.date(), 17, 45)
    if local.weekday() > 3 or local < at:
        return None
    # The inputs end at 17:45. The simulated purchase at 18:00 belongs to
    # the historical trading test; it is not a web forecast deadline.
    return at


def next_attempt(now):
    """Next model calculation window, subject to exchange schedule checks."""
    local = now.astimezone(model.MSK)
    for days in range(8):
        at = model.clock(local.date() + timedelta(days=days), 17, 45)
        if at.weekday() <= 3 and local < at:
            return at
    raise AssertionError("No model calculation window in the next week")


def _database():
    return Path(settings.BASE_DIR) / "data" / "morning" / "web-forecasts.sqlite3"


def _latest(ticker):
    path = _database()
    if not path.is_file():
        return None
    with closing(sqlite3.connect(path, timeout=3)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'forecasts'").fetchone():
            return None
        row = db.execute(
            "SELECT record, outcome FROM forecasts WHERE ticker = ? ORDER BY as_of DESC LIMIT 1",
            (ticker,),
        ).fetchone()
    if row is None:
        return None
    record = json.loads(row[0])
    forecast = _forecast(record["forecast"])
    at = datetime.fromisoformat(forecast["as_of"])
    created = datetime.fromisoformat(record["created_at"])
    target = model.clock(at.astimezone(model.MSK).date() + timedelta(days=1), 10)
    if (web_cutoff(created) != at or datetime.fromisoformat(forecast["target_at"]) != target
            or record["ticker"] != ticker or forecast["historical"] or "actual_price" in forecast):
        raise ValueError("Invalid stored forecast")
    record["forecast"] = forecast
    record["outcome"] = json.loads(row[1]) if row[1] else None
    return record


def _save(record, evidence):
    path = _database()
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=3)) as db, db:
        db.execute(
            """CREATE TABLE IF NOT EXISTS forecasts (
                ticker TEXT NOT NULL, as_of TEXT NOT NULL, record TEXT NOT NULL,
                evidence TEXT NOT NULL, outcome TEXT, PRIMARY KEY (ticker, as_of)
            )"""
        )
        db.execute(
            """INSERT INTO forecasts (ticker, as_of, record, evidence)
               VALUES (?, ?, ?, ?) ON CONFLICT (ticker, as_of) DO NOTHING""",
            (record["ticker"], record["forecast"]["as_of"], json.dumps(record, allow_nan=False),
             json.dumps(evidence, allow_nan=False)),
        )


def _save_outcome(record, outcome):
    with closing(sqlite3.connect(_database(), timeout=3)) as db, db:
        db.execute(
            "UPDATE forecasts SET outcome = ? WHERE ticker = ? AND as_of = ? AND outcome IS NULL",
            (json.dumps(outcome, allow_nan=False), record["ticker"], record["forecast"]["as_of"]),
        )


def _read_model(when, progress):
    artifact = load_artifact(when)
    if artifact is not None:
        return artifact
    progress("training")
    try:
        # The existing command fits the fixed model on verified, cached candles.
        # It does not download data or bypass the frozen protocol.
        call_command("train_morning_forecast", stdout=io.StringIO(), stderr=io.StringIO())
    except CommandError as exc:
        if str(exc).startswith("Зафиксированный протокол изменён"):
            raise ForecastUnavailable(
                "Код модели не совпадает с сохранённой версией обучения. "
                "Восстанови проверенный комплект модели и данных."
            ) from None
        raise ForecastUnavailable(
            "Не удалось подготовить модель. Проверь комплект сохранённой истории для семи акций."
        ) from None
    except (OSError, ValueError, KeyError):
        raise ForecastUnavailable(
            "Модель не готова. Для обучения нужна сохранённая проверенная история по всем семи акциям."
        ) from None
    except ImportError:
        raise ForecastUnavailable("Для обучения не установлены зависимости проекта.") from None
    artifact = load_artifact(now_utc())
    if artifact is None:
        raise ForecastUnavailable("Файл модели не прошёл проверку. Восстанови модель из проверенной копии проекта.")
    return artifact


def _calculate(ticker, when, progress):
    at = web_cutoff(when)
    if at is None:
        raise ForecastUnavailable("Новый расчёт доступен с понедельника по четверг после 17:45 МСК.")
    artifact = _read_model(when, progress)
    if ticker not in artifact["tickers"]:
        raise ForecastUnavailable("Этой акции нет в обученной модели. Выбери другую акцию.")
    progress("loading")
    from t_tech.invest.schemas import CandleInterval

    with open_client() as client:
        instrument = resolve(client, ticker)
        expected = artifact["instruments"][ticker]
        if any(instrument[key] != expected[key] for key in ("ticker", "uid", "lot", "currency")):
            raise ForecastUnavailable("Параметры акции изменились. Нужна новая проверка модели для этой акции.")
        schedules = client.instruments.trading_schedules(
            exchange="MOEX",
            from_=model.clock(at.date(), 0).astimezone(timezone.utc),
            to=model.clock(at.date() + timedelta(days=2), 0).astimezone(timezone.utc),
        )
        try:
            schedule = schedule_check(schedules, at)
        except CommandError:
            raise ForecastUnavailable(
                "Расписание биржи не подтверждает завтра в 10:00 МСК. "
                "Попробуй в следующий подходящий вечер: модель рассчитана на один день."
            ) from None
        start = model.clock(at.date() - timedelta(days=60), 0).astimezone(timezone.utc)
        bars = exchange_candles(
            client, instrument["uid"], start, at.astimezone(timezone.utc),
            CandleInterval.CANDLE_INTERVAL_15_MIN, 15,
        )
    progress("analyzing")
    if any(bar.time < start or bar.time + timedelta(minutes=15) > at for bar in bars):
        raise ForecastUnavailable("Часть свечей вышла за время расчёта. Попробуй запросить данные снова.")
    rows, coverage = model.build_examples(bars, ticker, instrument["lot"])
    row = next((sample for sample in rows if sample["at"] == at.isoformat()), None)
    if row is None:
        raise ForecastUnavailable("Не хватает закрытых свечей до 17:45 или данных за предыдущие 20 торговых дней.")
    if row["actual_price"] is not None or row["entry_price"] is not None:
        raise ForecastUnavailable("Не удалось отделить будущие цены от входных данных.")
    progress("forecasting")
    result = model.forecast(artifact["model"], row)
    result.pop("actual_price", None)
    result["historical"] = False
    result = _forecast(result)
    created = now_utc()
    if web_cutoff(created) != at:
        raise ForecastUnavailable("Расчёт не успел завершиться до полуночи МСК. Попробуй в следующий вечер.")
    record = {
        "ticker": ticker, "instrument_name": instrument["name"], "instrument_uid": instrument["uid"],
        "created_at": created.isoformat(), "model_sha256": artifact["artifact_sha256"],
        "forecast": result,
    }
    evidence = {
        "source": "T-Bank GetCandles · EXCHANGE", "schedule": schedule, "coverage": coverage,
        "bars": [{**asdict(bar), "time": bar.time.isoformat()} for bar in bars],
    }
    _save(record, evidence)


def _compare(record, when, progress):
    target = datetime.fromisoformat(record["forecast"]["target_at"])
    end = target + timedelta(minutes=15)
    if when < end:
        raise ForecastUnavailable("Свеча открытия ещё не закрылась. Сравнение доступно после 10:15 МСК.")
    progress("checking_actual")
    from t_tech.invest.schemas import CandleInterval

    with open_client() as client:
        bars = exchange_candles(
            client, record["instrument_uid"], target, end,
            CandleInterval.CANDLE_INTERVAL_15_MIN, 15,
        )
    actual = next((bar.open for bar in bars if bar.time == target and bar.volume > 0), None)
    if actual is None:
        raise ForecastUnavailable("Биржа пока не вернула завершённую свечу на 10:00 МСК. Попробуй позже.")
    _save_outcome(record, {"price": actual, "checked_at": now_utc().isoformat()})


def _forecast_for_display(record, when):
    forecast = dict(record["forecast"])
    target = datetime.fromisoformat(forecast["target_at"])
    forecast.update(
        historical=when >= target, created_at=record["created_at"],
        comparable_at=(target + timedelta(minutes=15)).isoformat(),
    )
    outcome = record["outcome"]
    if outcome is not None and when >= target + timedelta(minutes=15):
        actual = outcome["price"]
        error = abs(forecast["predicted_price"] - actual)
        forecast.update(actual_price=actual, actual_checked_at=outcome["checked_at"],
                        absolute_error_rub=error, absolute_error_pct=100 * error / actual)
    return forecast


def state(ticker, when=None):
    when = when or now_utc()
    with _lock:
        job = dict(_jobs.get(ticker, {}))
    record = _latest(ticker)
    payload = {"status": "ok" if record else "idle", "ticker": ticker, "entry_allowed": False,
               "message": "", **job}
    if record is not None:
        payload["instrument_name"] = record["instrument_name"]
        payload["forecast"] = _forecast_for_display(record, when)
    return payload


def _run(ticker, when, record, compare):
    def progress(stage):
        with _lock:
            _jobs[ticker] = {"status": "running", "stage": stage, "message": STAGES[stage]}

    try:
        if compare:
            _compare(record, when, progress)
        else:
            _calculate(ticker, when, progress)
    except ForecastUnavailable as exc:
        result = {"status": "error", "message": str(exc)}
    except (ValueError, KeyError, CommandError):
        result = {"status": "error", "message": "Не удалось проверить данные для расчёта. Попробуй позже."}
    except (OSError, sqlite3.Error):
        result = {"status": "error", "message": "Не удалось сохранить или прочитать расчёт. Проверь папку данных."}
    except Exception as exc:
        try:
            message = error_message(exc)
        except Exception:
            message = "Не удалось получить биржевые данные. Попробуй ещё раз позже."
        result = {"status": "error", "message": message}
    else:
        result = {"status": "ok", "message": "Фактическая цена получена." if compare else "Прогноз рассчитан."}
    with _lock:
        _jobs[ticker] = result


def start(ticker, action="forecast", when=None):
    when = when or now_utc()
    if ticker not in model.TICKERS:
        return {"ticker": ticker, "status": "error", "message": "Модель не поддерживает эту акцию."}, 422
    record = _latest(ticker)
    at = web_cutoff(when)
    target = datetime.fromisoformat(record["forecast"]["target_at"]) if record else None
    can_compare = record is not None and when >= target + timedelta(minutes=15) and record["outcome"] is None
    compare = action == "compare"
    if compare and not can_compare:
        if record and record["outcome"]:
            _clear_finished_job(ticker)
            return {**state(ticker, when), "status": "ok", "message": "Фактическая цена уже сохранена."}, 200
        message = ("Сравнение доступно через 15 минут после времени прогноза." if record
                   else "Сначала рассчитай прогноз по этой акции.")
        return {**state(ticker, when), "status": "error", "message": message}, 422
    if not compare and at is None:
        return {
            **state(ticker, when), "status": "error",
            "message": "Новый расчёт доступен с понедельника по четверг после 17:45 МСК.",
            "next_attempt_at": next_attempt(when).isoformat(),
        }, 422
    if not compare and record and datetime.fromisoformat(record["forecast"]["as_of"]) == at:
        _clear_finished_job(ticker)
        return {**state(ticker, when), "status": "ok", "message": "Расчёт за этот вечер уже сохранён."}, 200
    if not os.getenv("INVEST_TOKEN", "").strip():
        return {**state(ticker, when), "status": "error",
                "message": "Подключи Т-Банк в настройках проекта, чтобы получить биржевые данные."}, 503

    with _lock:
        existing = _jobs.get(ticker, {})
        if existing.get("status") == "running":
            return {"ticker": ticker, **existing}, 202
        if any(job["status"] == "running" for job in _jobs.values()):
            return {"ticker": ticker, "status": "error",
                    "message": "Идёт расчёт другой акции. Дождись окончания и попробуй снова."}, 409
        stage = "checking_actual" if compare else "loading"
        running = {"status": "running", "stage": stage, "message": STAGES[stage]}
        _jobs[ticker] = running
        try:
            Thread(target=_run, args=(ticker, when, record, compare), daemon=True).start()
        except RuntimeError:
            _jobs[ticker] = {"status": "error", "message": "Не удалось запустить расчёт. Попробуй ещё раз."}
            return {"ticker": ticker, **_jobs[ticker]}, 503
    return {"ticker": ticker, **running}, 202


def _clear_finished_job(ticker):
    with _lock:
        if _jobs.get(ticker, {}).get("status") != "running":
            _jobs.pop(ticker, None)
