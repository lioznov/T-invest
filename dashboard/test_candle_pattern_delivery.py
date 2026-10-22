import json
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
from unittest import TestCase
from unittest.mock import Mock, patch

from django.test import RequestFactory, override_settings

from .candle_pattern_panel import candle_panel
from .candle_pattern_research import load_candle_research
from .candle_patterns_v3 import VARIANTS
from .management.commands.study_candle_patterns import freeze
from .pattern_flow_study import PERIODS, TICKERS
from .signal_views import monitor_health
from .signal_worker import SignalWorker
from .test_pattern_flow_research import model, quality
from .test_price_patterns import AT, bars


class DeliveryTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        context = override_settings(BASE_DIR=self.base)
        context.enable()
        self.addCleanup(context.disable)

    def save(self, changed=None):
        directory = self.base/"data"
        signature = freeze(directory)
        saved = {"version": "candle_patterns_v3", "protocol_sha256": signature,
                 "at": "2026-10-06T13:00:00+00:00", "note": "Exploratory only",
                 "results": [{"ticker": ticker, "quality": quality(),
                              "variants": {key: {p: model() for p in PERIODS} for key in VARIANTS}}
                             for ticker in TICKERS]}
        if changed:
            changed(saved)
        path = directory/"candle-patterns-study-20261006-test.json"
        path.write_text(json.dumps(saved), encoding="utf-8")
        return path

    def test_no_report_is_not_zero_profit(self):
        self.assertEqual(load_candle_research("SBER")["status"], "insufficient")

    def test_positive_report_never_authorizes_trade(self):
        self.save()
        result = load_candle_research("SBER")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["patterns"]), 6)
        self.assertFalse(result["entry_allowed"])
        self.assertTrue(all(r["verdict"] == "exploratory" for r in result["patterns"]))

    def test_zero_trades_metrics_are_absent(self):
        def change(saved):
            for v in saved["results"][0]["variants"].values():
                v["recent"].update(trades=0, return_pct=0, stress_return_pct=0, win_rate_pct=None)
        self.save(change)
        self.assertTrue(all(r["return_pct"] is None and r["verdict"] == "insufficient"
                            for r in load_candle_research("SBER")["patterns"]))

    def test_protocol_tamper_fail_closed(self):
        self.save(lambda report: report.update(protocol_sha256="a"*64))
        self.assertEqual(load_candle_research("SBER")["status"], "insufficient")

    def test_malformed_metric_fail_closed(self):
        def change(saved):
            saved["results"][0]["variants"][next(iter(VARIANTS))]["recent"]["trades"] = True
        self.save(change)
        self.assertEqual(load_candle_research("SBER")["status"], "insufficient")

    def test_no_unknown_stock_zeros(self):
        self.save()
        result = load_candle_research("TATN")
        self.assertEqual(result["patterns"], [])
        self.assertFalse(result["entry_allowed"])

    def test_incomplete_or_stale_live_candles_not_shown(self):
        snapshot = {"candles": bars([(100, 100.2, 99.8, 100, 100)])}
        self.assertEqual(candle_panel(snapshot, AT+timedelta(minutes=5))["evaluated_at"], AT.isoformat())
        result = candle_panel(snapshot, AT+timedelta(minutes=16))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["items"]), 3)
        self.assertFalse(result["entry_allowed"])
        self.assertEqual(candle_panel(snapshot, AT+timedelta(hours=2))["markers"], [])

    def test_health_get_is_readonly(self):
        factory = RequestFactory()
        self.assertEqual(monitor_health(factory.get("/api/monitor-health/")).status_code, 200)
        self.assertEqual(monitor_health(factory.post("/api/monitor-health/")).status_code, 405)
        self.assertFalse((self.base/"data").exists())

    def test_owned_worker_uses_no_shell_or_visible_window_and_stops(self):
        worker = SignalWorker(["SBER"], 60, self.base)
        process = Mock()
        process.poll.return_value = None
        with patch("dashboard.signal_worker.subprocess.Popen", return_value=process) as launch:
            worker.start(self.base)
            self.assertTrue(worker.alive)
            args, kwargs = launch.call_args
            self.assertIn("monitor_signals", args[0])
            self.assertIn("--seconds", args[0])
            self.assertNotIn("shell", kwargs)
            self.assertEqual(kwargs["env"]["PYTHONIOENCODING"], "utf-8")
            worker.stop()
            process.terminate.assert_called_once()
            self.assertTrue(worker.output.closed)
