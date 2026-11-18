"""The website estimates a price from cutoff data and saves the later outcome independently."""
import json
import os
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from django.test import Client, SimpleTestCase, override_settings

from . import morning_job as job
from .test_morning_command import NOW, INSTRUMENT, artifact, bars_before_cutoff, schedule


EVENING = NOW.replace(hour=22, minute=20)
TARGET = (NOW + timedelta(days=1)).replace(hour=10, minute=0)
ENDPOINT = "/api/morning-forecast/job/"


class MorningJobTests(SimpleTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(TemporaryDirectory())
        self.stack.enter_context(override_settings(BASE_DIR=Path(directory)))
        self.stack.enter_context(patch.object(job, "_jobs", {}))
        self.stack.enter_context(patch.dict(os.environ, {"INVEST_TOKEN": "fake-test-token"}))
        self.clock = self.stack.enter_context(patch.object(job, "now_utc", return_value=EVENING))
        self.load = self.stack.enter_context(patch.object(job, "load_artifact", return_value=artifact()))
        self.stack.enter_context(patch.object(job, "resolve", return_value={**INSTRUMENT, "name": "Тест"}))
        self.candles = self.stack.enter_context(patch.object(job, "exchange_candles", return_value=bars_before_cutoff()))
        self.schedule = schedule()
        self.client_stub = SimpleNamespace(instruments=SimpleNamespace(trading_schedules=Mock(return_value=self.schedule)))
        self.broker = self.stack.enter_context(patch.object(job, "open_client", return_value=MagicMock()))
        self.broker.return_value.__enter__.return_value = self.client_stub

    def calculate(self):
        progress = Mock()
        job._calculate("SBER", EVENING, progress)
        return [entry.args[0] for entry in progress.call_args_list]

    def test_web_can_calculate_late_evening_but_never_uses_later_bars(self):
        self.assertEqual(self.calculate(), ["loading", "analyzing", "forecasting"])
        self.assertEqual(self.candles.call_args.args[3], NOW.replace(minute=45).astimezone(timezone.utc))
        before = job.state("SBER", TARGET - timedelta(seconds=1))["forecast"]
        self.assertAlmostEqual(before["predicted_price"], before["anchor_price"] * 1.003)
        self.assertEqual(before["target_at"], TARGET.isoformat())
        self.assertEqual(before["created_at"], EVENING.isoformat())
        self.assertFalse(before["historical"])
        self.assertNotIn("actual_price", before)
        self.assertTrue(job.state("SBER", TARGET)["forecast"]["historical"])
        self.assertEqual(job.start("SBER", when=EVENING)[1], 200)
        self.candles.assert_called_once()

    def test_future_or_missing_cutoff_candle_is_rejected_without_saving(self):
        all_bars = self.candles.return_value
        self.candles.return_value = all_bars + [replace(all_bars[-1], time=NOW.replace(minute=45))]
        with self.assertRaises(job.ForecastUnavailable):
            self.calculate()
        self.candles.return_value = all_bars[:-1]
        with self.assertRaises(job.ForecastUnavailable):
            self.calculate()
        self.assertIsNone(job._latest("SBER"))

    def test_closed_next_calendar_day_is_not_replaced_by_a_later_session(self):
        self.schedule.exchanges[0].days[1].is_trading_day = False
        with self.assertRaisesMessage(job.ForecastUnavailable, "Расписание биржи"):
            self.calculate()
        self.candles.assert_not_called()
        self.assertFalse(job._database().exists())

    def test_before_window_and_weekend_report_next_attempt_without_using_broker(self):
        for when, date in ((NOW.replace(hour=17, minute=44), NOW.date()),
                           (NOW.replace(day=24), NOW.replace(day=26).date())):
            with self.subTest(when=when):
                result, status = job.start("SBER", when=when)
                self.assertEqual(status, 422)
                self.assertEqual(datetime.fromisoformat(result["next_attempt_at"]).date(), date)
        self.broker.assert_not_called()
        self.load.assert_not_called()

    def test_missing_token_has_actionable_error_and_no_io_to_broker(self):
        with patch.dict(os.environ, {"INVEST_TOKEN": ""}):
            result, code = job.start("SBER", when=EVENING)
        self.assertEqual(code, 503)
        self.assertIn("Подключи", result["message"])
        self.broker.assert_not_called()

    def test_comparison_waits_for_closed_target_and_does_not_use_another_bar(self):
        self.calculate()
        record = job._latest("SBER")
        self.broker.reset_mock()
        self.assertEqual(job.start("SBER", "compare", TARGET + timedelta(minutes=14))[1], 422)
        self.broker.assert_not_called()
        # Calling the regular button outside its window does not trigger a comparison.
        self.assertEqual(job.start("SBER", when=TARGET + timedelta(minutes=15))[1], 422)
        self.broker.assert_not_called()
        source = self.candles.return_value[-1]
        self.candles.return_value = [replace(source, time=TARGET + timedelta(minutes=15))]
        with self.assertRaisesMessage(job.ForecastUnavailable, "свечу на 10:00"):
            job._compare(record, TARGET + timedelta(minutes=15), Mock())
        self.assertNotIn("actual_price", job.state("SBER", TARGET + timedelta(minutes=15))["forecast"])
        self.clock.return_value = TARGET + timedelta(minutes=15)
        self.candles.return_value = [replace(source, time=TARGET)]
        job._compare(record, self.clock.return_value, Mock())
        value = job.state("SBER", self.clock.return_value)["forecast"]
        self.assertEqual(value["actual_price"], source.open)
        self.assertAlmostEqual(value["absolute_error_rub"], abs(value["predicted_price"] - source.open))
        self.assertAlmostEqual(value["absolute_error_pct"], value["absolute_error_rub"] / source.open * 100)
        self.assertNotIn("actual_price", job._latest("SBER")["forecast"])

    def test_job_status_and_worker_count_remain_bounded(self):
        with patch.object(job, "Thread") as thread:
            initial, status = job.start("SBER", when=EVENING)
            self.assertEqual((initial["stage"], status), ("loading", 202))
            self.assertEqual(job.start("SBER", when=EVENING)[1], 202)
            self.assertEqual(job.start("GAZP", when=EVENING)[1], 409)
            thread.assert_called_once()
            run = thread.call_args.kwargs
            run["target"](*run["args"])
        self.assertEqual(job.state("SBER")["status"], "ok")
        self.assertEqual(job.state("GAZP")["status"], "idle")

    def test_web_calls_existing_offline_training_only_if_required(self):
        self.load.side_effect = [None, artifact()]
        with patch.object(job, "call_command") as train:
            stages = self.calculate()
        self.assertEqual(stages[0], "training")
        self.assertEqual(train.call_args.args, ("train_morning_forecast",))
        self.assertFalse(job.state("SBER")["entry_allowed"])

    def test_unexpected_provider_error_is_redacted_and_releases_worker(self):
        with patch.object(job, "_calculate", side_effect=RuntimeError("SECRET_DO_NOT_LEAK")):
            job._run("SBER", EVENING, None, False)
        result = job.state("SBER")
        self.assertEqual(result["status"], "error")
        self.assertNotIn("SECRET_DO_NOT_LEAK", str(result))
        self.assertNotIn("forecast", result)
        with patch.object(job, "Thread") as thread:
            self.assertEqual(job.start("GAZP", when=EVENING)[1], 202)
            thread.assert_called_once()

    def test_endpoint_is_read_only_on_get_and_requires_local_csrf_post(self):
        client = Client(enforce_csrf_checks=True)
        data = json.dumps({"ticker": "sber"})
        with patch.object(job, "Thread") as thread:
            initial = client.get(ENDPOINT, {"ticker": "SBER"})
            self.assertEqual(initial.json()["status"], "idle")
            self.assertEqual(initial["Cache-Control"], "no-store")
            self.assertEqual(client.get(ENDPOINT, REMOTE_ADDR="192.0.2.1").status_code, 403)
            thread.assert_not_called()
            self.assertEqual(client.post(ENDPOINT, data, content_type="application/json").status_code, 403)
            csrf = initial.cookies["csrftoken"].value
            reply = client.post(ENDPOINT, data, content_type="application/json", HTTP_X_CSRFTOKEN=csrf)
            self.assertEqual(reply.status_code, 202)
            self.assertEqual(reply.json()["ticker"], "SBER")
            self.assertEqual(client.get(ENDPOINT).json()["stage"], "loading")
            thread.assert_called_once()
